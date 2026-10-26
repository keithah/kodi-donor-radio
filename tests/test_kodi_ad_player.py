"""Kodi playback evidence stays session scoped and fixture-only."""
from types import SimpleNamespace
from itertools import count
import threading
import pytest

from ad_models import AdError, OutputSegment, Placement, Presentation, ResolvedAd, TrackingUrl
from kodi_ad_player import KodiAdPlayback, KodiSessionClockAnchor
from test_kodi_runtime import kodi  # Reuse the native Kodi boundary fixture.


def _patch_player_clock(monkeypatch, clock):
    import time as real_time
    monkeypatch.setattr('kodi_ad_player.time', SimpleNamespace(
        monotonic=clock, sleep=real_time.sleep, time=real_time.time))


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
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 24000, 72000),), False)
    clocks = iter((0.3, 0.5, 0.75, 1.0, 1.25, 1.5))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    records, queued = [], []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url):
            queued.append((placement_id, event, url)); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    monkeypatch.setattr(KodiAdPlayback, '_emit', lambda *_args: None)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .2)
    for _ in range(6):
        adapter.sample()
    assert adapter.mapping_valid is True
    assert adapter.last_sample.position_seconds == 1.5
    assert [event for _, event, _ in queued] == [
        'impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete']
    assert all(url.startswith('http://127.0.0.1:8765/') for _, _, url in queued)
    adapter.on_end("source_end")


@pytest.mark.parametrize('confirmation', (.1, .25, .318))
def test_startup_gate_does_not_replay_unverified_zero_as_preroll(monkeypatch, confirmation):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    tuple(TrackingUrl(event, 'http://127.0.0.1:8765/track/placement/' + event)
                          for event in ('impression', 'start', 'firstQuartile')),
                    ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 0, 48000),), False)
    clocks = iter((0.0, confirmation, .6))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    adapter.sample()
    assert queued == []
    adapter.sample()
    assert adapter._clock_ready is True
    assert queued == []
    adapter.sample()
    assert queued == []
    adapter.close()


def test_fixture_clock_jump_does_not_cross_ad_completion(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    (TrackingUrl('complete', 'http://127.0.0.1:8765/track/placement/complete'),), ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 0, 192528),), False)
    clocks = iter((1.37, 1.62, 1.87, 2.12, 2.37, 2.37, 7.77))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []
    records = []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, *event): queued.append(event); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    times = iter((10.0, 10.25, 10.50, 10.75, 11.0, 11.25, 11.50))
    _patch_player_clock(monkeypatch, lambda: next(times))
    for _ in range(5):
        adapter.sample()
    assert adapter._clock_ready is True
    for _ in range(2):
        adapter.sample()
    assert adapter.mapping_valid is True
    assert adapter.last_sample.position_seconds == 7.77
    assert not any(event[1] == 'complete' for event in queued)
    adapter.close()


def test_fixture_clock_anchor_is_sticky_and_invalidates_on_clock_reset(monkeypatch):
    session = _session()
    session.ad_enabled = True
    clocks = iter((0.0, .25, .5, .1, .35))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    mapped = []
    for _ in range(5):
        adapter.sample()
        mapped.append(adapter.mapping_valid)
    assert mapped == [True, True, True, False, False]
    assert adapter.last_sample.mapped is False
    adapter.close()


def test_clock_anchor_invalidates_on_negative_reset_after_sample_zero():
    anchor = KodiSessionClockAnchor("session")
    session = SimpleNamespace(id="session")
    assert anchor(session, None, 0.0) == 0.0
    assert anchor(session, None, -0.091) is None
    assert anchor.valid is False
    assert anchor(session, None, 0.25) is None


def test_clock_anchor_clamps_small_negative_jitter_at_zero():
    anchor = KodiSessionClockAnchor("session")
    session = SimpleNamespace(id="session")
    assert anchor(session, None, 0.0) == 0.0
    assert anchor(session, None, -0.03) == 0.0
    assert anchor.valid is True


def test_fixture_adapter_marks_cache_state_untrackable(monkeypatch):
    session = _session()
    session.ad_enabled = True
    session.presentation = lambda: Presentation((), (), False)
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: .25)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765", is_caching=lambda: True)
    adapter.bind(session.id)
    _patch_player_clock(monkeypatch, lambda: 10.0)
    adapter.sample()
    assert adapter.last_sample.caching is True
    adapter.close()


