# SPDX-License-Identifier: GPL-2.0-or-later
"""Continuous fixture output preserves validated AAC sample order, not a clock."""
import concurrent.futures
from dataclasses import FrozenInstanceError, replace
import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from ad_media import extract_adts
from ad_models import AdConfig
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from tools import ad_fixture_server
from url_policy import UrlPolicy


def request(url, method='GET', headers=None, body=None):
    target = urlsplit(url)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request(method, target.path, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def continuous_url(session):
    result = getattr(session, 'continuous_playback_url', None)
    assert result is not None, 'Controlled ad sessions must offer continuous AAC'
    return result


@pytest.fixture
def fixture(tmp_path, ad_assets):
    origin_server = ad_fixture_server.make_server(ad_assets, port=0)
    thread = threading.Thread(target=origin_server.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:%d' % origin_server.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             cache_root=tmp_path, resource_limit=12)
    relay = RelayServer(manager, client_timeout=.5)
    relay.start()

    def create(uuid='fixture-continuous', scenario='finite', enabled=True,
               midroll=True, vast='direct'):
        manager.ad_config = AdConfig(enabled, True, midroll,
                                     origin + '/vast/' + vast, origin)
        session = manager.create(Station(uuid, 'Fixture',
            origin + '/source/' + scenario + '/index.m3u8', 'AAC', True))
        if session.ad is not None and midroll:
            assert session.ad._midroll_done.wait(3)
        return session

    try:
        yield SimpleNamespace(origin=origin, relay=relay, manager=manager,
                              create=create, assets=ad_assets)
    finally:
        relay.close()
        origin_server.shutdown()
        origin_server.server_close()
        thread.join(2)


def asset_audio(fixture, names):
    return b''.join(extract_adts((fixture.assets / (name + '.ts')).read_bytes())[1]
                    for name in names)


def expected_names(last=6):
    return (['replacement000', 'replacement001', 'source000', 'source001',
             'replacement000', 'replacement001'] +
            ['source%03d' % index for index in range(4, last)])


def test_offer_reuses_root_token_and_only_controlled_ad_sessions(fixture):
    session = fixture.create()
    url = continuous_url(session)
    assert url == session.playback_url.removesuffix('.m3u8') + '.aac'
    assert session.lookup(url) is session.lookup(session.playback_url)
    assert session.resource_count == 2  # Native root plus existing Adaptive master.
    for arguments in ({'uuid': 'ordinary-radio'}, {'enabled': False},
                      {'midroll': False, 'vast': 'no-fill'}):
        other = fixture.create(**arguments)
        assert getattr(other, 'continuous_playback_url', None) is None


def test_control_response_advertises_optional_continuous_url(fixture):
    fixture.create()
    base = fixture.manager.base_url
    station = dict(uuid='fixture-post', name='Fixture', codec='AAC', hls=True,
                   url=fixture.origin + '/source/finite/index.m3u8')
    status, _, body = request(base + '/sessions', 'POST',
        {'Authorization': 'Bearer ' + fixture.relay.descriptor.control_token,
         'Content-Type': 'application/json'}, json.dumps(station))
    assert status == 201
    result = json.loads(body)
    assert result.get('continuous_playback_url') == (
        result['playback_url'].removesuffix('.m3u8') + '.aac')


def test_head_does_not_fetch_claim_or_change_delivery_evidence(fixture):
    session = fixture.create()
    url = continuous_url(session)
    before = json.loads(request(fixture.origin + '/stats')[2])
    state = session.continuous_audio_state()
    status, headers, body = request(url, 'HEAD', {'Range': 'bytes=123-'})
    assert (status, headers['Content-Type'], body) == (200, 'audio/aac', b'')
    assert 'Content-Length' not in headers
    assert json.loads(request(fixture.origin + '/stats')[2]) == before
    assert session.continuous_audio_state() == state
    assert not session._claimed
    assert not session.presentation().segments
    with pytest.raises(FrozenInstanceError):
        state.next_sample = 1


@pytest.mark.parametrize('range_header', [None, 'bytes=0-'])
def test_complete_aac_output_preserves_preroll_replacement_and_programme(fixture, range_header):
    session = fixture.create()
    headers = {} if range_header is None else {'Range': range_header}
    status, response_headers, body = request(continuous_url(session), headers=headers)
    assert status == 200
    assert response_headers['Content-Type'] == 'audio/aac'
    assert body == asset_audio(fixture, expected_names())
    wait_until(lambda: fixture.manager.get(session.id) is None)
    state = session.continuous_audio_state()
    assert state.started_from_zero and state.terminal
    assert state.next_sample == 8 * 96256
    assert json.loads(request(fixture.origin + '/stats')[2])['beacons'] == {}


@pytest.mark.parametrize('range_header', ['bytes=1-', 'bytes=0-7', 'bytes=-7', 'bad'])
def test_unsupported_ranges_never_claim_or_fetch(fixture, range_header):
    session = fixture.create()
    url = continuous_url(session)
    before = json.loads(request(fixture.origin + '/stats')[2])
    assert request(url, headers={'Range': range_header})[0] == 416
    assert not session._claimed
    assert fixture.manager.get(session.id) is session
    assert json.loads(request(fixture.origin + '/stats')[2]) == before


def test_unoffered_suffixes_cannot_select_the_continuous_representation(fixture):
    session = fixture.create()
    url = continuous_url(session)
    for suffix in ('', '.mp3', '.ts'):
        assert request(url.removesuffix('.aac') + suffix)[0] == 404
        assert request(url.removesuffix('.aac') + suffix, 'HEAD')[0] == 404
    assert not session._claimed


def test_single_consumer_get_does_not_make_head_or_conflict_consuming(fixture, monkeypatch):
    session = fixture.create()
    url = continuous_url(session)
    entered, release = threading.Event(), threading.Event()
    render = session.ad.render
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return render(*args)
    monkeypatch.setattr(session.ad, 'render', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(request, url)
        try:
            assert entered.wait(3)
            assert request(url, 'HEAD')[0] == 200
            assert request(url)[0] == 409
            assert fixture.manager.get(session.id) is session
        finally:
            release.set()
        assert first.result(timeout=3)[2] == asset_audio(fixture, expected_names())


def test_live_reloads_are_serialized_and_retirement_does_not_skip_audio(fixture, monkeypatch):
    import relay
    manifest = ad_fixture_server.source_manifest
    windows = []
    def advancing(scenario, window):
        if scenario != 'normal':
            return manifest(scenario, window)
        index = min(18, max(0, len(windows) - 1))
        windows.append(index)
        value = manifest(scenario, index)
        return value + ('#EXT-X-ENDLIST\n' if index == 18 else '')
    monkeypatch.setattr(ad_fixture_server, 'source_manifest', advancing)
    monkeypatch.setattr(relay, '_AD_RELOAD_INTERVAL', .01, raising=False)
    session = fixture.create(scenario='normal')
    url = continuous_url(session)
    root = session.lookup(session.playback_url)
    open_response = fixture.manager.client.open
    observations = []
    def checked(url, *args, **kwargs):
        if url == root.url:
            observations.append(root.playlist_lock.locked())
        return open_response(url, *args, **kwargs)
    monkeypatch.setattr(fixture.manager.client, 'open', checked)
    status, _, body = request(url)
    assert status == 200
    assert body == asset_audio(fixture, expected_names(24))
    assert observations and all(observations)
    assert len(windows) >= 19
    assert session.continuous_audio_state().next_sample == 26 * 96256
    assert session.resource_count <= session.resource_limit
    assert session.presentation().segments[0].sequence > 0


@pytest.mark.parametrize('fault', ['retired', 'sample_zero'])
def test_missing_sample_zero_fails_before_any_audio_is_offered(fixture, monkeypatch, fault):
    session = fixture.create()
    url = continuous_url(session)
    assert request(session.playback_url)[0] == 200
    presentation = session.ad.presentation()
    segments = presentation.segments
    if fault == 'retired':
        segments = segments[1:]
    else:
        segments = (replace(segments[0], start_sample=1024),) + segments[1:]
    monkeypatch.setattr(session.ad, 'presentation', lambda: replace(presentation, segments=segments))
    assert request(url)[0] == 502
    wait_until(lambda: session.continuous_audio_state().terminal)
    assert session.continuous_audio_state().next_sample == 0
    assert not session.continuous_audio_state().started_from_zero


@pytest.mark.parametrize('fault', ['sequence', 'sample', 'changed_resource', 'retired'])
def test_stream_closes_instead_of_jumping_or_accepting_changed_output(fixture, monkeypatch, fault):
    session = fixture.create()
    url = continuous_url(session)
    assert request(session.playback_url)[0] == 200
    original = session.ad.presentation()
    calls = []
    def changed():
        calls.append(True)
        if len(calls) == 1:
            return original
        segments = original.segments
        if fault == 'retired':
            segments = segments[2:]
        else:
            edits = {'sequence': {'sequence': 2}, 'sample': {'start_sample': 96257},
                     'changed_resource': {'resource_id': segments[2].resource_id}}
            segments = (segments[0], replace(segments[1], **edits[fault])) + segments[2:]
        return replace(original, segments=segments)
    monkeypatch.setattr(session.ad, 'presentation', changed)
    status, _, body = request(url)
    assert status == 200
    assert body == asset_audio(fixture, ['replacement000'])
    wait_until(lambda: session.continuous_audio_state().terminal)
    assert session.continuous_audio_state().next_sample == 96256


def test_stop_interrupts_waiting_for_next_source_window(fixture):
    session = fixture.create(scenario='normal')
    url = continuous_url(session)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(request, url)
        wait_until(lambda: session.continuous_audio_state().next_sample >= 8 * 96256)
        fixture.manager.stop(session.id)
        status, _, body = reader.result(timeout=2)
    assert status == 200 and body == asset_audio(fixture, expected_names())
    assert session.continuous_audio_state().terminal


def test_client_disconnect_during_source_wait_releases_session(fixture):
    session = fixture.create(scenario='normal')
    target = urlsplit(continuous_url(session))
    client = socket.create_connection((target.hostname, target.port), timeout=2)
    try:
        client.sendall(('GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n' % target.path).encode())
        wait_until(lambda: session.continuous_audio_state().next_sample >= 8 * 96256)
    finally:
        client.close()
    wait_until(lambda: fixture.manager.get(session.id) is None)
    assert session.continuous_audio_state().terminal


def test_partial_socket_write_never_advances_the_complete_sample_ceiling(fixture, monkeypatch):
    from relay import _Handler
    session = fixture.create()
    url = continuous_url(session)
    original_headers = _Handler._headers
    class PartialWriter:
        def __init__(self, writer):
            self.writer, self.writes = writer, 0
        def __getattr__(self, name):
            return getattr(self.writer, name)
        def write(self, data):
            self.writes += 1
            if self.writes == 2:
                self.writer.write(data[:31])
                raise TimeoutError('Simulated blocked consumer')
            return self.writer.write(data)
    def headers(self, status, values, length=None):
        original_headers(self, status, values, length)
        if values.get('content-type') == 'audio/aac':
            self.wfile = PartialWriter(self.wfile)
    monkeypatch.setattr(_Handler, '_headers', headers)
    status, _, body = request(url)
    assert status == 200
    assert body == (asset_audio(fixture, ['replacement000']) +
                    asset_audio(fixture, ['replacement001'])[:31])
    wait_until(lambda: fixture.manager.get(session.id) is None)
    state = session.continuous_audio_state()
    assert state.next_sample == 96256 and state.terminal
    assert json.loads(request(fixture.origin + '/stats')[2])['beacons'] == {}


def test_cancellation_during_composition_never_claims_sample_zero(fixture, monkeypatch):
    session = fixture.create()
    url = continuous_url(session)
    entered, release = threading.Event(), threading.Event()
    render = session.ad.render
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return render(*args)
    monkeypatch.setattr(session.ad, 'render', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(request, url)
        try:
            assert entered.wait(3)
            fixture.manager.stop(session.id)
            assert session.continuous_audio_state().terminal
        finally:
            release.set()
        assert reader.result(timeout=2)[0] == 502
    state = session.continuous_audio_state()
    assert not state.started_from_zero and state.next_sample == 0
