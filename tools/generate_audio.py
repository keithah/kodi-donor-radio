#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Generate project-authored 440 Hz tone MP3, AAC, and six HLS segments.

Run: python3 tools/generate_audio.py --ffmpeg /opt/homebrew/bin/ffmpeg
Assets default to tests/generated-audio, outside the distributable addon.
FFmpeg is a development tool only; Kodi never invokes it.
"""
import argparse
from pathlib import Path
import shutil
import subprocess


def generate(assets, ffmpeg):
    assets.mkdir(parents=True, exist_ok=True)
    common = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
              '-i', 'sine=frequency=440:sample_rate=48000:duration=12', '-ac', '2']
    subprocess.run(common + ['-c:a', 'libmp3lame', '-b:a', '128k', str(assets / 'tone.mp3')], check=True)
    subprocess.run(common + ['-c:a', 'aac', '-b:a', '128k', '-f', 'adts', str(assets / 'tone.aac')], check=True)
    subprocess.run(common + ['-c:a', 'aac', '-b:a', '128k', '-f', 'hls', '-hls_time', '2',
                            '-hls_list_size', '0', '-hls_segment_filename', str(assets / 'segment%03d.ts'),
                            str(assets / 'tone.m3u8')], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--assets', type=Path, default=Path('tests/generated-audio'))
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'), help='Developer FFmpeg executable path')
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error('FFmpeg is required to regenerate developer fixtures')
    generate(args.assets, args.ffmpeg)
    print('Generated playable assets in %s' % args.assets)


if __name__ == '__main__':
    main()
