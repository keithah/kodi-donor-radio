"""Concurrent addon-owned Player.play calls share a start lock."""
import importlib.util
from pathlib import Path
import threading


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
