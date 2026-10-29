#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Measure controlled fixture tones against captured Core Audio and Kodi clocks.

Example:
  python3 tools/analyze_captured_ad_audio.py CAPTURE_DIRECTORY \
    --metadata tests/generated-audio/ad-extended/metadata.json \
    --preroll-samples 192512 --output CAPTURE_DIRECTORY/measured-audio.json

FFmpeg is an optional developer CLI dependency; the measurement core uses only
the standard library. Source offsets assume the declared fixture composition
preserves source duration (including equal-length midroll replacement). Preroll
length is supplied explicitly, never inferred from a player clock or recording.
"""
import argparse
from array import array
from bisect import bisect_right
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measure_tones(samples, sample_rate, frequencies, window_seconds=.1,
                  silence_rms=1e-4, minimum_tone_fraction=.35):
    """Classify complete windows; a failed tone match never implies silence."""
    if not _number(sample_rate) or sample_rate <= 0:
        raise ValueError('Analysis sample rate must be positive')
    window_samples = round(window_seconds * sample_rate)
    if window_samples < 1 or not 0 < minimum_tone_fraction <= 1 or silence_rms < 0:
        raise ValueError('Invalid analysis thresholds')
    choices = sorted(set(frequencies))
    if not choices or any(not _number(hz) or not 0 < hz < sample_rate / 2 for hz in choices):
        raise ValueError('Fixture tones must lie below the analysis Nyquist frequency')
    coefficients = [(hz, 2 * math.cos(2 * math.pi * hz / sample_rate)) for hz in choices]
    windows, regions = [], []
    peak = 0.
    for offset in range(0, len(samples) - window_samples + 1, window_samples):
        values = samples[offset:offset + window_samples]
        if any(not math.isfinite(value) for value in values):
            raise ValueError('Nonfinite decoded audio sample')
        peak = max(peak, max(map(abs, values)))
        mean_square = sum(value * value for value in values) / window_samples
        fraction, best = 0., None
        if mean_square < silence_rms * silence_rms:
            classification = 'silence'
        else:
            maximum = -1.
            for hz, coefficient in coefficients:
                a = b = 0.
                for value in values:
                    a, b = value + coefficient * a - b, a
                strength = max(0., a*a + b*b - coefficient*a*b)
                if strength > maximum:
                    maximum, best = strength, hz
            fraction = min(1., 2 * maximum / (window_samples**2 * mean_square))
            classification = 'tone' if fraction >= minimum_tone_fraction else 'unclassified'
        window = {'start_seconds': offset / sample_rate,
                  'end_seconds': (offset + window_samples) / sample_rate,
                  'classification': classification,
                  'tone_hz': best if classification == 'tone' else None,
                  'rms': math.sqrt(mean_square), 'tone_energy_fraction': fraction}
        windows.append(window)
        if (regions and regions[-1]['classification'] == classification
                and regions[-1]['tone_hz'] == window['tone_hz']):
            regions[-1]['end_seconds'] = window['end_seconds']
        else:
            regions.append({key: window[key] for key in (
                'start_seconds', 'end_seconds', 'classification', 'tone_hz')})
    tail = samples[len(windows) * window_samples:]
    if any(not math.isfinite(value) for value in tail):
        raise ValueError('Nonfinite decoded audio sample')
    peak = max(peak, max(map(abs, tail), default=0.))
    return {'analysis_frames': len(samples), 'analysis_sample_rate': sample_rate,
            'duration_seconds': len(samples) / sample_rate, 'window_samples': window_samples,
            'window_seconds': window_samples / sample_rate,
            'unclassified_tail_frames': len(tail), 'decoded_peak': peak,
            'silence_rms_threshold': silence_rms,
            'minimum_tone_energy_fraction': minimum_tone_fraction,
            'frequency_search': 'exact metadata frequencies',
            'regions': regions, 'windows': windows}


def _timing_evidence(capture, rows, measurement):
    rate = capture.get('format', {}).get('sample_rate')
    if not _number(rate) or rate <= 0:
        raise ValueError('Capture sample rate is missing or invalid')
    issues, buffers = [], [row for row in rows if row.get('event') == 'audio_buffer']
    total = expected_offset = flagged = timestamp_gaps = 0
    previous = None
    maximum_gap = 0.
    if capture.get('transport') != 'core_audio_process_tap':
        issues.append('unsupported_capture_transport')
    if capture.get('status') != 'completed':
        issues.append('capture_not_completed')
    if not buffers:
        issues.append('no_audio_buffers')
    for row in buffers:
        offset, count = row.get('frame_offset'), row.get('frame_count')
        if type(offset) is not int or type(count) is not int or offset < 0 or count <= 0:
            issues.append('invalid_frame_range')
            continue
        if offset != expected_offset:
            issues.append('noncontiguous_frame_offsets')
        total += count
        expected_offset = offset + count
        flags = row.get('input_timestamp', {}).get('flags')
        host = row.get('host_pts_ns')
        if type(flags) is not int or not flags & 2 or type(host) is not int:
            issues.append('invalid_input_host_timestamp')
        else:
            if previous is not None:
                gap = host - (previous['host_pts_ns'] + previous['frame_count'] / rate * 1e9)
                maximum_gap = max(maximum_gap, abs(gap))
                if abs(gap) > 1e9 / rate:
                    timestamp_gaps += 1
            previous = row
        flagged += int(row.get('discontinuity') is True)
    manifest_total = capture.get('total_frames')
    if type(manifest_total) is not int or total != manifest_total:
        issues.append('manifest_frame_count_mismatch')
    if capture.get('audio_buffers') != len(buffers):
        issues.append('manifest_buffer_count_mismatch')
    reported = capture.get('discontinuities')
    if reported != flagged:
        issues.append('manifest_discontinuity_count_mismatch')
    if flagged or reported or timestamp_gaps:
        issues.append('capture_timing_discontinuity')
    duration = total / rate
    if abs(duration - measurement['duration_seconds']) > 1 / measurement['analysis_sample_rate'] + 1e-9:
        issues.append('decoded_duration_mismatch')
    summary = {'native_sample_rate': rate, 'manifest_total_frames': manifest_total,
               'sidecar_total_frames': total, 'sidecar_audio_buffers': len(buffers),
               'duration_seconds': duration, 'reported_discontinuities': reported,
               'sidecar_discontinuities': flagged, 'observed_timestamp_gaps': timestamp_gaps,
               'maximum_host_step_error_ns': maximum_gap,
               'issues': list(dict.fromkeys(issues)),
               'usable_for_clock_comparison': not issues}
    return summary, buffers, [row['frame_offset'] for row in buffers]


def _host_at(seconds, buffers, rate, offsets=None):
    """Use the containing acquisition buffer, not the recorder's start time."""
    frame = seconds * rate
    if offsets is None:
        offsets = [row['frame_offset'] for row in buffers]
    index = bisect_right(offsets, frame) - 1
    if index < 0:
        raise ValueError('Audio position precedes captured frames')
    row = buffers[index]
    if frame > row['frame_offset'] + row['frame_count'] + 1e-6:
        raise ValueError('Audio position is outside captured frames')
    return round(row['host_pts_ns'] + (frame - row['frame_offset']) / rate * 1e9)


