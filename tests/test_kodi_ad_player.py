"""Kodi playback evidence stays session scoped and fixture-only."""
from types import SimpleNamespace
from itertools import count
import threading

from ad_models import AdError, Placement, Presentation, ResolvedAd, TrackingUrl
from kodi_ad_player import KodiAdPlayback


def _session(identity="a"):
    return SimpleNamespace(id=identity, cancel=threading.Event(),
                           station=SimpleNamespace(uuid='fixture'),
                           source=SimpleNamespace(protocol='hls'),
                           presentation=lambda: Presentation((), (), False),
                           take_ad_errors=lambda: [])


def test_loopback_ad_session_maps_raw_kodi_clock_and_sends_success_events(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/placement/' + event)
        for event in ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')),
        ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 0, 48000),), False)
    clocks = iter((0.1, 0.3, 0.55, 0.8, 1.0))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    records, queued = [], []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url):
            queued.append((placement_id, event, url)); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    monkeypatch.setattr('kodi_ad_player.time.monotonic', lambda: 10.0 + next(ticks) * .2)
    for _ in range(5):
        adapter.sample()
    assert adapter.mapping_valid is True
    assert adapter.last_sample.position_seconds == 1.0
    assert [event for _, event, _ in queued] == [
        'impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete']
    assert all(url.startswith('http://127.0.0.1:8765/') for _, _, url in queued)
    adapter.on_end("source_end")


def test_fixture_clock_jump_does_not_cross_ad_completion(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    (TrackingUrl('complete', 'http://127.0.0.1:8765/track/placement/complete'),), ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 0, 192528),), False)
    clocks = iter((1.37, 1.37, 7.77))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, *event): queued.append(event); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    times = iter((10.0, 15.0, 15.26))
    monkeypatch.setattr('kodi_ad_player.time.monotonic', lambda: next(times))
    for _ in range(3):
        adapter.sample()
    assert adapter.last_sample.mapped is True
    assert not any(event[1] == 'complete' for event in queued)
    adapter.close()


def test_non_fixture_session_does_not_install_clock_mapper():
    session = _session()
    session.ad_enabled = True
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda identity: session),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: .1),
                             client=SimpleNamespace(), fixture_origin=None)
    adapter.bind(session.id)
    assert adapter.mapper is None
    adapter.close()


def test_stale_callbacks_cannot_retire_new_session():
    current = [_session("old")]
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: 1.0)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda identity: current[0] if identity == current[0].id else None),
                             player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind("old")
    current[0] = _session("new")
    adapter.bind("new")
    adapter.on_pause("old")
    adapter.on_seek("old")
    adapter.on_end("cancelled", "old")
    assert adapter.session_id == "new"
    assert adapter.paused is False
    adapter.close()


def test_errors_are_dispatched_only_for_current_session(monkeypatch):
    session = _session()
    session.take_ad_errors = lambda: [AdError(403, "No supported VAST media", ("http://127.0.0.1:8765/error",))]
    calls = []
    monkeypatch.setattr("kodi_ad_player.enqueue_ad_error", lambda worker, identity, error: calls.append((identity, error.code)))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: 0.0)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda identity: session,
                                              sink=SimpleNamespace(emit=lambda event: None)), player,
                             client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    adapter.sample()
    assert calls == [(session.id, 403)]
    adapter.close()


def test_early_playback_error_flushes_preparation_error(monkeypatch):
    session = _session()
    session.take_ad_errors = lambda: [AdError(403, 'No supported VAST media', ())]
    calls = []
    monkeypatch.setattr('kodi_ad_player.enqueue_ad_error',
                        lambda worker, identity, error: calls.append(error.code))
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, SimpleNamespace(), client=SimpleNamespace(),
                             fixture_origin='http://127.0.0.1:8765')
    adapter.bind(session.id)
    adapter.on_end('cancelled', session.id)
    assert calls == [403]


def test_shutdown_cancels_inflight_error_beacon_without_kodi_calls():
    entered = threading.Event()
    cancelled = threading.Event()
    caller = threading.current_thread()
    class Client:
        def open(self, url, headers, cancel, **kwargs):
            assert threading.current_thread() is not caller
            entered.set()
            assert __import__('time').monotonic() < kwargs['deadline']
            for _ in range(200):
                if cancel.is_set():
                    cancelled.set()
                    break
                __import__('time').sleep(.005)
            raise OSError('cancelled')
    session = _session()
    session.ad_enabled = True
    pending = [AdError(403, 'No supported VAST media', ('http://127.0.0.1:8765/error',))]
    session.take_ad_errors = lambda: [pending.pop()] if pending else []
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    def is_playing():
        assert threading.current_thread() is caller
        return False
    player = SimpleNamespace(isPlaying=is_playing)
    adapter = KodiAdPlayback(manager, player, client=Client(),
                             fixture_origin='http://127.0.0.1:8765')
    adapter.bind(session.id)
    adapter.sample()
    assert entered.wait(1)
    before = __import__('time').monotonic()
    adapter.close()
    assert __import__('time').monotonic() - before < 1
    assert cancelled.is_set()
    assert adapter.session_id is None
