# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional Adaptive transport uses only a validated controlled composition."""
from contextlib import contextmanager
import http.client
import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest

from ad_models import AdConfig
from control import ControlClient, ServiceDescriptor
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import ResourceLimit, SessionManager
from tools.ad_fixture_server import make_server
from url_policy import UrlPolicy


def get(url, method='GET'):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
    try:
        connection.request(method, parts.path)
        reply = connection.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        connection.close()


@contextmanager
def controlled_relay(assets, cache, resource_limit=2048, enabled=True):
    fixture = make_server(assets, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             resource_limit=resource_limit,
                             ad_config=AdConfig(enabled, True, False,
                                                origin + '/vast/preroll', origin),
                             cache_root=cache)
    relay = RelayServer(manager)
    relay.start()
    try:
        yield relay, origin
    finally:
        relay.close()
        fixture.shutdown()
        fixture.server_close()
        thread.join(2)


def fixture_station(origin, uuid='fixture-adaptive'):
    return Station(uuid, 'Controlled audio', origin + '/source/normal/index.m3u8', '', True)


def test_audio_master_validates_composition_and_preserves_native_url(ad_assets, tmp_path):
    with controlled_relay(ad_assets, tmp_path, resource_limit=10) as (relay, origin):
        result = ControlClient(relay.descriptor).create(fixture_station(origin))
        master_url = result.get('adaptive_playback_url')
        assert master_url, 'Controlled composition must offer its optional Adaptive URL'
        assert master_url != result['playback_url']
        session = relay.manager.get(result['id'])
        assert session.resource_count == 2
        assert get(master_url, 'HEAD')[0] == 200
        assert not session.ad.committed, 'HEAD must not fetch, observe or compose audio'

        status, headers, body = get(master_url)
        assert status == 200
        assert headers['Content-Type'] == 'application/vnd.apple.mpegurl'
        text = body.decode()
        assert 'CODECS="mp4a.40.2"' in text
        child = [line for line in text.splitlines() if line and not line.startswith('#')]
        assert child == [result['playback_url']]
        assert session.ad.committed
        assert session.resource_count == 10
        status, _, manifest = get(result['playback_url'])
        assert status == 200 and b'#EXT-X-STREAM-INF' not in manifest
        routes = [line for line in manifest.decode().splitlines() if line and not line.startswith('#')]
        first_url = 'http://127.0.0.1:%d' % relay.descriptor.port + routes[0]
        assert get(first_url)[2] == (ad_assets / 'preroll000.ts').read_bytes()
        assert get(master_url)[2] == body
        assert session.resource_count <= 10


def test_unsupported_source_never_receives_audio_codec_declaration(ad_assets, tmp_path):
    assets = tmp_path / 'assets'
    shutil.copytree(ad_assets, assets)
    (assets / 'source000.ts').write_bytes(b'unsupported source audio')
    with controlled_relay(assets, tmp_path / 'cache') as (relay, origin):
        result = ControlClient(relay.descriptor).create(fixture_station(origin))
        assert result.get('adaptive_playback_url'), 'Controlled fixture should expose the guarded route'
        status, _, body = get(result['adaptive_playback_url'])
        assert status == 502
        assert b'CODECS' not in body
        session = relay.manager.get(result['id'])
        assert session.ad is None
        status, _, body = get(result['playback_url'])
        assert status == 200 and b'#EXT-X-CUE-OUT' in body
        assert b'CODECS' not in body


def test_audio_master_is_pinned_and_counts_against_resource_limit(ad_assets, tmp_path):
    with controlled_relay(ad_assets, tmp_path, resource_limit=3) as (relay, origin):
        result = ControlClient(relay.descriptor).create(fixture_station(origin))
        assert result.get('adaptive_playback_url'), 'One master slot must be reserved'
        session = relay.manager.get(result['id'])
        master_path = urlsplit(result['adaptive_playback_url']).path
        native_path = urlsplit(result['playback_url']).path
        session.register_many([origin + '/one'])
        session.register_many([origin + '/two'])
        assert session.lookup(master_path) is not None
        assert session.lookup(native_path) is not None
        assert session.resource_count == 3
        with pytest.raises(ResourceLimit):
            session.register_many([origin + '/three', origin + '/four'])
        relay.manager.stop(session.id)
        assert get(result['adaptive_playback_url'])[0] == 404


