# SPDX-License-Identifier: GPL-2.0-or-later
"""HTTP integration for controlled ad playback, including decoded AAC output."""
import http.client
import concurrent.futures
import json
import math
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

from ad_media import inspect_ts
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from tools.ad_fixture_server import make_server
from url_policy import UrlPolicy


ASSETS = Path(__file__).parent / 'generated-audio' / 'ad'


def get(url, method='GET', headers=None):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
    connection.request(method, parts.path, headers=headers or {})
    reply = connection.getresponse()
    result = reply.status, dict(reply.getheaders()), reply.read()
    connection.close()
    return result


def decode_tone(data):
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'mpegts', '-i', 'pipe:0',
                          '-f', 'f32le', '-ac', '1', 'pipe:1'], input=data,
                         capture_output=True, check=True).stdout
    import array
    samples = array.array('f'); samples.frombytes(pcm)
    # A narrow Goertzel scan makes the assertion about decoded sound, not TS bytes.
    def power(freq):
        n = min(24000, len(samples))
        coeff = 2 * math.cos(2 * math.pi * freq / 48000)
        a = b = 0.0
        for sample in samples[3000:3000+n]:
            a, b = sample + coeff*a - b, a
        return a*a + b*b - coeff*a*b
    return max((220, 330, 440, 550, 660, 770, 880), key=power)


