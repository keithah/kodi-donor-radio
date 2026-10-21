#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Developer-only playable origin; generated assets never belong in the addon.

URLs: /continuous.mp3, /continuous.aac, /icy.mp3,
/hls/{scenario}/index.m3u8, /expected/{scenario}.json, /stats.
Each HLS scenario advances every four seconds from its first manifest request.
Restart the server to reset clocks and connection counters.
"""
import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time


SCENARIOS = ('normal', 'mid-break', 'missing-end', 'malformed-cue', 'no-cue')


def manifest(scenario, version):
    if scenario not in SCENARIOS or version not in (0, 1, 2):
        raise ValueError('Unknown fixture schedule')
    sequence = version * 2
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:2',
             '#EXT-X-MEDIA-SEQUENCE:%s' % sequence]
    if version == 0:
        cue = {'normal': '#EXT-X-CUE-OUT:10',
               'mid-break': '#EXT-X-CUE-OUT-CONT:4/20',
               'missing-end': '#EXT-X-CUE-OUT:6',
               'malformed-cue': '#EXT-X-CUE-OUT:bogus'}.get(scenario)
        if cue:
            lines.append(cue)
    if version == 2 and scenario in ('normal', 'mid-break'):
        lines.append('#EXT-X-CUE-IN')
    for number in range(sequence, sequence + 2):
        lines.extend(('#EXTINF:2.0,', '/assets/segment%03d.ts' % number))
    if version == 2:
        lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines) + '\n'


def expected(scenario):
    kinds = {
        'normal': [('cue_observed', 'signaled'), ('break_start', 'signaled'),
                   ('cue_observed', 'signaled'), ('break_end', 'signaled')],
        'mid-break': [('cue_observed', 'in_progress'), ('cue_observed', 'signaled'),
                      ('break_end', 'signaled')],
        'missing-end': [('cue_observed', 'signaled'), ('break_start', 'signaled'),
                        ('break_unresolved', 'signaled')],
        'malformed-cue': [('cue_observed', 'invalid')],
        'no-cue': [],
    }
    return [{'kind': kind, 'classification': classification} for kind, classification in kinds[scenario]]


def make_server(assets, host='127.0.0.1', port=8765):
    assets = Path(assets)
    counters, starts, lock = Counter(), {}, threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            with lock:
                counters[self.path] += 1
            if self.path == '/stats':
                with lock:
                    body = json.dumps(dict(counters)).encode()
                return self.respond(body, 'application/json')
            match = re.fullmatch('/expected/([^/]+).json', self.path)
            if match and match.group(1) in SCENARIOS:
                return self.respond(json.dumps(expected(match.group(1))).encode(), 'application/json')
            match = re.fullmatch('/hls/([^/]+)/index.m3u8', self.path)
            if match and match.group(1) in SCENARIOS:
                scenario = match.group(1)
                with lock:
                    start = starts.setdefault(scenario, time.monotonic())
                    version = min(2, int((time.monotonic() - start) / 4))
                return self.respond(manifest(scenario, version).encode(), 'application/vnd.apple.mpegurl')
            if self.path in ('/continuous.mp3', '/continuous.aac', '/icy.mp3'):
                filename = 'tone.aac' if self.path.endswith('.aac') else 'tone.mp3'
                path = assets / filename
                if not path.is_file():
                    return self.respond(b'Generate audio assets first\n', 'text/plain', 404)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/aac' if filename.endswith('.aac') else 'audio/mpeg')
                if self.path == '/icy.mp3':
                    self.send_header('icy-metaint', '4096')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    with path.open('rb') as source:
                        while not self.server.stopping.is_set():
                            block = source.read(4096)
                            if not block:
                                source.seek(0)
                                continue
                            # Complete each ICY interval across file boundaries.
                            if self.path == '/icy.mp3':
                                while len(block) < 4096:
                                    source.seek(0)
                                    block += source.read(4096 - len(block))
                                title = b"StreamTitle='Synthetic fixture tone';"
                                padded = title.ljust(((len(title) + 15) // 16) * 16, b'\0')
                                block += bytes([len(padded) // 16]) + padded
                            self.wfile.write(block)
                            self.wfile.flush()
                            self.server.stopping.wait(.25)
                except OSError:
                    pass
                return
            match = re.fullmatch(r'/assets/(segment[0-9]{3}\.ts)', self.path)
            if match:
                path = assets / match.group(1)
                if path.is_file():
                    return self.respond(path.read_bytes(), 'video/mp2t', ranges=True)
            self.respond(b'', 'text/plain', 404)

        def respond(self, body, mime, status=200, ranges=False):
            headers = {}
            if ranges and self.headers.get('Range'):
                match = re.fullmatch(r'bytes=([0-9]+)-([0-9]*)', self.headers['Range'])
                if match:
                    start = int(match.group(1))
                    end = min(int(match.group(2)) if match.group(2) else len(body) - 1, len(body) - 1)
                    if start <= end:
                        headers['Content-Range'] = 'bytes %s-%s/%s' % (start, end, len(body))
                        body, status = body[start:end + 1], 206
                    else:
                        headers['Content-Range'] = 'bytes */%s' % len(body)
                        body, status = b'', 416
                else:
                    headers['Content-Range'] = 'bytes */%s' % len(body)
                    body, status = b'', 416
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.stopping = threading.Event()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--assets', type=Path, default=Path('tests/generated-audio'), help='Generated fixture assets directory')
    parser.add_argument('--host', default='127.0.0.1', help='Bind address; use an explicitly reachable public test host for addon playback')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.assets, args.host, args.port)
    print('Fixture origin: http://%s:%s; scenarios: %s' % (args.host, server.server_port, ', '.join(SCENARIOS)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()


if __name__ == '__main__':
    main()
