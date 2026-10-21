#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Decode synthetic fixtures through the real relay; this is not a Kodi test."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin.audio.kodi_donor_radio/resources/lib'))

from fixture_server import make_server
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from url_policy import UrlPolicy


def smoke(ffmpeg, assets):
    origin = make_server(assets, port=0)
    worker = threading.Thread(target=origin.serve_forever, daemon=True)
    worker.start()
    base = 'http://127.0.0.1:%s' % origin.server_port
    relay = RelayServer(SessionManager(HttpClient(UrlPolicy([base]))))
    relay.start()
    results = []
    try:
        for path in ('/continuous.mp3', '/icy.mp3', '/hls/normal/index.m3u8'):
            session = relay.manager.create(Station('synthetic', 'Fixture', base + path, '', False))
            try:
                run = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error',
                                      '-i', session.playback_url, '-t', '2', '-map', '0:a:0',
                                      '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                # Output is 2s * 8000 samples/s * 2 bytes/sample; require decoded sound.
                results.append({'fixture': path, 'exit': run.returncode,
                                'pcm_bytes': len(run.stdout),
                                'nonzero_audio': any(run.stdout),
                                'stderr': run.stderr.decode('utf-8', 'replace').replace(session.playback_url, '[relay]')})
            except subprocess.TimeoutExpired:
                results.append({'fixture': path, 'exit': 'timeout', 'pcm_bytes': 0, 'nonzero_audio': False})
            finally:
                relay.manager.stop(session.id)
    finally:
        relay.close()
        origin.stopping.set()
        origin.shutdown()
        origin.server_close()
        worker.join(2)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--assets', type=Path, default=ROOT / 'tests/generated-audio')
    args = parser.parse_args()
    results = smoke(args.ffmpeg, args.assets)
    print(json.dumps(results, indent=2))
    return 0 if all(r['exit'] == 0 and r['pcm_bytes'] == 32000 and r['nonzero_audio'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
