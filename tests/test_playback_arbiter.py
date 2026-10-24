"""Concurrent addon-owned Player.play calls share a start lock."""
import importlib.util
from pathlib import Path
import threading
import time


MODULE = Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio/resources/lib/playback_arbiter.py'


def load_module():
    spec = importlib.util.spec_from_file_location('playback_arbiter_test', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distinct_addon_instances_serialize_idle_check_and_start(tmp_path):
    module = load_module()
    first = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    second = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    entered_start, release_start, second_started = (threading.Event() for _ in range(3))
    state = {'playing': False}

    def preview_start():
        entered_start.set()
        assert release_start.wait(2)
        state['playing'] = True

    preview = threading.Thread(target=lambda: first.start_if_available(
        lambda: not state['playing'], preview_start))
    direct = threading.Thread(target=lambda: second.start(
        lambda: (state.update(playing=True), second_started.set())))
    preview.start()
    assert entered_start.wait(2)
    direct.start()
    direct.join(0.05)
    assert not second_started.is_set()
    release_start.set()
    preview.join(2)
    direct.join(2)
    assert not preview.is_alive() and not direct.is_alive()
    assert second_started.is_set()


def test_preview_start_rechecks_busy_state_under_shared_lock(tmp_path):
    module = load_module()
    arbiter = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    player = {'playing': False, 'file': ''}
    started = []
    assert arbiter.start_if_available(
        lambda: not player['playing'],
        lambda: (started.append('preview'), player.update(playing=True, file='preview')))
    assert not arbiter.start_if_available(
        lambda: not player['playing'],
        lambda: started.append('stale-preview'))
    assert started == ['preview'] and player['file'] == 'preview'


def test_nested_arbiter_calls_reuse_the_outer_profile_file_lock(tmp_path):
    module = load_module()
    arbiter = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    completed = threading.Event()
    result = []

    def run():
        def outer_callback():
            nested = arbiter.start(lambda: None)
            result.append(arbiter.is_current_intent(nested))
        arbiter.start(outer_callback)
        completed.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert completed.wait(1), 'nested calls must not reacquire the file lock'
    worker.join(1)
    assert not worker.is_alive()
    assert result == [True]


def test_player_stop_callback_runs_without_shared_lock_and_blocks_new_starts(tmp_path):
    module = load_module()
    arbiter = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    intent = arbiter.start(lambda: None)
    entered_stop, release_stop, start_finished = threading.Event(), threading.Event(), threading.Event()
    stopped, started = [], []

    def stop():
        entered_stop.set()
        assert release_stop.wait(2)
        stopped.append(True)

    stopper = threading.Thread(target=lambda: arbiter.stop_if_current(intent, lambda: True, stop))
    stopper.start()
    assert entered_stop.wait(1)

    # A Kodi main-thread callback must be able to inspect intent while Player.stop
    # is waiting for that thread to service the stop request.
    assert not arbiter.is_current_intent(intent)

    starter = threading.Thread(target=lambda: (
        arbiter.start(lambda: started.append(True)), start_finished.set()))
    starter.start()
    assert not start_finished.wait(.05), 'new play must not race an in-flight stop'
    release_stop.set()
    stopper.join(1)
    starter.join(1)
    assert not stopper.is_alive() and not starter.is_alive()
    assert stopped == [True] and started == [True]


def test_future_dated_stop_marker_is_treated_as_stale(tmp_path):
    module = load_module()
    arbiter = module.PlaybackStartArbiter(tmp_path / 'playback.lock')
    with arbiter._locked():
        arbiter._write_intent('!%.6f' % (time.time() + 3600))

    started = []
    token = arbiter.start_if_available(lambda: True, lambda: started.append(True))

    assert token and started == [True]