def _rpc_evidence(observation):
    timeline = observation.get('timeline', [])
    compatible = observation.get('clock') == 'mach_absolute_time'
    valid, mismatched, unverified, invalid = [], 0, 0, 0
    previous_request = None
    for ordinal, sample in enumerate(timeline):
        if not sample.get('playing'):
            continue
        if sample.get('item_matches_expected') is False:
            mismatched += 1
            continue
        if sample.get('item_matches_expected') is not True:
            unverified += 1
            continue
        start, end = sample.get('request_host_ns'), sample.get('response_host_ns')
        clock = sample.get('player_time_seconds')
        if (type(start) is not int or type(end) is not int or start > end
                or not _number(clock) or (previous_request is not None and start < previous_request)):
            invalid += 1
            continue
        previous_request = start
        if compatible:
            valid.append({'request_host_ns': start, 'response_host_ns': end,
                          'raw_clock_seconds': clock, 'speed': sample.get('speed'),
                          '_ordinal': ordinal})
    summary = {'observed_samples': len(timeline), 'usable_clock_samples': len(valid),
               'mismatched_item_samples': mismatched, 'unverified_item_samples': unverified,
               'invalid_clock_samples': invalid, 'compatible_clock_domain': compatible,
               'item_identity_limit': 'The recorded match flag is not independent exact-session evidence.'}
    return summary, valid


def _public_sample(sample):
    return {key: value for key, value in sample.items() if not key.startswith('_')} if sample else None