@pytest.mark.parametrize('enabled,uuid,limit', [
    (False, 'fixture-adaptive', 10), (True, 'radio-browser-station', 10),
    (True, 'fixture-adaptive', 2),
])
def test_audio_master_not_offered_outside_supported_scope(ad_assets, tmp_path, enabled, uuid, limit):
    with controlled_relay(ad_assets, tmp_path, resource_limit=limit, enabled=enabled) as (relay, origin):
        result = ControlClient(relay.descriptor).create(fixture_station(origin, uuid))
        assert 'adaptive_playback_url' not in result


def play_fixture(kodi, monkeypatch, adaptive_url, addon_result, handle=7, preflight=True):
    runtime, state, _ = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    native = 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'd' * 48 + '.m3u8'
    result = {'id': 'c' * 48, 'playback_url': native,
              'mime_type': 'application/vnd.apple.mpegurl'}
    if adaptive_url is not None:
        result['adaptive_playback_url'] = adaptive_url
    stopped = []
    client = SimpleNamespace(descriptor=ServiceDescriptor(32123, 'a' * 48, 'b' * 64),
                             create=lambda station: result, stop=stopped.append)
    monkeypatch.setattr(runtime, '_service', lambda *args: client)
    requests = []
    def rpc(raw):
        requests.append(json.loads(raw))
        return json.dumps(addon_result)
    monkeypatch.setattr(runtime.xbmc, 'executeJSONRPC', rpc, raising=False)
    monkeypatch.setattr(runtime, '_adaptive_ready', lambda *args: preflight, raising=False)
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', str(handle),
                        '?' + urlencode({'mode': 'play_fixture', 'id': 'fixture-30023'})])
    return native, stopped, requests


ADAPTIVE_URL = 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'e' * 48 + '.m3u8'
ENABLED_ADAPTIVE = {'jsonrpc': '2.0', 'id': 1, 'result': {
    'addon': {'addonid': 'inputstream.adaptive', 'enabled': True, 'version': '22.3.23'}}}


@pytest.mark.parametrize('handle', [7, -1])
def test_enabled_adaptive_plays_validated_same_session_master(kodi, monkeypatch, handle):
    native, stopped, requests = play_fixture(kodi, monkeypatch, ADAPTIVE_URL, ENABLED_ADAPTIVE, handle)
    _, state, _ = kodi
    item = state.resolved[0][1] if handle >= 0 else state.players[-1].item
    assert item.path == ADAPTIVE_URL
    assert item.props['inputstream'] == 'inputstream.adaptive'
    assert item.props['inputstream.adaptive.play_timeshift_buffer'] == 'true'
    assert item.mime == 'application/vnd.apple.mpegurl' and item.lookup is False
    if handle < 0:
        assert state.players[-1].file == ADAPTIVE_URL
    assert not stopped
    assert requests[0]['method'] == 'Addons.GetAddonDetails'
    assert requests[0]['params']['addonid'] == 'inputstream.adaptive'


@pytest.mark.parametrize('addon_result', [
    {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32602, 'message': 'Invalid params.'}},
    {'result': {'addon': {'addonid': 'inputstream.adaptive', 'enabled': False, 'version': '22.3.23'}}},
    {'result': {'addon': {'addonid': 'inputstream.adaptive', 'enabled': True, 'version': '20.0.0'}}},
    {'result': {'addon': {'addonid': 'other.addon', 'enabled': True, 'version': '22.3.23'}}},
])
def test_unavailable_adaptive_keeps_native_playback(kodi, monkeypatch, addon_result):
    native, stopped, _ = play_fixture(kodi, monkeypatch, ADAPTIVE_URL, addon_result)
    _, state, _ = kodi
    assert state.resolved[0][0]
    assert state.resolved[0][1].path == native
    assert 'inputstream' not in state.resolved[0][1].props
    assert not stopped and not state.builtins


def test_unready_adaptive_master_falls_back_to_native(kodi, monkeypatch):
    native, stopped, _ = play_fixture(kodi, monkeypatch, ADAPTIVE_URL, ENABLED_ADAPTIVE,
                                    preflight=False)
    _, state, _ = kodi
    assert state.resolved[0][0]
    assert state.resolved[0][1].path == native
    assert 'inputstream' not in state.resolved[0][1].props
    assert not stopped and not state.logs


