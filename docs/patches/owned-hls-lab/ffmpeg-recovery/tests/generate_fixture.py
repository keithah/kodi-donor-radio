#!/usr/bin/env python3
"""Create the fixed four-second stereo AAC/HLS recovery fixture."""
import argparse
from pathlib import Path
import subprocess
parser = argparse.ArgumentParser()
parser.add_argument('--ffmpeg', default='/opt/homebrew/bin/ffmpeg')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
(root / 'artifacts').mkdir(parents=True, exist_ok=True)
command = [args.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
           'aevalsrc=0.08*sin(2*PI*(370*t+13*t*t))|0.07*sin(2*PI*(710*t+19*t*t)):s=48000:d=4',
           '-c:a', 'aac', '-b:a', '128k', '-f', 'hls', '-hls_time', '1',
           '-hls_playlist_type', 'vod', '-hls_segment_filename',
           str(root/'artifacts/segment-%02d.ts'), str(root/'artifacts/reference.m3u8')]
subprocess.run(command, check=True)