def _clock_comparison(interval, expected, samples):
    if not samples:
        return {'status': 'unavailable', 'reason': 'no_matching_clock_observations'}
    lower, upper = interval
    before = next((sample for sample in reversed(samples) if sample['response_host_ns'] <= lower), None)
    after = next((sample for sample in samples if sample['request_host_ns'] >= upper), None)
    overlapping = [sample for sample in samples
                   if sample['response_host_ns'] >= lower and sample['request_host_ns'] <= upper]
    selected = ([before] if before else []) + overlapping + ([after] if after else [])
    if not selected:
        return {'status': 'unavailable', 'reason': 'no_bracketing_clock_observations'}
    values = [sample['raw_clock_seconds'] for sample in selected]
    return {'status': 'observed', 'bracket_complete': before is not None and after is not None,
            'before': _public_sample(before), 'after': _public_sample(after),
            'overlapping': [_public_sample(sample) for sample in overlapping],
            'observed_raw_clock_range_seconds': [min(values), max(values)],
            'clock_minus_expected_range_seconds': [min(values) - expected, max(values) - expected],
            'maximum_rpc_duration_seconds': max(
                (sample['response_host_ns'] - sample['request_host_ns']) / 1e9 for sample in selected),
            'observed_span_seconds': (selected[-1]['response_host_ns'] - selected[0]['request_host_ns']) / 1e9,
            'nonmonotonic_observations': any(a > b for a, b in zip(values, values[1:])),
            'all_observed_speeds_one': all(sample['speed'] == 1 for sample in selected)}


def _source_boundaries(metadata, measurement, preroll_samples, buffers, offsets, timing, rpc):
    assets = metadata['assets']
    counts = Counter(item['expected_tone_hz'] for item in assets.values())
    unique = {item['expected_tone_hz']: (name, item) for name, item in assets.items()
              if counts[item['expected_tone_hz']] == 1 and 'source_start_sample' in item}
    result, seen = [], set()
    for index, region in enumerate(measurement['regions']):
        hz = region['tone_hz']
        if (region['classification'] != 'tone' or hz not in unique or hz in seen
                or region['end_seconds'] - region['start_seconds'] < .3 - 1e-9):
            continue
        seen.add(hz)
        name, asset = unique[hz]
        expected_sample = asset['source_start_sample'] + preroll_samples
        expected_seconds = expected_sample / metadata['sample_rate']
        at, quantum = region['start_seconds'], measurement['window_seconds']
        previous = measurement['regions'][index - 1] if index else None
        unclassified = (previous['end_seconds'] - previous['start_seconds']
                        if previous and previous['classification'] == 'unclassified' else 0.)
        boundary = {'asset': name, 'source_sequence': asset.get('source_sequence'), 'tone_hz': hz,
                    'source_start_sample': asset['source_start_sample'],
                    'expected_output_sample': expected_sample, 'expected_output_seconds': expected_seconds,
                    'first_sustained_window_seconds': at,
                    'classified_region_seconds': region['end_seconds'] - at,
                    'classification_interval_seconds': [max(0., at - quantum), at + quantum],
                    'preceding_unclassified_seconds': unclassified,
                    'onset_may_be_masked': bool(unclassified),
                    'clock_observation': {'status': 'unavailable', 'reason': 'invalid_capture_timing'}}
        if timing['usable_for_clock_comparison']:
            interval = [_host_at(seconds, buffers, timing['native_sample_rate'], offsets)
                        for seconds in boundary['classification_interval_seconds']]
            boundary['classification_host_interval_ns'] = interval
            boundary['clock_observation'] = _clock_comparison(interval, expected_seconds, rpc)
        result.append(boundary)
    return result


def _plateaus(samples, measurement, timing, buffers, offsets):
    if not timing['usable_for_clock_comparison']:
        return []
    groups, current = [], []
    for sample in samples:
        if sample['speed'] != 1:
            if current:
                groups.append(current)
            current = []
            continue
        if current and (sample['_ordinal'] != current[-1]['_ordinal'] + 1
                        or sample['raw_clock_seconds'] != current[-1]['raw_clock_seconds']):
            groups.append(current)
            current = []
        current.append(sample)
    if current:
        groups.append(current)
    result = []
    for group in groups:
        start, end = group[0]['response_host_ns'], group[-1]['request_host_ns']
        if len(group) < 3 or end - start < 500_000_000:
            continue
        lengths, tones = Counter(), set()
        for region in measurement['regions']:
            lower = _host_at(region['start_seconds'], buffers, timing['native_sample_rate'], offsets)
            upper = _host_at(region['end_seconds'], buffers, timing['native_sample_rate'], offsets)
            overlap = max(0, min(end, upper) - max(start, lower)) / 1e9
            lengths[region['classification']] += overlap
            if overlap and region['classification'] == 'tone':
                tones.add(region['tone_hz'])
        result.append({'raw_clock_seconds': group[0]['raw_clock_seconds'], 'samples': len(group),
                       'observed_host_interval_ns': [start, end],
                       'observed_duration_seconds': (end - start) / 1e9,
                       'classified_tones_hz': sorted(tones),
                       'classified_tone_seconds': lengths['tone'],
                       'unclassified_audio_seconds': lengths['unclassified'],
                       'silence_seconds': lengths['silence']})
    return result