def test_startup_clock_reset_discards_pending_preroll_tracking(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    tuple(TrackingUrl(event, 'http://127.0.0.1:8765/track/placement/' + event)
                          for event in ('impression', 'start', 'firstQuartile')),
                    ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 0, 48000),), False)
    clocks = iter((0.0, 0.0, -0.091, .25))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    for _ in range(4):
        adapter.sample()
    assert queued == []
    assert adapter.mapping_valid is False
    adapter.close()


def test_pause_callback_discards_pending_startup_samples(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    tuple(TrackingUrl(event, 'http://127.0.0.1:8765/track/placement/' + event)
                          for event in ('impression', 'start', 'firstQuartile')),
                    ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 24000, 72000),), False)
    clocks = iter((0.0, .25, .5))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    adapter.sample()
    adapter.on_pause(session.id)
    adapter.on_resume(session.id)
    adapter.sample()
    assert queued == []
    adapter.sample()
    assert queued[:2] == ['impression', 'start']
    adapter.close()


def test_cache_after_startup_cannot_emit_milestones(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1.0,
                    tuple(TrackingUrl(event, 'http://127.0.0.1:8765/track/placement/' + event)
                          for event in ('impression', 'start', 'firstQuartile', 'midpoint',
                                        'thirdQuartile', 'complete')),
                    ())
    session.presentation = lambda: Presentation((), (Placement('placement', ad, 14400, 62400),), False)
    clocks = iter((.1, .3, .6, .8))
    cache_states = iter((False, False, True, True))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    queued = []
    records = []
    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    monkeypatch.setattr(KodiAdPlayback, '_emit', lambda *_args: None)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765",
                             is_caching=lambda: next(cache_states))
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    for _ in range(4):
        adapter.sample()
    assert adapter._clock_ready is True
    assert queued == ['impression', 'start']
    assert adapter.mapping_valid is True
    assert adapter.last_sample.mapped is True
    cached = [record for record in records
              if record.kind == 'ad_clock_sample' and record.details.get('caching')]
    assert len(cached) == 2
    assert all(record.details['tracking_enabled'] is False for record in cached)
    adapter.close()


@pytest.mark.parametrize('cached_polls', (3, 5))
def test_long_cache_plateau_taints_only_intersected_ad_after_close_resumption(monkeypatch, cached_polls):
    session = _session()
    session.ad_enabled = True
    events = ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event) for event in events), ())
    session.presentation = lambda: Presentation((), (
        Placement('stalled', ad, 24000, 72000),
        Placement('later', ad, 96000, 144000)), False)
    # The cache plateau lasts 750 ms or longer, then resumes only 100 ms
    # beyond the last played clock. Proximity alone cannot recover delivery.
    clocks = iter((.25, .5) + (.5,) * cached_polls +
                  (.6, .75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3))
    cache_states = iter((False, False) + (True,) * cached_polls + (False,) * 11)
    queued, records = [], []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url):
            queued.append((placement_id, event)); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=records.append)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765',
                             is_caching=lambda: next(cache_states))
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    try:
        adapter.sample()
        adapter.sample()
        assert queued == [('stalled', 'impression'), ('stalled', 'start')]
        for _ in range(cached_polls):
            adapter.sample()
        assert queued == [('stalled', 'impression'), ('stalled', 'start')]
        for _ in range(11):
            adapter.sample()
        assert [event for placement, event in queued if placement == 'stalled'] == ['impression', 'start']
        assert [event for placement, event in queued if placement == 'later'] == list(events)
        cached = [record for record in records
                  if record.kind == 'ad_clock_sample' and record.details['caching']]
        assert all(record.details['tracking_enabled'] is False for record in cached)
    finally:
        adapter.close()


def test_fixture_adapter_fails_closed_when_cache_state_is_unavailable(monkeypatch):
    session = _session()
    session.ad_enabled = True
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: .25)
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    def unavailable():
        raise RuntimeError("condition unavailable")
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765", is_caching=unavailable)
    adapter.bind(session.id)
    _patch_player_clock(monkeypatch, lambda: 10.0)
    adapter.sample()
    assert adapter.last_sample.caching is True
    adapter.close()


