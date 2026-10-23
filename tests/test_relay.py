# SPDX-License-Identifier: GPL-2.0-or-later
import concurrent.futures
import hashlib
import http.client
import json
import socket
import struct
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from http_client import HttpClient
from models import Station
from url_policy import UrlPolicy


@pytest.fixture
def origin():
    calls = Counter()
    seen_headers = []
    audio = bytes(range(256)) * 32
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            calls[self.path] += 1
            seen_headers.append(dict(self.headers))
            status, headers, body = 200, {}, audio
            if self.path == '/truncated-list':
                body = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-CUE-OUT:20\n#EXTINF:2,\n/segment\n'
                headers['Content-Type'] = 'application/vnd.apple.mpegurl'
            elif self.path == '/empty-segment':
                body = b''
            elif self.path == '/truncated-segment':
                body = b'part'
            elif self.path == '/reset':
                version = calls[self.path]
                seq = 10 if version <= 3 else 9 if version == 4 else 0
                lead = '#EXTINF:2,\n/segment?reset=9\n' if version == 4 else ''
                body = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%s\n%s#EXT-X-DATERANGE:ID="same",START-DATE="2099-01-01T00:00:00Z",SCTE35-OUT=0xfc00\n#EXTINF:2,\n/segment?reset=%s\n' % (seq, lead, seq)).encode()
                headers['Content-Type'] = 'application/vnd.apple.mpegurl'
            elif self.path.startswith('/list') or self.path.startswith('/variant'):
                seq = calls[self.path]
                body = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%s\n#EXT-X-CUE-OUT:20\n#EXTINF:2,\n/segment?version=%s\n' % (seq, seq)).encode()
                headers['Content-Type'] = 'application/vnd.apple.mpegurl'
            elif self.path == '/master':
                body = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\n/variant-a\n#EXT-X-STREAM-INF:BANDWIDTH=128000\n/variant-b\n'
                headers['Content-Type'] = 'application/vnd.apple.mpegurl'
            elif self.path.startswith('/icy-markers/'):
                titles = {
                    'explicit': ['AD_START_30', 'AD_START_30', 'AD_STOP', 'AD_STOP'],
                    'inferred': ['-', '-', 'Artist - Track', 'Artist - Track'],
                    'tracks': ['Artist - One', 'Artist - Two'],
                }[self.path.rsplit('/', 1)[-1]]
                chunks = []
                for index, title in enumerate(titles):
                    metadata = ("StreamTitle='%s';" % title).encode()
                    metadata += b'\0' * (-len(metadata) % 16)
                    chunks.append(bytes([65 + index]) * 4 + bytes([len(metadata) // 16]) + metadata)
                body = b''.join(chunks)
                headers.update({'Content-Type': 'audio/mpeg', 'icy-metaint': '4'})
            elif self.path == '/icy':
                metadata = b"StreamTitle='token=secret';".ljust(32, b'\0')
                body = b'abcd' + b'\2' + metadata + b'efgh' + b'\0'
                headers.update({'Content-Type': 'audio/mpeg', 'icy-metaint': '4'})
            elif self.path == '/nominated':
                headers.update({'Content-Type': 'audio/mpeg', 'Connection': 'content-type', 'Set-Cookie': 'token=upstream-secret'})
            elif self.path == '/bad-range':
                status = 206
                headers['Content-Range'] = 'bytes 5-2/10'
            elif self.path == '/compressed':
                headers['Content-Encoding'] = 'gzip'
            elif self.path == '/fail':
                status, body = 503, b'no'
            elif self.path == '/hang':
                headers['Content-Type'] = 'audio/mpeg'
                body = b'x' * 512
            elif self.path.startswith('/segment') and self.headers.get('Range'):
                if self.headers['Range'] == 'bytes=2-5':
                    status, body = 206, audio[2:6]
                    headers['Content-Range'] = 'bytes 2-5/%s' % len(audio)
                else:
                    status, body = 416, b''
                    headers['Content-Range'] = 'bytes */%s' % len(audio)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            truncated = self.path in ('/truncated-list', '/empty-segment', '/truncated-segment')
            self.send_header('Content-Length', '100000000' if self.path == '/hang' else str(len(body) + (100 if truncated else 0)))
            if truncated:
                self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
                if self.path == '/hang':
                    time.sleep(2)
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = 'http://127.0.0.1:%s' % server.server_port
    yield base, calls, audio, seen_headers
    server.shutdown()
    server.server_close()


@pytest.fixture
def relay(origin):
    from relay import RelayServer
    from sessions import SessionManager
    class Sink:
        def __init__(self):
            self.events = []
        def emit(self, event):
            self.events.append(event)
    sink = Sink()
    m = SessionManager(HttpClient(UrlPolicy([origin[0]]), read_timeout=.5), sink)
    server = RelayServer(m)
    descriptor = server.start()
    yield server, m, sink, descriptor
    server.close()


def request(url, method='GET', body=None, headers=None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
    conn.request(method, parts.path + ('?' + parts.query if parts.query else ''), body, headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def create(relay, origin, path):
    return relay[1].create(Station('id', 'Radio', origin[0] + path, '', False))


def resource_url(session, body):
    return 'http://127.0.0.1:%s' % urlsplit(session.playback_url).port + next(line for line in body.decode().splitlines() if not line.startswith('#'))


def test_continuous_uses_probe_replays_prefix_and_exact_audio(relay, origin):
    s = create(relay, origin, '/audio')
    status, headers, body = request(s.playback_url)
    assert status == 200 and body == origin[2]
    assert origin[1]['/audio'] == 1
    assert 'Content-Length' not in headers
    assert s.cancel.is_set()


def test_hls_same_fetch_observed_and_segment_hash_and_ranges(relay, origin):
    s = create(relay, origin, '/list')
    status, _, body = request(s.playback_url)
    assert status == 200 and b'MEDIA-SEQUENCE:2' in body
    assert origin[1]['/list'] == 2  # resolver probe plus exactly one relay fetch
    assert relay[2].events[0].details['media_sequence'] == 2
    target = resource_url(s, body)
    assert '127.0.0.1' in target and 'version' not in target
    assert hashlib.sha256(request(target)[2]).digest() == hashlib.sha256(origin[2]).digest()
    status, headers, body = request(target, headers={'Range': 'bytes=2-5'})
    assert status == 206 and body == origin[2][2:6] and headers['Content-Range'].startswith('bytes 2-5/')
    status, headers, body = request(target, headers={'Range': 'bytes=99999-'})
    assert status == 416 and headers['Content-Range'].startswith('bytes */') and body == b''


def test_icy_headers_removed_metadata_event_and_sink_errors_do_not_break_audio(relay, origin):
    s = create(relay, origin, '/icy')
    def emit(event):
        relay[2].events.append(event)
        raise RuntimeError('diagnostics failed')
    relay[2].emit = emit
    status, headers, body = request(s.playback_url)
    assert status == 200 and body == b'abcdefgh'
    assert not any(k.lower() in ('content-length', 'icy-metaint') for k in headers)
    event = relay[2].events[0]
    assert event.kind == 'metadata_observed' and event.session_id == s.id
    assert event.evidence == {'StreamTitle': 'token=secret'}


def test_control_auth_health_forgery_and_switch(relay, origin):
    base = 'http://127.0.0.1:%s' % relay[3].port
    assert request(base + '/health')[0] == 200
    assert relay[3].control_token.encode() not in request(base + '/health')[2]
    payload = json.dumps({'uuid': 'id', 'name': 'Radio', 'url': origin[0] + '/audio', 'codec': '', 'hls': False})
    assert request(base + '/sessions', 'POST', payload)[0] == 401
    auth = {'Authorization': 'Bearer ' + relay[3].control_token}
    untrusted = json.dumps({'uuid': 'id', 'name': 'Radio', 'url': origin[0] + '/list',
                            'codec': '', 'hls': True, 'ads': {'enabled': True}})
    assert request(base + '/sessions', 'POST', untrusted, auth)[0] == 400
    status, _, body = request(base + '/sessions', 'POST', payload, auth)
    assert status == 201
    first = json.loads(body)
    status, _, body = request(base + '/sessions', 'POST', payload, auth)
    assert status == 201 and request(first['playback_url'])[0] == 404
    current = json.loads(body)
    assert request(base + '/media/forged/forged')[0] == 404
    assert request(current['playback_url'] + '?url=http://127.0.0.1/')[0] == 404
    assert request(base + '/sessions/' + current['id'], 'DELETE', headers=auth)[0] == 204
    assert request(current['playback_url'])[0] == 404


def test_hls_rolling_resources_bounded_and_concurrent_renditions(relay, origin):
    relay[1].resource_limit = 6
    s = create(relay, origin, '/master')
    _, _, master = request(s.playback_url)
    urls = ['http://127.0.0.1:%s' % relay[3].port + line for line in master.decode().splitlines() if not line.startswith('#')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, urls))
    assert all(result[0] == 200 for result in results)
    assert len({event.details['rendition_id'] for event in relay[2].events}) == 2
    for _ in range(12):
        assert request(urls[0])[0] == 200
        assert s.resource_count <= 6


def test_upstream_failures_and_transformations_are_not_success(relay, origin):
    with pytest.raises(OSError):
        create(relay, origin, '/fail')
    s = create(relay, origin, '/compressed')
    assert request(s.playback_url)[0] == 502
    s = create(relay, origin, '/list')
    route = s.register_many([origin[0] + '/fail'])[0]
    assert request('http://127.0.0.1:%s' % relay[3].port + route)[0] == 503


def test_cancel_shutdown_ends_blocked_reads_within_deadline(relay, origin):
    s = create(relay, origin, '/hang')
    parts = urlsplit(s.playback_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=2)
    conn.request('GET', parts.path)
    response = conn.getresponse()
    assert response.read(512) == b'x' * 512
    started = time.monotonic()
    relay[0].close()
    assert time.monotonic() - started < 12
    assert s.cancel.is_set() and s.source.response.closed
    conn.close()


def test_head_does_not_consume_probe_and_second_get_fails_clearly(relay, origin):
    s = create(relay, origin, '/audio')
    assert request(s.playback_url, 'HEAD')[0] == 200
    assert not s.source.response.closed
    assert request(s.playback_url)[2] == origin[2]
    assert request(s.playback_url)[0] == 404
    assert origin[1]['/audio'] == 1


def test_partial_control_and_worker_limit_shutdown(relay, origin):
    relay[0].close()
    from relay import RelayServer
    from sessions import SessionManager
    server = RelayServer(SessionManager(), worker_limit=1, client_timeout=.3)
    descriptor = server.start()
    client = socket.create_connection(('127.0.0.1', descriptor.port))
    client.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\n')
    try:
        time.sleep(.05)
        assert request('http://127.0.0.1:%s/health' % descriptor.port)[0] == 503
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 1
    finally:
        client.close()
        server.close()


def test_slow_disconnected_consumer_has_bounded_reads_and_cleanup():
    from relay import RelayServer
    from sessions import SessionManager
    from station_resolver import ResolvedSource
    class Infinite:
        status = 200
        headers = {'content-type': 'audio/mpeg'}
        def __init__(self):
            self.reads, self.closed = 0, False
        def read(self, size):
            self.reads += 1
            return b'a' * size
        def close(self):
            self.closed = True
    upstream = Infinite()
    manager = SessionManager(resolver=lambda *args: ResolvedSource('http://radio.example/', 'continuous', upstream, b'first'))
    server = RelayServer(manager, client_timeout=.15)
    descriptor = server.start()
    session = manager.create(Station('id', 'Radio', 'http://radio.example/', '', False))
    client = socket.socket()
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    client.connect(('127.0.0.1', descriptor.port))
    client.sendall(('GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n' % urlsplit(session.playback_url).path).encode())
    try:
        time.sleep(.4)
        assert upstream.reads < 200  # kernel buffers plus one 64 KiB application chunk
        assert upstream.closed and session.cancel.is_set()
    finally:
        client.close()
        server.close()


def test_abrupt_disconnect_closes_continuous_origin(relay, origin):
    s = create(relay, origin, '/hang')
    client = socket.create_connection(('127.0.0.1', relay[3].port))
    client.sendall(('GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n' % urlsplit(s.playback_url).path).encode())
    assert client.recv(4096)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    client.close()
    deadline = time.monotonic() + 2
    # Transport closure can precede handler unwinding and session cancellation.
    while not (s.source.response.closed and s.cancel.is_set()) and time.monotonic() < deadline:
        time.sleep(.02)
    assert s.source.response.closed and s.cancel.is_set()


def test_control_client_and_credential_headers_not_forwarded(relay, origin):
    from control import ControlClient
    control = ControlClient(relay[3])
    assert control.health()['status'] == 'ok'
    result = control.create(Station('id', 'Radio', origin[0] + '/list', '', True))
    s = relay[1].get(result['id'])
    _, _, body = request(result['playback_url'])
    target = resource_url(s, body)
    assert request(target, headers={'Authorization': 'Bearer local-secret', 'Cookie': 'token=local-secret'})[0] == 200
    assert 'Authorization' not in origin[3][-1] and 'Cookie' not in origin[3][-1]
    control.stop(s.id)
    assert request(result['playback_url'])[0] == 404


def test_hop_headers_and_invalid_range_never_forwarded_as_success(relay, origin):
    s = create(relay, origin, '/nominated')
    status, headers, body = request(s.playback_url)
    assert status == 200 and body == origin[2]
    assert 'Content-Type' not in headers and 'Set-Cookie' not in headers
    s = create(relay, origin, '/list')
    route = s.register_many([origin[0] + '/bad-range'])[0]
    assert request('http://127.0.0.1:%s' % relay[3].port + route, headers={'Range': 'bytes=2-5'})[0] == 502


def test_playlist_reset_reuses_cue_id_in_new_epoch_but_overlap_does_not(relay, origin):
    s = create(relay, origin, '/reset')
    assert request(s.playback_url)[0] == 200
    assert request(s.playback_url)[0] == 200  # identical window
    assert request(s.playback_url)[0] == 200  # lower first sequence with stable overlap
    starts = [e for e in relay[2].events if e.kind == 'break_start']
    assert len(starts) == 1 and starts[0].details['epoch'] == 0
    assert request(s.playback_url)[0] == 200  # clear non-overlapping reset to zero
    starts = [e for e in relay[2].events if e.kind == 'break_start']
    assert [e.details['epoch'] for e in starts] == [0, 1]
    assert any(e.kind == 'break_unresolved' and e.details['reason'] == 'epoch_changed' for e in relay[2].events)


@pytest.mark.parametrize('path,kind', [('/empty-segment', 'segment'), ('/truncated-list', 'playlist')])
def test_premature_upstream_eof_before_headers_is_502(relay, origin, path, kind):
    s = create(relay, origin, '/list')
    route = s.register_many([origin[0] + path], [kind])[0]
    count = s.resource_count
    status, _, body = request('http://127.0.0.1:%s' % relay[3].port + route)
    assert status == 502 and body == b''
    assert s.resource_count == count
    assert [e.kind for e in relay[2].events] == ['stream_error']
    assert relay[2].events[0].details['reason'] == 'incomplete_upstream_body'


def test_premature_segment_eof_after_headers_terminates_and_observes_error(relay, origin):
    s = create(relay, origin, '/list')
    route = s.register_many([origin[0] + '/truncated-segment'], ['segment'])[0]
    conn = http.client.HTTPConnection('127.0.0.1', relay[3].port, timeout=2)
    try:
        conn.request('GET', route)
        response = conn.getresponse()
        assert response.status == 200  # status was already delivered with first bytes
        with pytest.raises(http.client.IncompleteRead) as error:
            response.read()
        assert error.value.partial == b'part'
        assert relay[2].events[-1].kind == 'stream_error'
        assert relay[2].events[-1].details['reason'] == 'incomplete_upstream_body'
    finally:
        conn.close()


@pytest.mark.parametrize('path,expected', [('/audio', 'audio'), ('/icy', 'icy'), ('/list', 'hls')])
def test_initial_full_body_range_probe_is_accepted(relay, origin, path, expected):
    s = create(relay, origin, path)
    status, headers, body = request(s.playback_url, headers={'Range': 'bytes=0-'})
    assert status == 200
    assert 'Content-Range' not in headers
    if expected == 'hls':
        assert body.startswith(b'#EXTM3U')
        assert request(resource_url(s, body), headers={'Range': 'bytes=2-5'})[0] == 206
        assert 'Range' not in origin[3][-2]  # manifest probe was not forwarded
    else:
        assert body == (b'abcdefgh' if expected == 'icy' else origin[2])
        assert origin[1][path] == 1  # the retained probe remains the sole upstream


@pytest.mark.parametrize('path', ['/audio', '/list'])
@pytest.mark.parametrize('value', ['bytes=1-', 'bytes=0-10', 'bytes=-10', 'invalid'])
def test_nonseekable_resources_reject_other_ranges(relay, origin, path, value):
    s = create(relay, origin, path)
    assert request(s.playback_url, headers={'Range': value})[0] == 416


def test_safe_suffix_routes_are_exact_and_still_relay_ranges(relay, origin):
    s = create(relay, origin, '/list')
    assert s.playback_url.endswith('.m3u8')
    base = 'http://127.0.0.1:%s' % relay[3].port
    for extension in ('ts', 'aac', 'mp3', 'mp4', 'm4s', 'm4a', 'ac3', 'ec3'):
        route = s.register_many([origin[0] + '/segment.' + extension + '?token=secret'], ['segment'])[0]
        assert route.endswith('.' + extension)
        assert 'token' not in route and 'segment' not in route
        assert request(base + route, headers={'Range': 'bytes=2-5'})[0] == 206
        assert request(base + route.rsplit('.', 1)[0] + '.exe')[0] == 404
        assert request(base + route.rsplit('.', 1)[0])[0] == 404
    unknown = s.register_many([origin[0] + '/segment.untrusted'], ['segment'])[0]
    assert '.' not in unknown.rsplit('/', 1)[-1]
    assert request(base + unknown + '.ts')[0] == 404
    assert request(base + unknown)[0] == 200


def test_extensionless_path_named_like_codec_does_not_gain_suffix(relay, origin):
    s = create(relay, origin, '/list')
    route = s.register_many([origin[0] + '/ts'], ['segment'])[0]
    assert '.' not in route.rsplit('/', 1)[-1]


@pytest.mark.parametrize('path,content_type', [('/audio', None), ('/icy', 'audio/mpeg'),
    ('/list', 'application/vnd.apple.mpegurl')])
def test_head_metadata_does_not_claim_fetch_or_observe_before_playback(relay,origin,path,content_type):
    s = create(relay, origin, path)
    calls = origin[1].copy()
    for headers in ({}, {'Range': 'bytes=1-'}):
        status, returned, body = request(s.playback_url, 'HEAD', headers=headers)
        assert status == 200 and body == b''
        assert returned.get('Content-Type') == content_type
        assert 'Content-Length' not in returned and 'icy-metaint' not in returned
        assert origin[1] == calls and not relay[2].events
        assert not s.cancel.is_set() and not s.active
        if s.source.response is not None:
            assert not s.source.response.closed
    status, _, body = request(s.playback_url)
    assert status == 200
    if path == '/list':
        assert body.startswith(b'#EXTM3U')
    else:
        assert body == (b'abcdefgh' if path == '/icy' else origin[2])
        assert origin[1] == calls


def test_head_validates_exact_resources_without_fetching_segments(relay,origin):
    s = create(relay, origin, '/list')
    base = 'http://127.0.0.1:%s' % relay[3].port
    variant = s.register_many([origin[0] + '/variant-a'], ['playlist'])[0]
    segment = s.register_many([origin[0] + '/segment.ts'], ['segment'])[0]
    calls = origin[1].copy()
    assert request(base + variant, 'HEAD')[0] == 200
    assert request(base + segment, 'HEAD')[0] == 405
    for invalid in (s.playback_url + '?url=hidden', s.playback_url[:-5],
                    base + segment[:-3], base + '/media/' + 'a'*48 + '/' + 'b'*48):
        assert request(invalid, 'HEAD')[0] == 404
    assert request(base + '/sessions', 'HEAD')[0] == 405
    assert origin[1] == calls and not relay[2].events and not s.cancel.is_set()
    relay[1].stop(s.id)
    assert request(s.playback_url, 'HEAD')[0] == 404


@pytest.mark.parametrize('path,hint,expected_mime', [
    ('/icy', True, 'audio/mpeg'),
    ('/audio', True, 'application/octet-stream'),
    ('/list', False, 'application/vnd.apple.mpegurl'),
])
def test_control_exposes_resolved_mime_without_consuming_media(relay, origin, path, hint, expected_mime):
    from control import ControlClient
    control = ControlClient(relay[3])
    result = control.create(Station('mime', 'Radio', origin[0] + path, 'WRONG', hint))
    assert result['mime_type'] == expected_mime
    assert request(result['playback_url'])[0] == 200


def test_loopback_relay_start_does_not_resolve_hostname(monkeypatch):
    from relay import RelayServer

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError('Loopback relay startup must not wait for DNS')

    monkeypatch.setattr(socket, 'getfqdn', unexpected_lookup)
    server = RelayServer()
    try:
        descriptor = server.start()
        assert server._server.server_name == '127.0.0.1'
        assert server._server.server_port == descriptor.port
        assert server._server.socket.getsockname() == ('127.0.0.1', descriptor.port)
        assert request('http://127.0.0.1:%s/health' % descriptor.port)[0] == 200
    finally:
        server.close()


@pytest.mark.parametrize('scenario,titles,audio,boundaries', [
    ('explicit', ['AD_START_30', 'AD_START_30', 'AD_STOP', 'AD_STOP'],
     b'AAAABBBBCCCCDDDD', [('break_start', 'explicit'), ('break_end', 'explicit')]),
    ('inferred', ['-', '-', 'Artist - Track', 'Artist - Track'],
     b'AAAABBBBCCCCDDDD', [('break_start', 'inferred'), ('break_end', 'inferred')]),
    ('tracks', ['Artist - One', 'Artist - Two'], b'AAAABBBB', []),
])
def test_icy_marker_observations_keep_metadata_and_exact_audio(
        relay, origin, scenario, titles, audio, boundaries):
    session = create(relay, origin, '/icy-markers/' + scenario)
    status, headers, body = request(session.playback_url)
    assert status == 200 and body == audio
    assert not any(key.lower() == 'icy-metaint' for key in headers)
    events = relay[2].events
    metadata = [event for event in events if event.kind == 'metadata_observed']
    assert [event.evidence['StreamTitle'] for event in metadata] == titles
    assert all(event.source == 'icy' for event in metadata)
    markers = [event for event in events if event.source == 'icy_marker']
    assert [(event.kind, event.details['classification']) for event in markers
            if event.kind.startswith('break_')] == boundaries
    assert all(event.session_id == session.id and event.station_uuid == 'id'
               and event.protocol == 'icy' for event in markers)
    assert session.ad is None and not session.ad_enabled
    candidates = [event for event in markers if event.kind == 'icy_marker_candidate']
    assert [event.evidence['StreamTitle'] for event in candidates] == (
        ['-'] if scenario == 'inferred' else [])
    if scenario == 'explicit':
        assert markers[0].to_dict()['details'] == {
            'classification': 'explicit', 'marker_format': 'ad_command',
            'confidence': 'explicit', 'duration': 30}


def observe_marker(session, marker):
    session.observe_icy_metadata({'StreamTitle': marker}, '2026-10-23T00:00:00Z', time.monotonic())


def test_icy_control_requires_auth_rejects_origins_and_caps_body(relay, origin, tmp_path):
    from icy_marker_store import IcyMarkerStore
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    session = create(relay, origin, '/icy')
    observe_marker(session, '-')
    base = 'http://127.0.0.1:%d' % relay[3].port
    auth = {'Authorization': 'Bearer ' + relay[3].control_token}
    payload = json.dumps({'session_id': session.id, 'marker': '-', 'action': 'end'})
    for headers in ({}, {'Authorization': 'Bearer wrong'}, dict(auth, Origin='https://evil.example'),
                    dict(auth, Origin='')):
        assert request(base + '/icy-marker/candidate', headers=headers)[0] == 401
        assert request(base + '/icy-marker/learn', 'POST', payload, headers)[0] == 401
    assert request(base + '/icy-marker/learn', 'POST', ' ' * 4097, auth)[0] == 400
    for data in ('[]', '{}', '{', json.dumps({'session_id': session.id, 'marker': '-', 'action': 'bad'}),
                 json.dumps({'session_id': session.id, 'marker': '-', 'action': 'end', 'station_uuid': 'other'})):
        assert request(base + '/icy-marker/learn', 'POST', data, auth)[0] == 400
    assert relay[1].icy_marker_store.lookup('id', '-') is None


def test_icy_client_learning_is_exact_station_scoped_and_overrides_next_metadata(relay, origin, tmp_path):
    from control import ControlClient
    from icy_marker_store import IcyMarkerStore
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    client = ControlClient(relay[3])
    assert client.current_icy_candidate() is None
    session = create(relay, origin, '/icy')
    observe_marker(session, '-')
    assert client.current_icy_candidate() == {'session_id': session.id, 'station_uuid': 'id', 'marker': '-'}
    with pytest.raises(OSError):
        client.learn_icy_marker(session.id, ' - ', 'end')
    client.learn_icy_marker(session.id, '-', 'end')
    assert IcyMarkerStore(tmp_path).lookup('id', '-') == 'end'
    observe_marker(session, '-')  # same title, newly learned meaning must take precedence
    assert [(e.kind, e.details['classification']) for e in relay[2].events
            if e.source == 'icy_marker' and e.kind == 'break_end'] == [('break_end', 'learned')]
    other = relay[1].create(Station('other', 'Other', origin[0] + '/icy', '', False))
    observe_marker(other, '-')
    assert relay[2].events[-1].details['classification'] == 'inferred'
    restored = relay[1].create(Station('id', 'Radio', origin[0] + '/icy', '', False))
    observe_marker(restored, '-')
    assert relay[2].events[-1].details['classification'] == 'learned'
    assert restored.ad is None


def test_icy_learning_rejects_stale_candidate_session_and_stop(relay, origin, tmp_path):
    from control import ControlClient
    from icy_marker_store import IcyMarkerStore
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    client = ControlClient(relay[3])
    session = create(relay, origin, '/icy')
    observe_marker(session, '-')
    observe_marker(session, '#')
    with pytest.raises(OSError):
        client.learn_icy_marker(session.id, '-', 'end')
    replacement = relay[1].create(Station('other', 'Other', origin[0] + '/icy', '', False))
    observe_marker(replacement, '#')
    with pytest.raises(OSError):
        client.learn_icy_marker(session.id, '#', 'end')
    relay[1].stop(replacement.id)
    assert client.current_icy_candidate() is None
    with pytest.raises(OSError):
        client.learn_icy_marker(replacement.id, '#', 'end')
    assert not (tmp_path / 'icy-markers.json').exists()


def test_icy_control_does_not_expose_unsafe_or_oversized_candidate(relay, origin):
    from control import ControlClient
    client = ControlClient(relay[3])
    session = create(relay, origin, '/icy')
    for candidate in ('token=secret', 'https://radio.example/', '\n-', 'é' * 129):
        # Exercise the trust boundary even if future parser variants nominate unsafe data.
        with session._lock:
            session.icy_tracker._candidate = (time.monotonic(), candidate)
        assert client.current_icy_candidate() is None


def test_icy_learning_body_boundary_and_invalid_framing(relay, origin, tmp_path):
    from icy_marker_store import IcyMarkerStore
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    session = create(relay, origin, '/icy')
    observe_marker(session, '-')
    path = '/icy-marker/learn'
    auth = {'Authorization': 'Bearer ' + relay[3].control_token}
    base = 'http://127.0.0.1:%d' % relay[3].port
    payload = json.dumps({'session_id': session.id, 'marker': '-', 'action': 'end'})
    assert request(base + path, 'POST', payload.ljust(4096), auth)[0] == 204
    for extra in ('Content-Length: 2\r\nContent-Length: 2\r\n',
                  'Content-Length: -1\r\n', 'Content-Length: invalid\r\n', '',
                  'Transfer-Encoding: chunked\r\n'):
        client = socket.create_connection(('127.0.0.1', relay[3].port))
        client.settimeout(2)
        try:
            client.sendall(('POST %s HTTP/1.1\r\nHost: localhost\r\nAuthorization: %s\r\n%s\r\n{}' % (
                path, auth['Authorization'], extra)).encode())
            response = http.client.HTTPResponse(client)
            response.begin()
            assert response.status == (401 if 'Transfer-Encoding' in extra else 400)
            response.read()
        finally:
            client.close()


def test_icy_candidate_expires_and_disk_failure_does_not_apply_label(relay, origin, tmp_path, monkeypatch):
    from control import ControlClient
    from icy_marker_store import IcyMarkerStore
    import os
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    session = create(relay, origin, '/icy')
    client = ControlClient(relay[3])
    observe_marker(session, '-')
    def fail(*args):
        raise OSError('private filesystem failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        client.learn_icy_marker(session.id, '-', 'end')
    assert relay[1].icy_marker_store.lookup('id', '-') is None
    with session._lock:
        session.icy_tracker._candidate = (time.monotonic() - 301, '-')
    assert client.current_icy_candidate() is None
    with pytest.raises(OSError):
        client.learn_icy_marker(session.id, '-', 'end')


def test_icy_learning_rejects_truncated_json_body_even_when_json_is_complete(relay, origin, tmp_path):
    from icy_marker_store import IcyMarkerStore
    relay[1].icy_marker_store = IcyMarkerStore(tmp_path)
    session = create(relay, origin, '/icy')
    observe_marker(session, '-')
    payload = json.dumps({'session_id': session.id, 'marker': '-', 'action': 'end'}).encode()
    client = socket.create_connection(('127.0.0.1', relay[3].port))
    client.settimeout(2)
    try:
        headers = ('POST /icy-marker/learn HTTP/1.1\r\nHost: localhost\r\n'
                   'Authorization: Bearer %s\r\nContent-Length: %d\r\n\r\n' % (
                       relay[3].control_token, len(payload) + 10))
        client.sendall(headers.encode() + payload)
        client.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 400
        assert relay[1].icy_marker_store.lookup('id', '-') is None
    finally:
        client.close()