def test_preroll_is_first_published_audio_and_cached_gets_are_stable(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, True, False, origin + '/vast/preroll', origin),
                             cache_root=tmp_path)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
        assert get(session.playback_url, 'HEAD')[0] == 200
        status, _, body = get(session.playback_url)
        assert status == 200
        text = body.decode()
        assert '#EXT-X-START:TIME-OFFSET=0' in text
        assert '#EXT-X-MEDIA-SEQUENCE:0' in text
        routes = [line for line in text.splitlines() if line and not line.startswith('#')]
        assert len(routes) >= 7
        base = 'http://127.0.0.1:%d' % relay.descriptor.port
        first = get(base + routes[0]); second = get(base + routes[1])
        assert first[0] == second[0] == 200
        assert inspect_ts(first[2]).samples == 96256
        assert decode_tone(first[2]) != decode_tone(get(base + routes[4])[2])
        assert get(base + routes[0])[2] == first[2]
        assert get(base + routes[0], headers={'Range': 'bytes=0-187'})[2] == first[2][:188]
        assert get(base + routes[0], headers={'Range': 'bytes=999999-'})[0] == 416
        assert get(session.playback_url, 'HEAD')[0] == 200
        stats = json.loads(get(origin + '/stats')[2])
        assert stats['requests']['/vast/preroll'] == 1
        assert stats['beacons'] == {}
    finally:
        relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_async_midroll_preserves_first_break_for_later_replacement(tmp_path, monkeypatch):
    from ad_models import AdConfig
    from vast import VastResolver
    import time
    gate = threading.Event()
    original = VastResolver.resolve
    def delayed(self, url, cancel):
        gate.wait(2)
        return original(self, url, cancel)
    monkeypatch.setattr(VastResolver, 'resolve', delayed)
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, False, True, origin + '/vast/direct', origin),
                             cache_root=tmp_path)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
        first = get(session.playback_url)[2].decode()
        assert len([line for line in first.splitlines() if line and not line.startswith('#')]) == 1
        assert '#EXT-X-ENDLIST' not in first
        gate.set()
        deadline = time.monotonic() + 5
        while not session.ad._midroll_done.is_set() and time.monotonic() < deadline:
            time.sleep(.01)
        assert session.ad._midroll_done.is_set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reloads = list(pool.map(get, [session.playback_url] * 2))
        assert all(result[0] == 200 for result in reloads)
        later = reloads[-1][2].decode()
        assert reloads[0][2] == reloads[1][2]
        routes = [line for line in later.splitlines() if line and not line.startswith('#')]
        assert len(routes) >= 5
        base = 'http://127.0.0.1:%d' % relay.descriptor.port
        replacement = get(base + routes[1])[2]
        assert replacement == (ASSETS / 'replacement000.ts').read_bytes()
        assert decode_tone(replacement) != decode_tone((ASSETS / 'source002.ts').read_bytes())
        assert '#EXT-X-DISCONTINUITY' in later
        assert json.loads(get(origin + '/stats')[2])['beacons'] == {}
    finally:
        gate.set(); relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_no_fill_and_disabled_mode_keep_source_manifest(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    try:
        for config in (AdConfig(True, True, False, origin + '/vast/no-fill', origin),
                       AdConfig(False, True, True, origin + '/vast/direct', origin)):
            manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                                     ad_config=config, cache_root=tmp_path)
            relay = RelayServer(manager); relay.start()
            try:
                session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
                manifest = get(session.playback_url)[2].decode()
                assert '#EXT-X-START:TIME-OFFSET=0' not in manifest
                assert '#EXT-X-CUE-OUT' in manifest
                assert session.presentation() is None
            finally:
                relay.close()
        assert json.loads(get(origin + '/stats')[2])['beacons'] == {}
    finally:
        fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_preroll_anchors_to_window_after_source_expiry(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, True, False, origin + '/vast/preroll', origin),
                             cache_root=tmp_path)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/expired-window/index.m3u8', '', True))
        manifest = get(session.playback_url)[2].decode()
        routes = [line for line in manifest.splitlines() if line and not line.startswith('#')]
        assert len(routes) == 4
        base = 'http://127.0.0.1:%d' % relay.descriptor.port
        assert get(base + routes[0])[2] == (ASSETS / 'preroll000.ts').read_bytes()
        assert get(base + routes[2])[2] == (ASSETS / 'source004.ts').read_bytes()
        assert '#EXT-X-START:TIME-OFFSET=0' in manifest
    finally:
        relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_unsupported_creative_falls_back_without_publishing_ad(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, True, False, origin + '/vast/unsupported-media', origin),
                             cache_root=tmp_path)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
        manifest = get(session.playback_url)[2].decode()
        assert '#EXT-X-CUE-OUT' in manifest
        assert session.presentation() is None
        assert json.loads(get(origin + '/stats')[2])['beacons'] == {}
    finally:
        relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_small_source_retention_omits_preroll_without_losing_programme(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, True, False, origin + '/vast/preroll', origin),
                             cache_root=tmp_path, resource_limit=6)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
        manifest = get(session.playback_url)[2].decode()
        assert '#EXT-X-START:TIME-OFFSET=0' not in manifest
        routes = [line for line in manifest.splitlines() if line and not line.startswith('#')]
        base = 'http://127.0.0.1:%d' % relay.descriptor.port
        assert get(base + routes[0])[2] == (ASSETS / 'source001.ts').read_bytes()
        assert session.resource_count <= 6
    finally:
        relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)


def test_stop_during_retained_source_invalidates_routes_and_removes_cache(tmp_path):
    from ad_models import AdConfig
    fixture = make_server(ASSETS, port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:%d' % fixture.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             ad_config=AdConfig(True, True, False, origin + '/vast/preroll', origin),
                             cache_root=tmp_path)
    relay = RelayServer(manager); relay.start()
    try:
        session = manager.create(Station('id', 'Fixture', origin + '/source/normal/index.m3u8', '', True))
        body = get(session.playback_url)[2].decode()
        route = next(line for line in body.splitlines() if line and not line.startswith('#'))
        base = 'http://127.0.0.1:%d' % relay.descriptor.port
        assert get(base + route)[0] == 200
        cache_path = session.ad.creative.session
        assert cache_path.exists()
        manager.stop(session.id)
        assert get(base + route)[0] == 404
        assert not cache_path.exists()
        assert session.cancel.is_set()
    finally:
        relay.close(); fixture.shutdown(); fixture.server_close(); thread.join(2)