def test_fixture_adapter_logs_clock_read_failure_without_crashing(monkeypatch):
    session = _session()
    session.ad_enabled = True
    def unavailable():
        raise RuntimeError("player unavailable")
    player = SimpleNamespace(isPlaying=unavailable, getTime=lambda: .25)
    records = []
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    _patch_player_clock(monkeypatch, lambda: 10.0)
    adapter.sample()
    assert adapter.last_sample.playing is False
    assert adapter.last_sample.mapped is False
    assert adapter.last_sample.caching is True
    event = next(record for record in records if record.kind == 'ad_clock_sample')
    assert event.details['mapped'] is False
    assert 'raw_clock' not in event.to_dict().get('details', {})
    adapter.close()


def test_fixture_adapter_records_clock_and_mapping_diagnostics(monkeypatch):
    session = _session()
    session.ad_enabled = True
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: .25)
    records = []
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    _patch_player_clock(monkeypatch, lambda: 10.0)
    adapter.sample()
    event = next(record for record in records if record.kind == 'ad_clock_sample')
    assert event.source == 'kodi_player'
    assert event.evidence == 'poll'
    assert event.details == {'raw_clock': .25, 'mapped_position': .25, 'mapped': True,
                             'tracking_enabled': False,
                             'playing': True, 'paused': False, 'seeking': False,
                             'caching': False}
    assert 'http://' not in repr(event.to_dict())
    adapter.close()


def test_fixture_anchor_keeps_session_sample_origin_when_playlist_window_slides(monkeypatch):
    session = _session()
    session.ad_enabled = True
    clocks = iter((20.0, 20.25))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    session.presentation = lambda: Presentation(
        (OutputSegment(10, 1, "older-segment", 20 * 48000, 96256, None),), (), False)
    times = iter((10.0, 10.25))
    _patch_player_clock(monkeypatch, lambda: next(times))
    adapter.sample()
    adapter.sample()
    assert adapter.last_sample.position_seconds == 20.25
    assert adapter.last_sample.mapped is True
    adapter.close()


def test_fixture_clock_anchor_is_invalid_after_seek(monkeypatch):
    session = _session()
    session.ad_enabled = True
    clocks = iter((.1, .35, .6))
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks))
    manager = SimpleNamespace(get=lambda identity: session,
                              sink=SimpleNamespace(emit=lambda event: None))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10.0 + next(ticks) * .25)
    adapter.sample()
    adapter.on_seek(session.id)
    adapter.sample()
    assert adapter.last_sample.mapped is False
    adapter.sample()
    assert adapter.last_sample.mapped is False
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


def test_fixture_session_without_explicit_ad_opt_in_does_not_install_mapper_or_worker(monkeypatch):
    session = _session()
    created = []
    monkeypatch.setattr('kodi_ad_player.BeaconWorker', lambda *_args: created.append(True))
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda identity: session),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: .1),
                             client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    assert adapter.mapper is None
    assert adapter.worker is None
    assert created == []
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
    session.ad_enabled = True
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
    session.ad_enabled = True
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


def test_speed_callbacks_suppress_milestones_and_distrust_first_normal_sample(monkeypatch):
    session = _session()
    session.ad_enabled = True
    events = ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
    ad = ResolvedAd('creative', 'fixture.ts', 1.0, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event) for event in events), ())
    session.presentation = lambda: Presentation((), (Placement('p', ad, 24000, 72000),), False)
    clocks = iter((.25, .5, .75, 1, 1.25, 1.5))
    queued, records = [], []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=records.append)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765')
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    adapter.sample()
    adapter.sample()
    assert queued == ['impression', 'start']
    adapter.on_speed_changed(.5, session.id)
    adapter.sample()
    assert adapter.last_sample.speed == .5
    assert adapter.last_sample.speed_changed is True
    adapter.on_speed_changed(1, session.id)
    adapter.sample()
    assert adapter.last_sample.speed_changed is True
    assert records[-1].details['tracking_enabled'] is False
    adapter.sample()
    assert adapter.last_sample.speed_changed is False
    adapter.sample()
    assert queued == ['impression', 'start']
    adapter.close()


