# SPDX-License-Identifier: GPL-2.0-or-later
"""Continuous controlled audio selects the supported buffered video player."""
import json
from types import SimpleNamespace
import pytest
from control import ServiceDescriptor

NATIVE = 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'd' * 48 + '.m3u8'
MASTER = NATIVE.replace('d' * 48, 'e' * 48)
AUDIO = NATIVE[:-5] + '.aac'


def play(kodi, monkeypatch, enabled=True, ready=True, audio=AUDIO, handle=7):
    runtime, state, _ = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    result = dict(id='c' * 48, playback_url=NATIVE, adaptive_playback_url=MASTER,
                  continuous_playback_url=audio, mime_type='application/vnd.apple.mpegurl')
    stopped = []
    client = SimpleNamespace(descriptor=ServiceDescriptor(32123, 'a' * 48, 'b' * 64),
                             create=lambda station: result, stop=stopped.append)
    monkeypatch.setattr(runtime, '_service', lambda *args: client)
    monkeypatch.setattr(runtime, '_adaptive_ready', lambda *args: ready)
    def rpc(raw):
        addon = json.loads(raw)['params']['addonid']
        return json.dumps({'result': {'addon': {'addonid': addon,
            'enabled': enabled if addon == 'inputstream.ffmpegdirect' else True,
            'version': '22.2.10'}}})
    monkeypatch.setattr(runtime.xbmc, 'executeJSONRPC', rpc, raising=False)
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', str(handle),
                        '?mode=play_fixture&id=fixture-30045'])
    return state, stopped


@pytest.mark.parametrize('handle', [7, -1])
def test_continuous_fixture_forces_buffered_video_player(kodi, monkeypatch, handle):
    state, stopped = play(kodi, monkeypatch, handle=handle)
    item = state.resolved[0][1] if handle >= 0 else state.players[-1].item
    assert item.props['playlist_type_hint'] == '0'
    assert item.music_info.title
    assert item.path == AUDIO
    assert item.mime == 'audio/aac' and item.lookup is False
    assert item.props['inputstream'] == 'inputstream.ffmpegdirect'
    assert item.props['inputstream-player'] == 'videodefaultplayer'
    assert item.props['inputstream.ffmpegdirect.stream_mode'] == 'timeshift'
    assert item.props['inputstream.ffmpegdirect.is_realtime_stream'] == 'false'
    assert not stopped


def test_missing_buffered_player_preserves_adaptive(kodi, monkeypatch):
    state, stopped = play(kodi, monkeypatch, enabled=False)
    assert state.resolved[0][1].path == MASTER
    assert state.resolved[0][1].props['inputstream'] == 'inputstream.adaptive'
    assert not stopped


def test_unready_composition_preserves_native(kodi, monkeypatch):
    state, stopped = play(kodi, monkeypatch, ready=False)
    assert state.resolved[0][1].path == NATIVE
    assert 'inputstream' not in state.resolved[0][1].props
    assert not stopped


@pytest.mark.parametrize('audio', [
    AUDIO.replace('127.0.0.1', 'localhost'), AUDIO.replace('c' * 48, 'b' * 48),
    AUDIO.replace('d' * 48, 'f' * 48), AUDIO + '?extra=1', AUDIO[:-4] + '.mp3',
])
def test_continuous_url_requires_same_root_identity(kodi, monkeypatch, audio):
    state, stopped = play(kodi, monkeypatch, audio=audio)
    assert state.resolved[0][0] is False
    assert stopped == ['c' * 48]
