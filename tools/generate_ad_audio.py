#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Generate controlled AAC-LC MPEG-TS assets from checked-in descriptions.

FFmpeg, FFprobe and espeak are developer tools only. Output is ignored by git.
Each segment is independently decodable and contains exactly 94 AAC frames.
"""
import argparse
from array import array
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


DESCRIPTION = Path(__file__).with_name('ad_audio_sources.json')


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def probe(path, ffprobe):
    result = run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
                  '-show_entries', 'stream=codec_name,sample_rate,channels:packet=pts,duration_ts',
                  '-show_streams', '-show_packets', '-of', 'json', str(path)],
                 capture_output=True, text=True)
    data = json.loads(result.stdout)
    stream = data['streams'][0]
    packets = data['packets']
    decoded = run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
                   '-show_entries', 'frame=nb_samples', '-show_frames', '-of', 'json', str(path)],
                  capture_output=True, text=True)
    frames = json.loads(decoded.stdout)['frames']
    pts = [int(packet['pts']) for packet in packets if 'pts' in packet]
    pts_steps_ok = (len(pts) == len(packets) and
                    all(b - a == 1920 for a, b in zip(pts, pts[1:])))
    # AAC-LC carries 1024 decoded samples per access unit. Return measured
    # values so the relay smoke can report deviations instead of aborting.
    return {'codec': stream['codec_name'], 'sample_rate': int(stream['sample_rate']),
            'channels': int(stream['channels']), 'aac_frames': len(packets),
            'decoded_frames': len(frames), 'pts_steps_ok': pts_steps_ok,
            'samples': sum(int(frame['nb_samples']) for frame in frames),
            'first_pts': pts[0] if pts else None,
            'last_pts': pts[-1] if pts else None}


def validate_probe(info, path):
    """Raise when generated fixtures violate the declared AAC-TS contract."""
    if info['codec'] != 'aac':
        raise ValueError('%s: expected AAC audio' % path)
    if info['sample_rate'] != 48000 or info['channels'] != 2:
        raise ValueError('%s: expected 48 kHz stereo audio' % path)
    if info['aac_frames'] != 94 or info['decoded_frames'] != 94:
        raise ValueError('%s: expected 94 AAC frames' % path)
    if not info['pts_steps_ok']:
        raise ValueError('%s: expected contiguous 1920-tick PTS steps' % path)


def validate_segment_timeline(segments):
    """Require numbered source/ad TS segments to form a continuous PTS timeline."""
    previous = {}
    for filename, info in sorted(segments.items()):
        match = re.fullmatch(r'([A-Za-z_-]+)(\d{3})\.ts', filename)
        if not match:
            continue  # e.g. the deliberately mutated source003 fixture.
        prefix, sequence = match.group(1), int(match.group(2))
        prior = previous.get(prefix)
        if prior is None:
            if sequence != 0:
                raise ValueError('%s: segment sequence is not contiguous from zero' % filename)
        else:
            prior_sequence, prior_last_pts = prior
            if sequence != prior_sequence + 1:
                raise ValueError('%s: segment sequence is not contiguous' % filename)
            if info['first_pts'] != prior_last_pts + 1920:
                raise ValueError('%s: PTS is not contiguous with previous segment' % filename)
        previous[prefix] = (sequence, info['last_pts'])


def decoded_tone(path, ffmpeg, choices):
    # This is an independent decode of the encoded transport stream. Speech is
    # placed at the start, so inspect a later 0.4 s window for the steady tone.
    result = run([ffmpeg, '-v', 'error', '-i', str(path), '-ss', '1.2', '-t', '0.4',
                  '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', 'pipe:1'],
                 capture_output=True)
    samples = array('f')
    samples.frombytes(result.stdout)
    if len(samples) < 10000:
        raise ValueError('Decoded fixture is too short')
    def strength(hz):
        real = sum(value * math.cos(2 * math.pi * hz * i / 48000)
                   for i, value in enumerate(samples))
        imag = sum(value * math.sin(2 * math.pi * hz * i / 48000)
                   for i, value in enumerate(samples))
        return real * real + imag * imag
    return max(choices, key=strength)


def generate(assets, ffmpeg, ffprobe, espeak):
    description = json.loads(DESCRIPTION.read_text())
    assets.mkdir(parents=True, exist_ok=True)
    choices = sorted({item['tone_hz'] for item in description['segments'].values()})
    metadata = {'sample_rate': 48000, 'tone_intervals_hz': {}, 'assets': {}}
    with tempfile.TemporaryDirectory() as temporary:
        for filename, item in description['segments'].items():
            speech = Path(temporary) / (filename + '.wav')
            run([espeak, '-s', '180', '-w', str(speech), item['spoken']],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            path = assets / filename
            sequence = int(re.search(r'\d{3}', filename).group())
            # FFmpeg's MPEG-TS offset starts one AAC packet early for a
            # positive output offset. Compensate to keep adjacent PTS exact.
            offset_seconds = (sequence * 94 + (1 if sequence else 0)) * 1024 / 48000
            run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                 '-f', 'lavfi', '-i', 'sine=frequency=%d:sample_rate=48000:duration=3' % item['tone_hz'],
                 '-i', str(speech), '-filter_complex',
                 '[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.35,aresample=48000,pan=stereo|c0=c0|c1=c0[a]',
                 '-map', '[a]', '-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '128k',
                 '-frames:a', '93', '-f', 'mpegts',
                 '-output_ts_offset', '%.9f' % offset_seconds, str(path)])
            info = probe(path, ffprobe)
            validate_probe(info, path)
            info['section'] = item['section']
            info['expected_tone_hz'] = item['tone_hz']
            info['decoded_tone_hz'] = decoded_tone(path, ffmpeg, choices)
            if info['decoded_tone_hz'] != item['tone_hz']:
                raise ValueError('%s: decoded tone did not match source description' % filename)
            metadata['assets'][filename] = info
            metadata['tone_intervals_hz'][item['section']] = item['tone_hz']
    validate_segment_timeline(metadata['assets'])
    run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
         '-i', 'sine=frequency=330:sample_rate=48000:duration=1', '-c:a', 'aac',
         str(assets / 'unsupported.mp4')])
    (assets / 'metadata.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=Path('tests/generated-audio/ad'))
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--ffprobe', default=shutil.which('ffprobe'))
    parser.add_argument('--espeak', default=shutil.which('espeak'))
    args = parser.parse_args()
    if not all((args.ffmpeg, args.ffprobe, args.espeak)):
        parser.error('FFmpeg, FFprobe and espeak are required to regenerate fixtures')
    generated = generate(args.assets, args.ffmpeg, args.ffprobe, args.espeak)
    print('Generated %d independently decoded AAC segments in %s' %
          (len(generated['assets']), args.assets))


if __name__ == '__main__':
    main()
