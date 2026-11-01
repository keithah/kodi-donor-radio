# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent synthetic measurement checks for controlled audio captures."""
import importlib
import importlib.util
import json
import math
import random
import shutil
import struct
import subprocess
import wave

import pytest


BASE_NS = 1_000_000_000_000


def get_analyzer():
    name = 'tools.analyze_captured_ad_audio'
    assert importlib.util.find_spec(name) is not None, 'A reproducible capture analyzer is required'
    return importlib.import_module(name)


def tone(hz, seconds, rate=8000):
    return [.15 * math.sin(2 * math.pi * hz * sample / rate)
            for sample in range(round(seconds * rate))]


def noise(seconds, rate=8000):
    rng = random.Random(742)
    return [rng.uniform(-.2, .2) for _ in range(round(seconds * rate))]


def metadata():
    return {'sample_rate': 48000, 'assets': {
        'source000.ts': {'expected_tone_hz': 440, 'source_sequence': 0,
                         'source_start_sample': 0, 'samples': 19200},
        'source001.ts': {'expected_tone_hz': 440, 'source_sequence': 1,
                         'source_start_sample': 19200, 'samples': 19200},
        'source006.ts': {'expected_tone_hz': 1120, 'source_sequence': 6,
                         'source_start_sample': 96000, 'samples': 19200},
        'source007.ts': {'expected_tone_hz': 1140, 'source_sequence': 7,
                         'source_start_sample': 115200, 'samples': 19200},
        'source127.ts': {'expected_tone_hz': 3540, 'source_sequence': 127,
                         'source_start_sample': 12224512, 'samples': 96256},
        'preroll000.ts': {'expected_tone_hz': 990, 'samples': 48000},
    }}


def capture_evidence(seconds):
    frames = round(seconds * 48000)
    rows = []
    for offset in range(0, frames, 4800):
        rows.append({'event': 'audio_buffer', 'frame_offset': offset,
                     'frame_count': min(4800, frames - offset),
                     'host_pts_ns': BASE_NS + round(offset / 48000 * 1e9),
                     'input_timestamp': {'flags': 3}, 'discontinuity': False})
    capture = {'status': 'completed', 'transport': 'core_audio_process_tap',
               'format': {'sample_rate': 48000}, 'total_frames': frames,
               'audio_buffers': len(rows), 'discontinuities': 0}
    return capture, rows


def observations(seconds, value=lambda seconds: seconds, speed=1):
    timeline = []
    for index in range(round(seconds * 10)):
        at = index / 10 + .05
        timeline.append({'request_host_ns': BASE_NS + round((at - .002) * 1e9),
                         'response_host_ns': BASE_NS + round((at + .002) * 1e9),
                         'player_time_seconds': value(at), 'speed': speed,
                         'playing': True, 'item_matches_expected': True})
    return {'clock': 'mach_absolute_time', 'timeline': timeline}


def analyze(analyzer, samples, *, rows=None, observation=None):
    capture, default_rows = capture_evidence(len(samples) / 8000)
    return analyzer.analyze_capture(
        samples, 8000, metadata(), capture, default_rows if rows is None else rows,
        observations(len(samples) / 8000) if observation is None else observation,
        preroll_samples=48000)


def test_silence_tones_and_unclassified_audio_are_distinct():
    analyzer = get_analyzer()
    samples = [0.] * 1600 + noise(.3) + tone(1120, .5) + tone(3540, .3)
    measured = analyzer.measure_tones(samples, 8000, [1120, 3540])
    assert [region['classification'] for region in measured['regions']] == [
        'silence', 'unclassified', 'tone', 'tone']
    assert measured['regions'][2]['tone_hz'] == 1120
    assert measured['regions'][3]['tone_hz'] == 3540
    assert measured['window_seconds'] == .1
    assert all(window['rms'] > .01 for window in measured['windows'][2:5])


def test_capture_timing_precomputes_acquisition_buffer_offsets():
    analyzer = get_analyzer()
    capture, rows = capture_evidence(.5)
    measurement = analyzer.measure_tones(tone(1120, .5), 8000, [1120])
    timing, buffers, offsets = analyzer._timing_evidence(capture, rows, measurement)
    assert timing['usable_for_clock_comparison'] is True
    assert offsets == [row['frame_offset'] for row in buffers]
    assert analyzer._host_at(.2, buffers, 48000, offsets) == BASE_NS + 200_000_000


def test_null_capture_format_reports_missing_sample_rate():
    analyzer = get_analyzer()
    with pytest.raises(ValueError, match='Capture sample rate is missing or invalid'):
        analyzer._timing_evidence({'format': None}, [], {})


def test_only_first_sustained_unambiguous_source_tones_are_correlated():
    analyzer = get_analyzer()
    samples = tone(440, .3) + tone(1120, .1) + noise(.2) + tone(1120, .5)
    report = analyze(analyzer, samples)
    boundaries = report['source_boundaries']
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary['asset'] == 'source006.ts'
    assert boundary['first_sustained_window_seconds'] == pytest.approx(.6)
    assert boundary['expected_output_sample'] == 144000
    assert boundary['expected_output_seconds'] == 3
    assert boundary['preceding_unclassified_seconds'] == pytest.approx(.2)
    assert boundary['onset_may_be_masked'] is True
    assert report['mapping_proven'] is False
    assert report['speaker_audibility_verified'] is False