def test_speed_round_trip_between_polls_stays_untrusted_and_session_scoped(monkeypatch):
    session = _session()
    session.ad_enabled = True
    clocks = iter((0, .25, .5, .75))
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=lambda _: None)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765')
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    adapter.sample()
    adapter.sample()
    adapter.on_speed_changed(2, 'old-session')
    adapter.sample()
    assert adapter.last_sample.speed == 1
    assert adapter.last_sample.speed_changed is False
    adapter.on_speed_changed(2, session.id)
    adapter.on_speed_changed(1, session.id)
    adapter.sample()
    assert adapter.last_sample.speed == 1
    assert adapter.last_sample.speed_changed is True
    adapter.close()


@pytest.mark.parametrize('cached_speed', (.5, 1))
def test_cached_speed_change_keeps_crossed_placement_ambiguous(monkeypatch, cached_speed):
    session = _session()
    session.ad_enabled = True
    events = ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event) for event in events), ())
    session.presentation = lambda: Presentation((), (Placement('p', ad, 2400, 50400),), False)
    clocks = iter((0, .05, .1, .15, .3, .55, .8, 1.05))
    cache_states = iter((False, False, True, False, False, False, False, False))
    queued, records = [], []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=records.append)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765',
                             is_caching=lambda: next(cache_states))
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    try:
        adapter.sample()
        adapter.sample()
        assert queued == ['impression', 'start']
        adapter.on_speed_changed(.5, session.id)
        if cached_speed == 1:
            adapter.on_speed_changed(1, session.id)
        adapter.sample()
        cached_sample = adapter.last_sample
        assert cached_sample.caching is True
        assert records[-1].details['tracking_enabled'] is False
        assert queued == ['impression', 'start']
        if cached_speed != 1:
            adapter.on_speed_changed(1, session.id)
        # Resumption at .15 is within .25 of the last good position (.05).
        # Only retained speed evidence can reject this otherwise tolerated gap.
        for _ in range(5):
            adapter.sample()
        assert queued == ['impression', 'start']
        assert adapter.tracker._progress['p'].ambiguous is True
        assert cached_sample.mapped is True
        assert cached_sample.position_seconds == .1
        assert cached_sample.speed == cached_speed
        assert cached_sample.speed_changed is True
    finally:
        adapter.close()


@pytest.mark.parametrize('caching', (False, True))
@pytest.mark.parametrize('paused_speed', (.5, 1))
def test_paused_speed_change_taints_started_ad_after_close_resumption(monkeypatch, caching, paused_speed):
    session = _session()
    session.ad_enabled = True
    events = ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event) for event in events), ())
    session.presentation = lambda: Presentation((), (
        Placement('paused', ad, 24000, 72000),
        Placement('later', ad, 96000, 144000)), False)
    clocks = iter((.25, .5, .6, .65, .75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3))
    cache_states = iter((False, False, caching) + (False,) * 11)
    queued, records = [], []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url):
            queued.append((placement_id, event)); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=records.append)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765',
                             is_caching=lambda: next(cache_states))
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    try:
        adapter.sample()
        adapter.sample()
        assert adapter._clock_ready is True
        assert queued == [('paused', 'impression'), ('paused', 'start')]
        adapter.on_speed_changed(.5, session.id)
        if paused_speed == 1:
            adapter.on_speed_changed(1, session.id)
        adapter.on_pause(session.id)
        adapter.sample()
        paused_sample = adapter.last_sample
        assert records[-1].details['tracking_enabled'] is False
        assert queued == [('paused', 'impression'), ('paused', 'start')]
        adapter.on_resume(session.id)
        if paused_speed != 1:
            adapter.on_speed_changed(1, session.id)
        # Resuming 150 ms beyond the last good clock must not erase the
        # ambiguity carried by the paused speed sample across the active ad.
        for _ in range(11):
            adapter.sample()
        assert [event for placement, event in queued if placement == 'paused'] == ['impression', 'start']
        assert [event for placement, event in queued if placement == 'later'] == list(events)
        assert paused_sample.paused is True
        assert paused_sample.caching is caching
        assert paused_sample.mapped is True
        assert paused_sample.position_seconds == .6
        assert paused_sample.speed == paused_speed
        assert paused_sample.speed_changed is True
    finally:
        adapter.close()


