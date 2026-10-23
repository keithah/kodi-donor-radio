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
ICY_METAINT = 4096
ICY_SCENARIOS = ('explicit', 'tildeats', 'wrapped', 'return-to-track',
                 'paired-marker', 'punctuation', 'unresolved')
ICY_TITLES = {
    'explicit': ('AD_START_30', 'AD_STOP'),
    'tildeats': ('Station ~ Track ~ 30 ~ 4 ~ I ~ 30 ^',
                 'Station ~ Track ~ 30 ~ 0 ^',
                 'Station ~ Track ~ 30 ~ 4 ^', '##Station ~ Track ~ 30 ~ 4 ^'),
    'wrapped': ('#AD_START#', '#AD_END#'),
    'return-to-track': ('-', 'Artist - Track'),
    'paired-marker': ('#', '*', '#'),
    'punctuation': ('-', '#', '*', 'Artist - Track'),
    'unresolved': ('#',),
}
ICY_EXPECTED = {
    'explicit': [('break_start', 'explicit'), ('break_end', 'explicit')],
    'tildeats': [('break_start', 'explicit'), ('break_end', 'explicit'),
                 ('break_start', 'explicit'), ('break_end', 'explicit')],
    'wrapped': [('break_start', 'explicit'), ('break_end', 'explicit')],
    'return-to-track': [('icy_marker_candidate', 'candidate'),
                        ('break_start', 'inferred'), ('break_end', 'inferred')],
    'paired-marker': [('icy_marker_candidate', 'candidate'),
                      ('break_start', 'inferred'),
                      ('icy_marker_candidate', 'candidate'),
                      ('icy_marker_candidate', 'candidate'),
                      ('break_end', 'inferred')],
    'punctuation': [('icy_marker_candidate', 'candidate'),
                    ('break_start', 'inferred'),
                    ('icy_marker_candidate', 'candidate'),
                    ('icy_marker_candidate', 'candidate'),
                    ('break_end', 'inferred')],
    'unresolved': [('icy_marker_candidate', 'candidate'),
                   ('break_start', 'inferred'), ('break_unresolved', 'inferred')],
}


def icy_metadata(scenario):
    if scenario not in ICY_SCENARIOS:
        raise ValueError('Unknown ICY fixture scenario')
    return ICY_TITLES[scenario]


def icy_observation_times(scenario):
    return tuple(index * 0.25 for index, _ in enumerate(icy_metadata(scenario)))


def icy_audio_bytes(scenario, source=None):
    """Repeat valid generated MP3 frame bytes to fill every ICY audio interval."""
    count = len(icy_metadata(scenario)) * ICY_METAINT
    if source is None:
        # Small deterministic unit-test payload; the HTTP endpoint requires tone.mp3.
        source = b''.join(bytes([65 + index]) * ICY_METAINT
                          for index, _ in enumerate(icy_metadata(scenario)))
    source = bytes(source)
    if not source:
        raise ValueError('ICY fixture audio source cannot be empty')
    return (source * ((count + len(source) - 1) // len(source)))[:count]


def icy_stream_frames(scenario, audio=None):
    """Return complete audio-interval/metadata frames for a controlled ICY stream."""
    titles = icy_metadata(scenario)
    audio = icy_audio_bytes(scenario) if audio is None else audio
    if len(audio) != len(titles) * ICY_METAINT:
        raise ValueError('ICY fixture audio must contain one interval per title')
    frames = []
    for index, title in enumerate(titles):
        metadata = ("StreamTitle='%s';" % title).encode('utf-8')
        padded = metadata.ljust(((len(metadata) + 15) // 16) * 16, b'\0')
        interval = audio[index * ICY_METAINT:(index + 1) * ICY_METAINT]
        frames.append(interval + bytes([len(padded) // 16]) + padded)
    return tuple(frames)


def icy_stream_bytes(scenario, audio=None):
    """Frame a finite controlled ICY stream; only metadata framing is added."""
    return b''.join(icy_stream_frames(scenario, audio))


def mp3_frame_payload(path):
    """Drop file-level ID3 tags so repeating generated MP3 frames stays streamable."""
    data = Path(path).read_bytes()
    if data.startswith(b'ID3') and len(data) >= 10:
        size_bytes = data[6:10]
        if any(value & 0x80 for value in size_bytes):
            raise ValueError('Invalid ID3v2 size in fixture MP3')
        size = ((size_bytes[0] << 21) | (size_bytes[1] << 14)
                | (size_bytes[2] << 7) | size_bytes[3])
        footer = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer:]
    if len(data) >= 128 and data[-128:-125] == b'TAG':
        data = data[:-128]
    if not data:
        raise ValueError('Generated fixture MP3 has no frame data')
    return data


def expected_icy(scenario):
    if scenario not in ICY_SCENARIOS:
        raise ValueError('Unknown ICY fixture scenario')
    return ICY_EXPECTED[scenario]


def send_fixture_response(handler, body, mime, status=200, ranges=False):
    """Send a finite fixture response; shared with the controlled ad origin."""
    headers = {}
    if ranges and handler.headers.get('Range'):
        match = re.fullmatch(r'bytes=([0-9]+)-([0-9]*)', handler.headers['Range'])
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
    handler.send_response(status)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


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
            match = re.fullmatch(r'/expected/icy/([^/]+)\.json', self.path)
            if match and match.group(1) in ICY_SCENARIOS:
                rows = [{'kind': kind, 'classification': classification}
                        for kind, classification in expected_icy(match.group(1))]
                return self.respond(json.dumps(rows).encode(), 'application/json')
            match = re.fullmatch(r'/icy/([^/]+)\.mp3', self.path)
            if match and match.group(1) in ICY_SCENARIOS:
                scenario = match.group(1)
                tone_path = assets / 'tone.mp3'
                if not tone_path.is_file():
                    return self.respond(b'Generate audio assets first\n', 'text/plain', 404)
                audio = icy_audio_bytes(scenario, mp3_frame_payload(tone_path))
                frames = icy_stream_frames(scenario, audio)
                content_length = sum(len(frame) for frame in frames)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('icy-metaint', str(ICY_METAINT))
                self.send_header('Content-Length', str(content_length))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    # Pace complete metadata frames; block sizes vary with the title.
                    for index, frame in enumerate(frames):
                        self.wfile.write(frame)
                        self.wfile.flush()
                        if index + 1 < len(frames):
                            self.server.stopping.wait(.25)
                except OSError:
                    pass
                return
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
            send_fixture_response(self, body, mime, status, ranges)

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
