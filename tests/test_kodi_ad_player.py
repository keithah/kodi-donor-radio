"""Kodi playback evidence stays session scoped and uncalibrated by default."""
from types import SimpleNamespace
import threading

from ad_models import AdError, Presentation
from kodi_ad_player import KodiAdPlayback


def _session(identity="a"):
    return SimpleNamespace(id=identity, cancel=threading.Event(),
                           station=SimpleNamespace(uuid='fixture'),
                           source=SimpleNamespace(protocol='hls'),
                           presentation=lambda: Presentation((), (), False),
                           take_ad_errors=lambda: [])


def test_unverified_clock_does_not_send_success_beacons():
    session = _session()
    player = SimpleNamespace(isPlaying=lambda: True, getTime=lambda: 0.1)
    manager = SimpleNamespace(get=lambda identity: session)
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(), fixture_origin="http://127.0.0.1:8765")
    adapter.bind(session.id)
    adapter.sample()
    assert adapter.mapping_valid is False
    assert adapter.last_sample.mapped is False
    adapter.on_end("source_end")


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
