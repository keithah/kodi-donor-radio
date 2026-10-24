# SPDX-License-Identifier: GPL-2.0-or-later
import threading
import time

import pytest

from models import Station
from station_resolver import ResolvedSource


class Probe:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


def manager(**kwargs):
    from sessions import SessionManager
    return SessionManager(resolver=lambda *args: ResolvedSource('http://radio.example/live', 'continuous', Probe(), b'a'), **kwargs)


def station():
    return Station('station', 'Radio', 'http://radio.example/live', 'MP3', False)


def test_ad_config_is_snapshotted_per_session_and_errors_survive_preparation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ad_models import AdConfig, AdError
    from sessions import SessionManager
    from url_policy import UrlPolicy
    import ad_session
    origin = 'http://127.0.0.1:8765'
    configs = []
    class FakeAdSession:
        def __init__(self, config, client, root, cancel, limit, source, on_error):
            configs.append(config)
            assert root == tmp_path
            on_error(AdError(403, 'No supported VAST media', (origin + '/error',)))
            self.preroll_available = False
        def close(self): pass
    monkeypatch.setattr(ad_session, 'AdSession', FakeAdSession)
    client = SimpleNamespace(policy=UrlPolicy(test_origins={origin}))
    first = AdConfig(True, True, True, origin + '/first.xml', origin)
    second = AdConfig(True, True, True, origin + '/second.xml', origin)
    manager = SessionManager(client=client, resolver=lambda *args: ResolvedSource(
        origin + '/hls/index.m3u8', 'hls'), ad_config=first, cache_root=tmp_path)
    session = manager.create(Station('fixture-1', 'Fixture', origin + '/hls/index.m3u8', 'AAC', True))
    manager.ad_config = second
    assert configs == [first]
    assert len(session.take_ad_errors()) == 1
    assert session.take_ad_errors() == []
    manager.create(Station('fixture-2', 'Fixture', origin + '/hls/index.m3u8', 'AAC', True))
    assert configs == [first, second]
    manager.close()


def test_presentation_uses_one_ad_reference_during_concurrent_disable():
    from sessions import PlaybackSession

    ad = type('Ad', (), {'presentation': lambda self: 'current'})()

    class Session:
        reads = 0

        @property
        def ad(self):
            self.reads += 1
            return ad if self.reads == 1 else None

    session = Session()
    assert PlaybackSession.presentation(session) == 'current'
    assert session.reads == 1


def test_retire_cached_uses_one_ad_reference_during_concurrent_disable():
    from sessions import PlaybackSession
    from collections import OrderedDict

    resource = type('Resource', (), {'id': 'cached-resource', 'active': 0})()
    class Session:
        def __init__(self):
            self.reads = 0
            self._lock = threading.RLock()
            self._cache_routes = {'cache-id': resource}
            self._resources = OrderedDict([(resource.id, resource)])
        @property
        def ad(self):
            self.reads += 1
            return type('Ad', (), {'published_ids': set()})() if self.reads == 1 else None

    session = Session()
    PlaybackSession.retire_cached(session)
    assert session.reads == 1
    assert not session._cache_routes
    assert not session._resources


def test_close_uses_one_ad_reference_during_concurrent_disable():
    from sessions import PlaybackSession
    class Ad:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    ad = Ad()
    class Tracker:
        def __init__(self): self.reason = None
        def finish(self, reason): self.reason = reason
    class Session:
        def __init__(self):
            self.reads = 0
            self.cancel = threading.Event()
            self.source = type('Source', (), {'response': None})()
            self.tracker = Tracker()
        @property
        def ad(self):
            self.reads += 1
            return ad if self.reads == 1 else None
    session = Session()
    PlaybackSession.close(session)
    assert session.reads == 1 and ad.closed
    assert session.tracker.reason == 'session_stopped'


