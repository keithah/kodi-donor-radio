#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compare a KodiAudioCapture recording with the offline splice of the same stream.

usage: analyze_splice_capture.py CAPTURE_DIR CODEC CREATIVE_NAME LOOPS
CREATIVE_NAME is creative-short or creative-long; LOOPS covers the capture.
The reference is the exact relay output (same fixture, same splicer) decoded by
FFmpeg. Transitions are aligned on programme-only tone changes, so splice
boundary errors exclude Kodi startup and output latency. Prints JSON.
Developer tool; needs FFmpeg and the generated splice fixtures.
"""
import json, os, shutil, statistics, subprocess, sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'plugin.audio.kodi_donor_radio' / 'resources' / 'lib'))
import continuous_splice_fixtures as F
import continuous_splice as cs
from icy import IcyDecoder

FF = shutil.which('ffmpeg')
RATE = F.RATE
WINDOW = 441


def reference(codec, creative_name, loops):
    assets = F.DEFAULT_ASSETS
    source = (assets / ('source.' + codec)).read_bytes()
    creative = cs.prepare_creative(codec, (assets / ('%s.%s' % (creative_name, codec))).read_bytes())
    stream, _, _ = F.looped_icy_stream(codec, source, loops)
    events = []
    splicer = cs.ContinuousSplicer(codec, creative, lambda k, d: events.append((k, d)))
    decoder = None
    decoder = IcyDecoder(F.METAINT, lambda v: splicer.observe_metadata(v, decoder.audio_position))
    out = bytearray()
    for i in range(0, len(stream), 65536):
        out += splicer.feed(decoder.feed(stream[i:i + 65536]))
    out += splicer.finish()
    pcm, _ = F.decode_pcm(FF, bytes(out), codec)
    return pcm, events


def capture_pcm(directory):
    manifest = json.load(open(os.path.join(directory, 'capture.json')))
    audio = os.path.join(directory, manifest.get('audio_file', 'audio.caf'))
    result = subprocess.run([FF, '-hide_banner', '-loglevel', 'error', '-i', audio, '-ac', '1',
                             '-ar', str(RATE), '-f', 's16le', 'pipe:1'], capture_output=True, check=True)
    pcm = array('h')
    pcm.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    return pcm, manifest


def classify(runs):
    """Label each transition as splice (touches CREATIVE) or programme."""
    labelled = []
    for (start, tone), (_, previous) in zip(runs[1:], runs[:-1]):
        kind = 'splice' if F.CREATIVE in (tone, previous) else 'programme'
        labelled.append((start, previous, tone, kind))
    return labelled


def main():
    directory, codec, creative_name, loops = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    ref_pcm, events = reference(codec, creative_name, loops)
    cap_pcm, manifest = capture_pcm(directory)
    ref_track, cap_track = F.tone_track(ref_pcm), F.tone_track(cap_pcm)
    ref = classify(F.transitions(ref_track))
    cap = classify(F.transitions(cap_track))
    # Align on the first programme transition present in both, then match in order.
    matched, unmatched, missing = [], [], []
    j = 0
    for start, a, b, kind in cap:
        k = j
        while k < len(ref) and (ref[k][1], ref[k][2]) != (a, b):
            k += 1
        if k == len(ref):
            # Keep the cursor: one extra capture transition must not end the comparison.
            unmatched.append({'capture_s': round(start / RATE, 3), 'from': a, 'to': b})
            continue
        if matched:
            # Reference transitions skipped between two matches were not heard.
            missing.extend({'reference_s': round(r[0] / RATE, 3), 'from': r[1], 'to': r[2],
                            'kind': r[3]} for r in ref[j:k])
        matched.append((kind, a, b, start, ref[k][0]))
        j = k + 1
    programme = [c - r for kind, _, _, c, r in matched if kind == 'programme']
    if not programme:
        print(json.dumps({'capture_status': manifest.get('status'),
                          'error': 'no programme transition to align on',
                          'unmatched_capture_transitions': unmatched}, indent=1))
        return
    offset = statistics.median(programme)
    rows = [{'kind': kind, 'from': a, 'to': b,
             'capture_s': round(c / RATE, 3),
             'error_ms': round((c - r - offset) / RATE * 1000, 1)} for kind, a, b, c, r in matched]
    ref_gaps = F.gaps(ref_track)
    cap_gaps = [(start - offset, windows) for start, windows in F.gaps(cap_track)]
    def near(gaps, at):
        """Milliseconds of unclear audio overlapping +/- 3 MP3 frames of a boundary."""
        low, high = at - 3 * 1152, at + 3 * 1152
        overlap = sum(max(0, min(high, s + w * WINDOW) - max(low, s)) for s, w in gaps)
        return overlap / RATE * 1000
    boundary = []
    for kind, a, b, c, r in matched:
        if kind == 'splice':
            boundary.append({'from': a, 'to': b, 'reference_unclear_ms': round(near(ref_gaps, r), 1),
                             'kodi_unclear_ms': round(near(cap_gaps, c - offset), 1)})
    first = matched[0][3] if matched else 0
    last = matched[-1][3] if matched else 0
    kodi_only = []
    for start, windows in cap_gaps:
        if not first <= start + offset <= last:
            continue
        # Count only the part of a capture gap that no reference gap explains.
        end = start + windows * WINDOW
        covered = sum(max(0, min(end, s + w * WINDOW) - max(start, s)) for s, w in ref_gaps)
        extra = end - start - covered
        if extra > 0:
            kodi_only.append({'at_s': round((start + offset) / RATE, 3),
                              'ms': round(extra / RATE * 1000, 1)})
    report = {
        'capture_status': manifest.get('status'), 'capture_frames': manifest.get('total_frames'),
        'native_rate': manifest.get('format', {}).get('sample_rate') if isinstance(manifest.get('format'), dict) else None,
        'alignment_offset_s': round(offset / RATE, 3),
        'programme_alignment_spread_ms': round((max(programme) - min(programme)) / RATE * 1000, 1),
        'transitions': rows, 'unmatched_capture_transitions': unmatched,
        'missing_reference_transitions': missing,
        'splice_boundary_unclear_audio': boundary,
        'kodi_only_unclear_runs_between_first_and_last_transition': kodi_only,
        'relay_reference_events': [(k, {x: d.get(x) for x in ('reason', 'start_sample', 'rejoin_sample',
                                                               'boundary_error_bytes', 'rejoin_reservoir_bytes')})
                                   for k, d in events],
    }
    print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()
