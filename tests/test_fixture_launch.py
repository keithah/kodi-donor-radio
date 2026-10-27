# SPDX-License-Identifier: GPL-2.0-or-later
"""Only an authenticated, single-use fixture launch can qualify its session."""
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from ad_models import AdConfig
from control import ControlClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from station_resolver import ResolvedSource
from url_policy import UrlPolicy


@pytest.fixture
def launches(monkeypatch, tmp_path):
    now = SimpleNamespace(value=100.0)
    monkeypatch.setattr('sessions.time', SimpleNamespace(monotonic=lambda: now.value))
    origin = 'http://127.0.0.1:8765'

    class Ad:
        preroll_available = True
        terminal = False
        def __init__(self, *args): pass
        def close(self): self.terminal = True

    monkeypatch.setattr('ad_session.AdSession', Ad)
    manager = SessionManager(
        client=SimpleNamespace(policy=UrlPolicy(test_origins={origin})),
        resolver=lambda url, *_args: ResolvedSource(url, 'hls'),
        ad_config=AdConfig(True, True, True, origin + '/vast/direct', origin),
        cache_root=tmp_path)
    relay = RelayServer(manager)
    relay.start()
    station = Station('fixture-30045', 'Fixture', origin + '/source/normal/index.m3u8', 'AAC', True)
    try:
        yield SimpleNamespace(manager=manager, relay=relay, now=now, station=station,
                              client=ControlClient(relay.descriptor))
    finally:
        relay.close()


def test_ticket_qualifies_only_its_created_session_and_never_ordinary_playback(launches):
    l = launches
    ordinary = l.manager.create(l.station)
    assert ordinary.fixture_clock_qualified is False
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    assert l.manager.qualify_fixture_clock(ordinary.id, nonce) is False
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    assert session.fixture_clock_qualified is False
    assert l.manager.qualify_fixture_clock(session.id, nonce) is True
    assert session.fixture_clock_qualified is True
    assert l.manager.qualify_fixture_clock(session.id, nonce) is False
    with pytest.raises(AttributeError):
        session.fixture_clock_qualified = True
    l.manager.stop(session.id)
    assert session.fixture_clock_qualified is False


def test_consumed_nonce_cannot_recreate_or_interrupt_current_session(launches):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    with pytest.raises(ValueError):
        l.manager.create(l.station, fixture_launch_nonce=nonce)
    assert l.manager.get(session.id) is session
    assert not session.cancel.is_set()


def test_wrong_fixture_does_not_consume_ticket_or_replace_selected_station(launches):
    l = launches
    original = l.manager.create(l.station)
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    other = Station('fixture-30044', 'Other', l.station.url, 'AAC', True)
    with pytest.raises(ValueError):
        l.manager.create(other, fixture_launch_nonce=nonce)
    assert l.manager.get(original.id) is original
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    assert l.manager.qualify_fixture_clock(session.id, nonce) is True


@pytest.mark.parametrize('consume_first', [False, True])
def test_ticket_expires_after_thirty_seconds_without_qualifying(launches, consume_first):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    session = l.manager.create(l.station, fixture_launch_nonce=nonce) if consume_first else None
    l.now.value += 30
    if session is None:
        with pytest.raises(ValueError):
            l.manager.create(l.station, fixture_launch_nonce=nonce)
    else:
        assert l.manager.qualify_fixture_clock(session.id, nonce) is False
        assert session.fixture_clock_qualified is False


def test_qualified_session_does_not_expire_with_its_spent_launch_ticket(launches):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    assert l.manager.qualify_fixture_clock(session.id, nonce) is True
    l.now.value += 60
    assert l.manager.get(session.id) is session
    assert session.fixture_clock_qualified is True


@pytest.mark.parametrize('qualification_wait,accepted', [(5, True), (29.9, True), (30, False)])
def test_consumed_launch_gets_bounded_setup_then_fresh_qualification_time(
        launches, qualification_wait, accepted):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    l.now.value += 29  # An almost-expired queued launch can still begin setup.

    def resolve(url, client, cancel):
        l.now.value += 35  # Includes slow request plus bounded preparation work.
        l.manager.expire()
        assert not cancel.is_set()
        return ResolvedSource(url, 'hls')

    l.manager.resolver = resolve
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    l.now.value += qualification_wait
    assert l.manager.qualify_fixture_clock(session.id, nonce) is accepted
    assert session.fixture_clock_qualified is accepted


@pytest.mark.parametrize('housekeeping', [False, True])
def test_create_phase_expires_at_forty_seconds_even_without_housekeeping(launches, housekeeping):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)

    def resolve(url, client, cancel):
        l.now.value += 40
        if housekeeping:
            l.manager.expire()
            assert cancel.is_set()
        return ResolvedSource(url, 'hls')

    l.manager.resolver = resolve
    with pytest.raises(RuntimeError, match='cancelled'):
        l.manager.create(l.station, fixture_launch_nonce=nonce)
    with pytest.raises(ValueError):
        l.manager.create(l.station, fixture_launch_nonce=nonce)