@pytest.mark.parametrize('failed_read', ('clock', 'cache'))
@pytest.mark.parametrize('unavailable_count', (1, 3))
def test_unmapped_speed_round_trip_taints_started_ad_after_close_recovery(
        monkeypatch, failed_read, unavailable_count):
    session = _session()
    session.ad_enabled = True
    events = ('impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event) for event in events), ())
    session.presentation = lambda: Presentation((), (
        Placement('affected', ad, 24000, 72000),
        Placement('later', ad, 96000, 144000)), False)
    clocks = iter((.25, .5) + (.55,) * unavailable_count +
                  (.6, .75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3))
    unavailable = False
    queued, records = [], []

    def clock():
        position = next(clocks)
        if unavailable and failed_read == 'clock':
            raise RuntimeError('clock unavailable')
        return position

    def cache():
        if unavailable and failed_read == 'cache':
            raise RuntimeError('cache state unavailable')
        return False

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url):
            queued.append((placement_id, event)); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=records.append)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=clock),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765',
                             is_caching=cache)
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    try:
        adapter.sample()
        adapter.sample()
        assert adapter._clock_ready is True
        assert queued == [('affected', 'impression'), ('affected', 'start')]
        adapter.on_speed_changed(.5, session.id)
        adapter.on_speed_changed(1, session.id)
        unavailable = True
        for tick in range(unavailable_count):
            adapter.sample()
            assert adapter.last_sample.mapped is False
            assert adapter.last_sample.speed_changed is (tick == 0)
            assert records[-1].details['tracking_enabled'] is False
            assert queued == [('affected', 'impression'), ('affected', 'start')]
        unavailable = False
        # Only 100 ms beyond the last played position; ordinary proximity
        # recovery must not erase the earlier unlocated speed transition.
        for _ in range(11):
            adapter.sample()
        assert [event for placement, event in queued if placement == 'affected'] == ['impression', 'start']
        assert [event for placement, event in queued if placement == 'later'] == list(events)
        assert adapter.tracker._progress['affected'].ambiguous is True
    finally:
        adapter.close()


def test_native_speed_callback_requires_owned_armed_session(kodi):
    runtime, _, _ = kodi
    session = _session('a' * 48)
    player = runtime.RelayPlayer(SimpleNamespace(get=lambda _: session, stop=lambda _: None), 32123)
    player.playing = True
    player.file = 'http://127.0.0.1:32123/media/' + session.id + '/' + 'b' * 48
    player.onAVStarted()
    player.onPlayBackSpeedChanged(.5)
    assert player.ads.speed == .5
    assert player.ads.speed_changed is True
    player.ads.speed_changed = False
    player.onPlayBackSpeedChanged(.5)
    assert player.ads.speed_changed is False
    player.file = 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'd' * 48
    player.onPlayBackSpeedChanged(1)
    assert player.ads.speed == .5
    player.ads.close()


def test_non_normal_startup_cannot_later_manufacture_preroll_start(monkeypatch):
    session = _session()
    session.ad_enabled = True
    ad = ResolvedAd('creative', 'fixture.ts', 1, (TrackingUrl('start', 'http://127.0.0.1:8765/start'),), ())
    session.presentation = lambda: Presentation((), (Placement('p', ad, 0, 48000),), False)
    clocks = iter((0, .05, .1, .15, .2))
    queued = []

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, placement_id, event, url): queued.append(event); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    adapter = KodiAdPlayback(SimpleNamespace(get=lambda _: session, sink=SimpleNamespace(emit=lambda _: None)),
                             SimpleNamespace(isPlaying=lambda: True, getTime=lambda: next(clocks)),
                             client=SimpleNamespace(), fixture_origin='http://127.0.0.1:8765')
    adapter.bind(session.id)
    ticks = count()
    _patch_player_clock(monkeypatch, lambda: 10 + next(ticks) * .25)
    adapter.on_speed_changed(.5, session.id)
    adapter.sample()
    adapter.sample()
    assert queued == []
    adapter.on_speed_changed(1, session.id)
    adapter.sample()
    assert queued == []
    adapter.sample()
    adapter.sample()
    assert queued == []
    adapter.close()