@pytest.mark.parametrize('supported', [True, False])
def test_preflight_accepts_only_a_successfully_composed_source(kodi, ad_assets, tmp_path, supported):
    runtime, _, _ = kodi
    assets = ad_assets
    if not supported:
        assets = tmp_path / 'assets'
        shutil.copytree(ad_assets, assets)
        (assets / 'source000.ts').write_bytes(b'unsupported source audio')
    with controlled_relay(assets, tmp_path / 'cache') as (relay, origin):
        result = ControlClient(relay.descriptor).create(fixture_station(origin))
        preflight = getattr(runtime, '_adaptive_ready', None)
        assert callable(preflight), 'Adaptive selection needs a bounded readiness check'
        assert preflight(result['adaptive_playback_url'], result['playback_url'],
                         relay.descriptor, threading.Event()) is supported
        session = relay.manager.get(result['id'])
        if supported:
            assert session.ad.committed
        else:
            assert session.ad is None
            assert get(result['playback_url'])[0] == 200


@contextmanager
def preflight_server(case):
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            if case == 'blocked':
                release.wait(2)
            native = 'http://127.0.0.1:%d/media/%s/%s.m3u8' % (
                self.server.server_port, 'c' * 48, 'd' * 48)
            child = 'http://radio.example/unrelated.m3u8' if case == 'other-child' else native
            body = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000,CODECS="mp4a.40.2"\n'
                    + child + '\n').encode()
            if case == 'media':
                body = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n/source.ts\n'
            if case == 'video':
                body = body.replace(b'mp4a.40.2', b'avc1.42e01e')
            if case == 'oversized':
                body += b'#' * 4096
            status = 503 if case == 'unavailable' else 302 if case == 'redirect' else 200
            self.send_response(status)
            self.send_header('Content-Type', 'text/html' if case == 'html' else 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(body)))
            if case == 'redirect':
                self.send_header('Location', native)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d/media/%s/' % (server.server_port, 'c' * 48)
    descriptor = ServiceDescriptor(server.server_port, 'a' * 48, 'b' * 64)
    try:
        yield base + 'e' * 48 + '.m3u8', base + 'd' * 48 + '.m3u8', descriptor, entered
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('case', ['unavailable', 'html', 'media', 'video', 'other-child', 'oversized', 'redirect'])
def test_preflight_rejects_error_or_unexpected_master(kodi, case):
    runtime, _, _ = kodi
    preflight = getattr(runtime, '_adaptive_ready', None)
    assert callable(preflight), 'Adaptive selection needs a bounded readiness check'
    with preflight_server(case) as (master, native, descriptor, _):
        assert preflight(master, native, descriptor, threading.Event()) is False


@pytest.mark.parametrize('cancelled', [False, True])
def test_preflight_deadline_falls_back_but_cancellation_propagates(kodi, monkeypatch, cancelled):
    from http_client import Cancelled
    runtime, _, _ = kodi
    preflight = getattr(runtime, '_adaptive_ready', None)
    assert callable(preflight), 'Adaptive selection needs a bounded readiness check'
    monkeypatch.setattr(runtime, 'ADAPTIVE_PREFLIGHT_TIMEOUT', .15)
    cancel = threading.Event()
    with preflight_server('blocked') as (master, native, descriptor, entered):
        timer = threading.Timer(.05, cancel.set) if cancelled else None
        if timer:
            timer.start()
        start = time.monotonic()
        try:
            if cancelled:
                with pytest.raises(Cancelled):
                    preflight(master, native, descriptor, cancel)
            else:
                assert preflight(master, native, descriptor, cancel) is False
            assert time.monotonic() - start < .7
        finally:
            if timer:
                timer.cancel()


@pytest.mark.parametrize('invalid', [
    'http://radio.example/master.m3u8',
    ADAPTIVE_URL.replace('32123', '32124'),
    ADAPTIVE_URL.replace('c' * 48, 'f' * 48),
    ADAPTIVE_URL + '?upstream=secret',
    ADAPTIVE_URL.replace('.m3u8', '.ts'),
    123,
])
def test_invalid_adaptive_url_cannot_escape_session(kodi, monkeypatch, invalid):
    _, stopped, _ = play_fixture(kodi, monkeypatch, invalid, ENABLED_ADAPTIVE)
    _, state, _ = kodi
    assert state.resolved and not state.resolved[0][0]
    assert stopped == ['c' * 48]
    assert not state.players and not state.logs