def analyze_capture(samples, analysis_rate, metadata, capture, timing_rows, observation,
                    preroll_samples):
    """Return descriptive evidence without deciding a universal mapping claim."""
    if type(preroll_samples) is not int or preroll_samples < 0:
        raise ValueError('Explicit preroll samples must be a nonnegative integer')
    source_rate = metadata.get('sample_rate')
    assets = metadata.get('assets')
    if not _number(source_rate) or source_rate <= 0 or not isinstance(assets, dict) or not assets:
        raise ValueError('Generated fixture metadata is required')
    for asset in assets.values():
        if (not isinstance(asset, dict) or not _number(asset.get('expected_tone_hz'))
                or ('source_start_sample' in asset and
                    (type(asset['source_start_sample']) is not int or asset['source_start_sample'] < 0))):
            raise ValueError('Invalid generated asset metadata')
    measurement = measure_tones(samples, analysis_rate,
                                [asset['expected_tone_hz'] for asset in assets.values()])
    timing, buffers, offsets = _timing_evidence(capture, timing_rows, measurement)
    rpc_summary, rpc = _rpc_evidence(observation)
    return {'schema_version': 1, 'kind': 'controlled_captured_audio_measurement',
            'composition_assumptions': {'preroll_samples': preroll_samples,
                'source_sample_rate': source_rate, 'midroll_replacement_preserves_source_duration': True},
            'measurement': measurement, 'capture_timing': timing, 'rpc': rpc_summary,
            'source_boundaries': _source_boundaries(metadata, measurement, preroll_samples,
                                                   buffers, offsets, timing, rpc),
            'raw_clock_plateaus': _plateaus(rpc, measurement, timing, buffers, offsets),
            'limitations': [
                'Tone classification uses 100 ms windows; surrounding RPC intervals add polling uncertainty.',
                'Classification intervals describe detected tone onset, not guaranteed segment-start bounds.',
                'Speech, mixing or resampling can mask or shift tones; unclassified audio is not silence.',
                'Clock ranges are observed values, without interpolation or extrapolation through pauses or seeks.',
                'Recorded item-match flags do not independently prove exact session identity.',
                'Process audio does not establish physical speaker audibility or universal clock mapping.'],
            'mapping_proven': False, 'speaker_audibility_verified': False}


def _decode(path, sample_rate, ffmpeg):
    result = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a:0',
                             '-ac', '1', '-ar', str(sample_rate), '-f', 'f32le', 'pipe:1'],
                            capture_output=True, check=True, timeout=180)
    samples = array('f')
    samples.frombytes(result.stdout)
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def _provenance(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(path.resolve()), 'sha256': digest.hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture_directory', type=Path)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--preroll-samples', type=int, required=True)
    parser.add_argument('--observations', type=Path,
                        help='Defaults to kodi-clock-observation.json in the capture directory')
    parser.add_argument('--output', type=Path, help='Write the report here; otherwise emit JSON to stdout')
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    args = parser.parse_args(argv)
    if not args.ffmpeg:
        parser.error('FFmpeg is required only for CLI audio decoding')
    root = args.capture_directory.resolve()
    manifest_path = root / 'capture.json'
    capture = json.loads(manifest_path.read_text())
    audio_path = (root / capture.get('audio_file', 'audio.caf')).resolve()
    timing_path = (root / capture.get('timing_file', 'timing.jsonl')).resolve()
    if audio_path.parent != root or timing_path.parent != root:
        parser.error('Capture audio and timing files must be inside the capture directory')
    observation_path = args.observations or root / 'kodi-clock-observation.json'
    paths = {'capture': manifest_path, 'audio': audio_path, 'timing': timing_path,
             'observations': observation_path, 'metadata': args.metadata}
    metadata = json.loads(args.metadata.read_text())
    timing = [json.loads(line) for line in timing_path.read_text().splitlines() if line.strip()]
    observed = json.loads(observation_path.read_text())
    report = analyze_capture(_decode(audio_path, 8000, args.ffmpeg), 8000,
                             metadata, capture, timing, observed, args.preroll_samples)
    report['inputs'] = {name: _provenance(path) for name, path in paths.items()}
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
        print(json.dumps({'output': str(args.output), 'duration_seconds': report['measurement']['duration_seconds'],
                          'source_boundaries': len(report['source_boundaries']),
                          'raw_clock_plateaus': len(report['raw_clock_plateaus']),
                          'timing_issues': report['capture_timing']['issues'], 'mapping_proven': False}))
    else:
        print(encoded, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