def test_only_latest_launch_ticket_remains_valid_without_interrupting_playback(launches):
    l = launches
    original = l.manager.create(l.station)
    previous = l.manager.reserve_fixture_launch(l.station.uuid)
    for _ in range(32):
        latest = l.manager.reserve_fixture_launch(l.station.uuid)
        assert latest != previous
        with pytest.raises(ValueError):
            l.manager.create(l.station, fixture_launch_nonce=previous)
        assert l.manager.get(original.id) is original
        previous = latest
    session = l.manager.create(l.station, fixture_launch_nonce=latest)
    assert l.manager.qualify_fixture_clock(session.id, latest) is True


def test_normal_station_selection_revokes_older_queued_fixture_launch(launches):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    selected = l.manager.create(l.station)
    with pytest.raises(ValueError):
        l.manager.create(l.station, fixture_launch_nonce=nonce)
    assert l.manager.get(selected.id) is selected


@pytest.mark.parametrize('change', ['disabled', 'origin', 'ordinary', 'fallback'])
def test_unqualified_configuration_or_transport_cannot_activate(launches, change):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    if change == 'disabled':
        l.manager.ad_config = None
    elif change == 'origin':
        l.manager.ad_config = AdConfig(True, True, True, 'http://127.0.0.1:8766/vast/direct',
                                       'http://127.0.0.1:8766')
    elif change == 'ordinary':
        with pytest.raises(ValueError):
            l.manager.reserve_fixture_launch('radio-station')
        return
    session = l.manager.create(l.station, fixture_launch_nonce=nonce)
    if change == 'fallback':
        session.ad.close()
        session.ad = None
    assert l.manager.qualify_fixture_clock(session.id, nonce) is False
    assert session.fixture_clock_qualified is False


def test_launch_cancellation_cancels_an_inflight_resolver(launches):
    l = launches
    nonce = l.manager.reserve_fixture_launch(l.station.uuid)
    entered, release = threading.Event(), threading.Event()
    failures = []

    def resolve(url, client, cancel):
        entered.set()
        assert release.wait(2)
        assert cancel.is_set()
        return ResolvedSource(url, 'hls')

    def create():
        try:
            l.manager.create(l.station, fixture_launch_nonce=nonce)
        except RuntimeError as error:
            failures.append(error)

    l.manager.resolver = resolve
    worker = threading.Thread(target=create)
    worker.start()
    try:
        assert entered.wait(2)
        l.manager.cancel_fixture_launch(nonce)
    finally:
        release.set()
        worker.join(2)
    assert failures and not worker.is_alive()


def test_authenticated_http_round_trip_and_launch_revocation(launches):
    l = launches
    nonce = l.client.reserve_fixture_launch(l.station.uuid)
    result = l.client.create(l.station, fixture_launch_nonce=nonce)
    session = l.manager.get(result['id'])
    assert session.fixture_clock_qualified is False
    l.client.qualify_fixture_clock(session.id, nonce)
    assert session.fixture_clock_qualified is True
    with pytest.raises(OSError):
        l.client.create(l.station, fixture_launch_nonce=nonce)
    l.client.cancel_fixture_launch(nonce)
    assert session.cancel.is_set()
    assert l.manager.get(session.id) is None


@pytest.mark.parametrize('path,payload', [
    ('/fixture-launches', {'fixture_id': 'fixture-30045'}),
    ('/fixture-clock/qualify', {'session_id': 'a' * 48, 'nonce': 'b' * 48}),
])
def test_launch_control_requires_authentication(launches, path, payload):
    client = http.client.HTTPConnection('127.0.0.1', launches.relay.descriptor.port)
    try:
        client.request('POST', path, json.dumps(payload), {'Content-Type': 'application/json'})
        response = client.getresponse()
        response.read()
        assert response.status == 401
    finally:
        client.close()


def test_station_fields_cannot_claim_clock_qualification(launches):
    l = launches
    payload = {key: getattr(l.station, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')}
    payload['fixture_clock_qualified'] = True
    with pytest.raises(OSError):
        l.client._request('POST', '/sessions', payload)


def test_qualification_read_uses_one_ad_snapshot_during_fallback():
    from sessions import PlaybackSession

    class ChangingSession:
        _lock = threading.RLock()
        _fixture_clock_qualified = True
        cancel = threading.Event()
        reads = 0

        @property
        def ad(self):
            self.reads += 1
            return SimpleNamespace(terminal=False) if self.reads == 1 else None

    session = ChangingSession()
    assert PlaybackSession.fixture_clock_qualified.fget(session) is True
    assert session.reads == 1