def test_rpc_comparison_uses_observed_intervals_and_never_extrapolates():
    analyzer = get_analyzer()
    samples = [0.] * 4000 + tone(1120, .5)
    report = analyze(analyzer, samples, observation=observations(1, lambda at: at + 2.5))
    boundary = report['source_boundaries'][0]
    comparison = boundary['clock_observation']
    assert comparison['bracket_complete'] is True
    assert comparison['before']['response_host_ns'] <= boundary['classification_host_interval_ns'][0]
    assert comparison['after']['request_host_ns'] >= boundary['classification_host_interval_ns'][1]
    assert comparison['observed_raw_clock_range_seconds'] == pytest.approx([2.85, 3.15])
    assert comparison['clock_minus_expected_range_seconds'] == pytest.approx([-.15, .15])
    assert comparison['maximum_rpc_duration_seconds'] == pytest.approx(.004)
    assert 'interpolated_clock' not in comparison


@pytest.mark.parametrize('fault', ['missing_buffer', 'invalid_host_flag', 'timestamp_gap',
                                   'missing_frame_offset', 'null_input_timestamp'])
def test_invalid_capture_timing_suppresses_clock_correlation(fault):
    analyzer = get_analyzer()
    samples = [0.] * 1600 + tone(1120, .5)
    _, rows = capture_evidence(.7)
    if fault == 'missing_buffer':
        del rows[1]
    elif fault == 'invalid_host_flag':
        rows[1]['input_timestamp']['flags'] = 1
    elif fault == 'timestamp_gap':
        rows[1]['host_pts_ns'] += 20_000_000
        rows[1]['discontinuity'] = True
    elif fault == 'missing_frame_offset':
        del rows[1]['frame_offset']
    else:
        rows[1]['input_timestamp'] = None
    report = analyze(analyzer, samples, rows=rows)
    assert report['capture_timing']['issues']
    assert report['capture_timing']['usable_for_clock_comparison'] is False
    assert report['capture_timing']['manifest_total_frames'] == 33600
    assert report['source_boundaries'][0]['clock_observation']['status'] == 'unavailable'
    assert report['mapping_proven'] is False


def test_report_counts_actual_frames_and_reports_partial_window_tail():
    analyzer = get_analyzer()
    samples = tone(1120, .435)
    report = analyze(analyzer, samples)
    assert report['capture_timing']['manifest_total_frames'] == 20880
    assert report['capture_timing']['sidecar_total_frames'] == 20880
    assert report['capture_timing']['reported_discontinuities'] == 0
    assert report['capture_timing']['observed_timestamp_gaps'] == 0
    assert report['measurement']['analysis_frames'] == 3480
    assert report['measurement']['unclassified_tail_frames'] == 280
    assert report['capture_timing']['usable_for_clock_comparison'] is True


def test_frozen_clock_reports_tone_and_unclassified_audio_without_calling_them_silence():
    analyzer = get_analyzer()
    samples = noise(1) + tone(1120, 1)
    report = analyze(analyzer, samples, observation=observations(2, lambda _: 7.25))
    assert len(report['raw_clock_plateaus']) == 1
    plateau = report['raw_clock_plateaus'][0]
    assert plateau['raw_clock_seconds'] == 7.25
    assert plateau['samples'] == 20
    assert plateau['classified_tone_seconds'] >= .8
    assert plateau['unclassified_audio_seconds'] >= .8
    assert plateau['silence_seconds'] == 0
    paused = analyze(analyzer, samples, observation=observations(2, lambda _: 7.25, speed=0))
    assert paused['raw_clock_plateaus'] == []


def test_stale_item_samples_do_not_become_matching_clock_evidence():
    analyzer = get_analyzer()
    samples = tone(1120, .5)
    observed = observations(.5)
    for sample in observed['timeline']:
        sample['item_matches_expected'] = False
    report = analyze(analyzer, samples, observation=observed)
    assert report['rpc']['mismatched_item_samples'] == 5
    assert report['source_boundaries'][0]['clock_observation']['status'] == 'unavailable'
    assert report['raw_clock_plateaus'] == []


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='Optional CLI decoder requires FFmpeg')
def test_cli_reads_real_caf_and_writes_reproducible_measurement(tmp_path):
    analyzer = get_analyzer()
    capture, rows = capture_evidence(.5)
    pcm = tone(1120, .5, rate=48000)
    wav = tmp_path / 'input.wav'
    with wave.open(str(wav), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b''.join(struct.pack('<h', round(value * 32767)) for value in pcm))
    subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-i', str(wav),
                    '-c:a', 'pcm_f32le', str(tmp_path / 'audio.caf')], check=True)
    capture.update(audio_file='audio.caf', timing_file='timing.jsonl')
    (tmp_path / 'capture.json').write_text(json.dumps(capture))
    (tmp_path / 'timing.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    (tmp_path / 'kodi-clock-observation.json').write_text(json.dumps(observations(.5)))
    (tmp_path / 'metadata.json').write_text(json.dumps(metadata()))
    output = tmp_path / 'measured.json'
    assert analyzer.main([str(tmp_path), '--metadata', str(tmp_path / 'metadata.json'),
                          '--preroll-samples', '48000', '--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['measurement']['regions'][0]['tone_hz'] == 1120
    assert report['capture_timing']['usable_for_clock_comparison'] is True
    assert report['inputs']['audio']['sha256']
    assert report['mapping_proven'] is False