def test_inflight_create_keeps_settings_from_selection_time(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ad_models import AdConfig
    from sessions import SessionManager
    from url_policy import UrlPolicy
    import ad_session
    origin = 'http://127.0.0.1:8765'
    entered, release = threading.Event(), threading.Event()
    configs = []
    class FakeAdSession:
        def __init__(self, config, *args):
            configs.append(config)
            self.preroll_available = False
        def close(self): pass
    monkeypatch.setattr(ad_session, 'AdSession', FakeAdSession)
    def resolve(*args):
        entered.set()
        release.wait(1)
        return ResolvedSource(origin + '/source.m3u8', 'hls')
    initial = AdConfig(True, True, True, origin + '/initial.xml', origin)
    later = AdConfig(True, True, True, origin + '/later.xml', origin)
    manager = SessionManager(client=SimpleNamespace(policy=UrlPolicy(test_origins={origin})),
                             resolver=resolve, ad_config=initial, cache_root=tmp_path)
    worker = threading.Thread(target=lambda: manager.create(Station(
        'fixture-1', 'Fixture', origin + '/source.m3u8', 'AAC', True)))
    worker.start()
    assert entered.wait(1)
    manager.ad_config = later
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert configs == [initial]
    manager.close()


def test_session_preparation_does_not_hold_manager_lock(monkeypatch):
    import sessions
    from sessions import SessionManager

    preparing, release, lookup_finished = threading.Event(), threading.Event(), threading.Event()
    original = sessions.PlaybackSession
    created, errors = [], []

    def delayed_session(*args, **kwargs):
        preparing.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(sessions, 'PlaybackSession', delayed_session)
    manager = SessionManager(resolver=lambda *args: ResolvedSource(
        'http://radio.example/live', 'continuous', Probe(), b'a'))

    def create():
        try:
            created.append(manager.create(station()))
        except Exception as error:
            errors.append(error)

    creator = threading.Thread(target=create)
    creator.start()
    assert preparing.wait(1)
    lookup = threading.Thread(target=lambda: (manager.get('missing'), lookup_finished.set()))
    lookup.start()
    try:
        assert lookup_finished.wait(.5), 'Session preparation blocked manager lookups'
    finally:
        release.set()
        creator.join(2)
        lookup.join(2)
        manager.close()
    assert not creator.is_alive() and not lookup.is_alive()
    assert not errors and len(created) == 1


def test_close_cancels_session_preparation_and_closes_unpublished_session(monkeypatch):
    import sessions
    from sessions import SessionManager

    preparing, release, closed, close_finished = (threading.Event() for _ in range(4))
    errors = []

    class PreparedSession:
        def close(self, *args):
            closed.set()

    def delayed_session(*args, **kwargs):
        preparing.set()
        assert release.wait(2)
        return PreparedSession()

    monkeypatch.setattr(sessions, 'PlaybackSession', delayed_session)
    manager = SessionManager(resolver=lambda *args: ResolvedSource(
        'http://radio.example/live', 'continuous', Probe(), b'a'))

    def create():
        try:
            manager.create(station())
        except Exception as error:
            errors.append(error)

    creator = threading.Thread(target=create)
    creator.start()
    assert preparing.wait(1)
    closer = threading.Thread(target=lambda: (manager.close(), close_finished.set()))
    closer.start()
    try:
        assert close_finished.wait(.5), 'Shutdown blocked while session preparation was running'
    finally:
        release.set()
        creator.join(2)
        closer.join(2)
    assert not creator.is_alive() and not closer.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert closed.is_set()


def test_switch_cancels_previous_and_invalidates_opaque_urls():
    m = manager()
    first = m.create(station())
    second = m.create(station())
    assert first.cancel.is_set() and first.source.response.closed
    assert len(first.id) >= 32 and first.id != second.id
    assert m.get(first.id) is None
    assert m.get(second.id) is second
    m.close()
    assert second.cancel.is_set()


def test_resource_limit_evicts_inactive_but_keeps_active_and_batch_atomic():
    from sessions import ResourceLimit
    m = manager(resource_limit=4)
    s = m.create(station())
    a = s.register_many(['http://a/1'])[0]
    with s.acquire(a) as active:
        assert active.url == 'http://a/1'
        s.register_many(['http://a/2', 'http://a/3'])
        assert s.resource_count == 4
        assert s.lookup(a) is active
        with pytest.raises(ResourceLimit):
            s.register_many(['http://a/4', 'http://a/5', 'http://a/6'])
        assert s.resource_count == 4
    m.close()


def test_generic_resource_eviction_keeps_cached_route_index_consistent():
    m = manager(resource_limit=3)
    s = m.create(station())
    cached = s.cached_route('cache-id')
    s.register_many(['http://a/1'])
    s.register_many(['http://a/2'])
    resource = s.cached_route('cache-id')
    assert resource == cached
    assert s.lookup(resource) is not None
    m.close()


def test_idle_expiry_closes_probe_and_closed_manager_rejects_create():
    m = manager(idle_timeout=.01)
    s = m.create(station())
    time.sleep(.02)
    m.expire()
    assert m.get(s.id) is None and s.source.response.closed
    m.close()
    with pytest.raises(RuntimeError):
        m.create(station())


def test_continuous_claim_only_once_and_unknown_resource_rejected():
    m = manager()
    s = m.create(station())
    assert s.claim_continuous()
    assert not s.claim_continuous()
    assert s.lookup('forged') is None
    m.close()


def test_close_cancels_inflight_resolution_without_waiting_for_create_lock():
    from sessions import SessionManager
    entered, stopped = threading.Event(), threading.Event()
    def resolve(url, client, cancel):
        entered.set()
        cancel.wait(2)
        stopped.set()
        raise OSError('cancelled')
    m = SessionManager(resolver=resolve)
    def create():
        with pytest.raises(OSError):
            m.create(station())
    worker = threading.Thread(target=create)
    worker.start()
    assert entered.wait(1)
    m.close()
    assert stopped.wait(1)
    worker.join(1)
    assert not worker.is_alive()


@pytest.mark.parametrize('selections', [2, 3])
@pytest.mark.parametrize('close_manager', [False, True])
def test_newest_selection_cancels_running_and_queued_creates(selections,close_manager):
    from sessions import SessionManager
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    queued = {str(index): threading.Event() for index in range(selections)}
    calls, successes, failures, probes = [], {}, {}, []

    class ObservedLock:
        # Signal queue entry without depending on scheduler timing or FIFO locks.
        def __init__(self): self.lock = threading.Lock()
        def __enter__(self):
            queued[threading.current_thread().name].set()
            self.lock.acquire()
        def __exit__(self, *args): self.lock.release()

    def resolve(url, client, cancel):
        calls.append(url)
        probe = Probe(); probes.append(probe)
        if url.endswith('/0'):
            entered.set()
            if cancel.wait(2): cancelled.set()
            release.wait(2)
        # Simulate an upstream completion racing cancellation: it still owns a probe.
        return ResolvedSource(url, 'continuous', probe, b'a')

    m = SessionManager(resolver=resolve)
    m._create_lock = ObservedLock()
    def create(index):
        try:
            successes[index] = m.create(Station(str(index), 'Radio',
                'http://radio.example/%d' % index, 'MP3', False))
        except Exception as error:
            failures[index] = error
    workers = []
    try:
        for index in range(selections):
            worker = threading.Thread(target=create, args=(index,), name=str(index))
            workers.append(worker); worker.start()
            assert queued[str(index)].wait(1)
            if index == 0:
                assert entered.wait(1)
            elif index == 1:
                assert cancelled.wait(1), 'New selection did not cancel pending resolution'
        if close_manager:
            m.close()
        release.set()
        for worker in workers:
            worker.join(1)
            assert not worker.is_alive()
        assert probes[0].closed
        assert all(isinstance(error, RuntimeError) for error in failures.values())
        if close_manager:
            assert not successes
            assert set(failures) == set(range(selections))
            assert calls == ['http://radio.example/0']
        else:
            latest = selections - 1
            assert set(successes) == {latest}
            assert set(failures) == set(range(latest))
            assert calls == ['http://radio.example/0', 'http://radio.example/%d' % latest]
            assert m.get(successes[latest].id) is successes[latest]
            assert not probes[-1].closed
    finally:
        release.set()
        m.close()
        for worker in workers: worker.join(3)
    assert all(probe.closed for probe in probes)


def test_ad_cache_setup_failure_falls_back_before_publication(monkeypatch, tmp_path):
    from ad_models import AdConfig
    from ad_session import AdSession
    from http_client import HttpClient
    from url_policy import UrlPolicy
    origin = 'http://127.0.0.1:8766'
    def fail_setup(*args, **kwargs):
        raise OSError('profile unavailable')
    monkeypatch.setattr(AdSession, '__init__', fail_setup)
    probe = Probe()
    from sessions import SessionManager
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             resolver=lambda *args: ResolvedSource(origin + '/index.m3u8', 'hls', probe, b''),
                             ad_config=AdConfig(True, True, False, origin + '/vast/preroll', origin),
                             cache_root=tmp_path)
    try:
        session = manager.create(station())
        assert session.presentation() is None
        assert manager.get(session.id) is session
        assert not probe.closed
    finally:
        manager.close()


def test_ad_config_does_not_activate_on_non_test_origin_hls(tmp_path):
    from ad_models import AdConfig
    from http_client import HttpClient
    from url_policy import UrlPolicy
    fixture = 'http://127.0.0.1:8766'
    from sessions import SessionManager
    probe = Probe()
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={fixture})),
                             resolver=lambda *args: ResolvedSource('https://radio.example/index.m3u8', 'hls', probe, b''),
                             ad_config=AdConfig(True, True, False, fixture + '/vast/preroll', fixture),
                             cache_root=tmp_path)
    try:
        session = manager.create(station())
        assert session.presentation() is None
        assert not (tmp_path / 'controlled-ad-creative-cache').exists()
    finally:
        manager.close()
