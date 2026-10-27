# SPDX-License-Identifier: GPL-2.0-or-later
"""The developer launcher requests the named core before qualifying a fixture."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from control import ServiceDescriptor


NATIVE = 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'd' * 48 + '.m3u8'
AUDIO = NATIVE[:-5] + '.aac'
NONCE = 'f' * 48


@pytest.fixture
def launch(kodi, monkeypatch):
    runtime, state, _ = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_preroll=True, ad_midroll=True,
                          ad_vast_url='http://127.0.0.1:8765/vast/direct')
    result = dict(id='c' * 48, playback_url=NATIVE,
                  adaptive_playback_url=NATIVE.replace('d' * 48, 'e' * 48),
                  continuous_playback_url=AUDIO, mime_type='application/vnd.apple.mpegurl')
    calls = SimpleNamespace(reserved=[], created=[], qualified=[], cancelled=[], stopped=[], rpc=[])
    behavior = SimpleNamespace(version='22.2.10', enabled=True, ready=True,
                               rpc_result={'result': 'OK'}, qualify_error=False)

    def reserve(fixture_id):
        calls.reserved.append(fixture_id)
        return NONCE

    def create(station, fixture_launch_nonce=None):
        calls.created.append((station.uuid, fixture_launch_nonce))
        return result

    def qualify(session_id, nonce):
        calls.qualified.append((session_id, nonce))
        if behavior.qualify_error:
            raise OSError('Qualification refused')

    def rpc(raw):
        request = json.loads(raw)
        calls.rpc.append(request)
        if request['method'] == 'Player.Open':
            return json.dumps(behavior.rpc_result)
        addon = request['params']['addonid']
        return json.dumps({'result': {'addon': {'addonid': addon, 'enabled': behavior.enabled,
                                               'version': behavior.version}}})

    client = SimpleNamespace(descriptor=ServiceDescriptor(32123, 'a' * 48, 'b' * 64),
                             reserve_fixture_launch=reserve, create=create,
                             qualify_fixture_clock=qualify,
                             cancel_fixture_launch=calls.cancelled.append,
                             stop=calls.stopped.append)
    monkeypatch.setattr(runtime, '_service', lambda *args: client)
    monkeypatch.setattr(runtime, '_adaptive_ready', lambda *args: behavior.ready)
    monkeypatch.setattr(runtime.xbmc, 'executeJSONRPC', rpc, raising=False)

    def run(mode='launch_fixture', handle=-1, nonce=None):
        params = {'mode': mode, 'id': 'fixture-30045'}
        if nonce is not None:
            params['launch_nonce'] = nonce
        runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', str(handle),
                            '?' + urlencode(params)])

    return SimpleNamespace(runtime=runtime, state=state, calls=calls, behavior=behavior,
                           result=result, run=run)


def test_launcher_uses_named_video_player_and_never_default_or_resume(launch):
    launch.run()
    assert launch.calls.reserved == ['fixture-30045']
    request, = [item for item in launch.calls.rpc if item['method'] == 'Player.Open']
    assert request['params']['options'] == {'playername': 'VideoPlayer', 'resume': False}
    target = urlsplit(request['params']['item']['file'])
    assert target.scheme == 'plugin' and target.netloc == 'plugin.audio.kodi_donor_radio'
    assert parse_qs(target.query) == {'mode': ['play_fixture'], 'id': ['fixture-30045'],
                                    'launch_nonce': [NONCE]}
    assert launch.calls.created == launch.calls.qualified == launch.calls.cancelled == []
    assert not launch.state.players and not launch.state.resolved


def test_failed_player_open_revokes_reserved_launch(launch):
    launch.behavior.rpc_result = {'error': {'code': -32602, 'message': 'Invalid parameters'}}
    launch.run()
    assert launch.calls.cancelled == [NONCE]
    assert launch.calls.created == launch.calls.qualified == []
    assert launch.state.messages


@pytest.mark.parametrize('version,enabled', [('22.2.11', True), ('23.0.0', True),
                                           ('22.2.10', False)])
def test_launcher_rejects_unqualified_inputstream_versions(launch, version, enabled):
    launch.behavior.version, launch.behavior.enabled = version, enabled
    launch.run()
    assert not launch.calls.reserved
    assert not any(item['method'] == 'Player.Open' for item in launch.calls.rpc)


def test_resolver_qualifies_only_exact_selected_aac_after_preflight(launch):
    launch.run(mode='play_fixture', handle=7, nonce=NONCE)
    assert launch.calls.created == [('fixture-30045', NONCE)]
    assert launch.calls.qualified == [('c' * 48, NONCE)]
    assert launch.state.resolved[0][0] is True
    item = launch.state.resolved[0][1]
    assert item.path == AUDIO
    assert item.props['inputstream'] == 'inputstream.ffmpegdirect'
    assert item.props['inputstream.ffmpegdirect.stream_mode'] == 'timeshift'
    assert not launch.calls.cancelled and not launch.calls.stopped


def test_ordinary_fixture_resolution_cannot_qualify(launch):
    launch.run(mode='play_fixture', handle=7)
    assert launch.calls.created == [('fixture-30045', None)]
    assert not launch.calls.qualified
    assert launch.state.resolved[0][0] is True


@pytest.mark.parametrize('failure', ['preflight', 'disabled', 'version', 'no_master'])
def test_fallback_never_qualifies_launch(launch, failure):
    if failure == 'preflight':
        launch.behavior.ready = False
    elif failure == 'disabled':
        launch.behavior.enabled = False
    elif failure == 'version':
        launch.behavior.version = '22.2.11'
    else:
        launch.result.pop('adaptive_playback_url')
    launch.run(mode='play_fixture', handle=7, nonce=NONCE)
    assert not launch.calls.qualified
    assert launch.state.resolved[0][0] is True


def test_qualification_failure_closes_created_session(launch):
    launch.behavior.qualify_error = True
    launch.run(mode='play_fixture', handle=7, nonce=NONCE)
    assert launch.calls.stopped == ['c' * 48]
    assert launch.state.resolved[0][0] is False


def test_nonce_cannot_qualify_a_direct_script_play(launch):
    launch.run(mode='play_fixture', handle=-1, nonce=NONCE)
    assert not launch.calls.created and not launch.calls.qualified
    assert not launch.state.players


def test_developer_context_action_does_not_change_ordinary_fixture_entries(launch):
    runtime = launch.runtime
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', '7', '?mode=tests'])
    matched = [(url, item) for url, item, _ in launch.state.items
               if parse_qs(urlsplit(url).query).get('id') == ['fixture-30045']]
    url, item = matched[0]
    assert parse_qs(urlsplit(url).query)['mode'] == ['play_fixture']
    assert item.props['IsPlayable'] == 'true'
    assert item.context == [('localized-30173',
        'RunPlugin(plugin://plugin.audio.kodi_donor_radio?mode=launch_fixture&id=fixture-30045)')]


def test_invalid_ad_settings_do_not_hide_ordinary_fixture_menu(launch):
    launch.state.settings['ad_vast_url'] = ''
    launch.runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', '7', '?mode=tests'])
    entries = [item for url, item, _ in launch.state.items
               if parse_qs(urlsplit(url).query).get('id') == ['fixture-30045']]
    assert len(entries) == 1
    assert entries[0].props['IsPlayable'] == 'true'
    assert entries[0].context == []


def test_failed_native_resolution_retires_qualified_session(launch, monkeypatch):
    def resolve(handle, success, item):
        if success:
            raise RuntimeError('Kodi rejected resolved item')
        launch.state.resolved.append((False, item))
    monkeypatch.setattr(launch.runtime.xbmcplugin, 'setResolvedUrl', resolve)
    launch.run(mode='play_fixture', handle=7, nonce=NONCE)
    assert launch.calls.stopped == ['c' * 48]
    assert launch.state.resolved[0][0] is False


@pytest.mark.parametrize('identifier,path', [
    ('fixture-30174', '/source/late-break/index.m3u8'),
    ('fixture-30175', '/source/recovery/index.m3u8'),
])
def test_extended_qualification_scenarios_are_explicit_hls_fixtures(launch, identifier, path):
    station = launch.runtime._fixture_station(identifier)
    assert station.url == 'http://127.0.0.1:8765' + path
    assert station.codec == 'AAC' and station.hls is True

