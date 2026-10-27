# SPDX-License-Identifier: GPL-2.0-or-later
"""Acceptance checks must reject a plausible but incorrect playback transcript."""
import json
import pytest

from tools.smoke_ad_relay import check_trace
from tools import smoke_ad_kodi


def valid():
    return {
        'sections': ['preroll', 'programme_a', 'programme_a', 'replacement',
                     'replacement', 'programme_b', 'programme_b'],
        'expected_sections': ['preroll', 'programme_a', 'programme_a', 'replacement',
                              'replacement', 'programme_b', 'programme_b'],
        'samples': [96256] * 7,
        'expected_samples': [96256] * 7,
        'events': [],
        'stopped_at': None,
    }


def test_valid_unmapped_trace_has_no_success_beacons():
    assert check_trace(valid()) == []


def test_relay_smoke_tone_map_covers_every_described_segment():
    import json
    from pathlib import Path
    from tools import smoke_ad_relay

    source_file = Path(__file__).parents[1] / 'tools' / 'ad_audio_sources.json'
    segments = json.loads(source_file.read_text())['segments']
    expected = {item['tone_hz']: item['section'] for item in segments.values()}
    assert smoke_ad_relay.TONE_SECTION == expected
    assert smoke_ad_relay.CHOICES == tuple(sorted(expected))


def test_original_break_leakage_fails():
    trace = valid(); trace['sections'][3] = 'original_break'
    assert any('section' in item for item in check_trace(trace))


def test_missing_programme_fails():
    trace = valid(); trace['sections'].pop()
    assert any('segment count' in item for item in check_trace(trace))


def test_invalid_pts_measurement_is_reported_instead_of_aborting_trace():
    trace = valid()
    trace['pts_steps_valid'] = [True, False]
    assert any('PTS' in item for item in check_trace(trace))


def test_duplicate_event_fails():
    trace = valid(); trace['events'] = [
        {'event': 'impression', 'placement_id': 'a', 'at': 1.0},
        {'event': 'impression', 'placement_id': 'a', 'at': 1.1},
    ]
    assert any('duplicate' in item for item in check_trace(trace))


def test_completion_after_stop_fails():
    trace = valid(); trace['stopped_at'] = 2.0
    trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 2.5}]
    assert any('after stop' in item for item in check_trace(trace))


def test_unmapped_success_beacon_fails():
    trace = valid(); trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 1.5}]
    assert any('playback evidence' in item for item in check_trace(trace))


def test_claimed_mapping_cannot_make_relay_only_success_pass():
    trace = valid(); trace['mapping_proven'] = True
    trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 1.5}]
    assert any('playback evidence' in item for item in check_trace(trace))


def test_relay_smoke_reports_segment_validation_failure_and_continues(monkeypatch):
    from types import SimpleNamespace
    from tools import smoke_ad_relay

    session = SimpleNamespace(id='session', playback_url='http://fixture.test/play', ad=None)
    stopped, closed = [], []

    class Manager:
        def __init__(self, *args, **kwargs): pass
        def create(self, station): return session
        def stop(self, session_id): stopped.append(session_id)

    class Relay:
        def __init__(self, manager):
            self.descriptor = SimpleNamespace(port=8766)
        def start(self): pass
        def close(self): closed.append(True)

    monkeypatch.setattr(smoke_ad_relay, 'SessionManager', Manager)
    monkeypatch.setattr(smoke_ad_relay, 'RelayServer', Relay)

    def fetch(url):
        if url.endswith('/reset'):
            return b''
        if url == session.playback_url:
            return ('#EXTM3U\n' + ''.join('/segment%d.ts\n' % i for i in range(6))).encode()
        return b'transport-stream'

    monkeypatch.setattr(smoke_ad_relay, '_bytes', fetch)
    monkeypatch.setattr(smoke_ad_relay, '_json', lambda _url: {
        'beacons': {}, 'requests': []})
    calls = []
    def fail_probe(_path, executable):
        assert executable == 'custom-ffprobe'
        calls.append(True)
        if len(calls) == 1:
            raise ValueError('bad segment')
        return {'samples': 96256, 'first_pts': 0, 'pts_steps_ok': True}
    monkeypatch.setattr(smoke_ad_relay, 'probe', fail_probe)
    monkeypatch.setattr(smoke_ad_relay, 'decoded_tone',
                        lambda *_args: smoke_ad_relay.CHOICES[0])
    trace = smoke_ad_relay.observe('http://fixture.test', 'normal', 'direct', False, False,
                                   ffmpeg='custom-ffmpeg', ffprobe='custom-ffprobe')
    assert stopped == [session.id]
    assert closed == [True]
    assert len(calls) == 6
    assert trace['sections'][0] is None
    assert trace['samples'][0] is None
    assert any('segment 0 decode failed: ValueError' in failure
               for failure in trace['failures'])


def test_kodi_smoke_rejects_unrelated_item_even_with_advancing_clock():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': False, 'time_seconds': 1.0},
        {'playing': True, 'item_matches_expected': False, 'time_seconds': 3.0}]}
    assert any('item' in item for item in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_ignores_previous_player_until_requested_fixture_starts():
    result = {'source': 'normal', 'fixture': {'requests': {'/source/normal/index.m3u8': 1}},
              'timeline': [
                  {'playing': True, 'item_matches_expected': False, 'time_seconds': 5.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.2},
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.4, 'at': 0.6},
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.8, 'at': 1.0}]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_rejects_frozen_clock_on_expected_item():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0}]}
    assert any('advance' in item for item in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_accepts_expected_item_with_advancing_clock():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.4},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.8}]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_requires_the_requested_loopback_source_manifest():
    result = {'source': 'normal', 'fixture': {'requests': {'/source/mutation/index.m3u8': 1}},
              'timeline': [
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.6, 'at': 0.6},
              ]}
    assert any('expected fixture source' in item
               for item in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_uses_wall_time_for_slow_json_rpc_samples():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.2, 'at': 1.2},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 2.4, 'at': 2.4}]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_accumulates_elapsed_time_across_clock_poll_plateaus():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.25},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.5},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0, 'at': 0.75},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0, 'at': 1.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0, 'at': 1.25},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 2.0, 'at': 1.5},
    ]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_ignores_negative_startup_clock_samples():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': -0.09, 'at': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.25},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.5},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.75, 'at': 1.0},
    ]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_accepts_requested_item_when_successful_stop_precedes_clock_progress():
    result = {'source': 'normal', 'actions': [{'action': 'stop', 'accepted': True,
                                               'state_observed': True}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.5},
                  {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.75},
              ]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_smoke_fails_when_a_requested_player_action_is_rejected():
    result = {'source': 'normal', 'actions': [
        {'action': 'pause', 'accepted': False},
        {'action': 'resume', 'accepted': True},
        {'action': 'seek', 'accepted': False},
    ], 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0, 'at': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.6, 'at': 0.6},
    ]}
    failures = smoke_ad_kodi.validate_observation(result)
    assert any('pause' in failure and 'rejected' in failure for failure in failures)
    assert any('seek' in failure and 'rejected' in failure for failure in failures)


def test_kodi_smoke_fails_when_player_accepts_an_action_without_its_effect():
    result = {'source': 'finite', 'actions': [
        {'action': 'pause', 'accepted': True, 'state_observed': False},
        {'action': 'resume', 'accepted': True, 'state_observed': True},
        {'action': 'seek', 'accepted': True, 'state_observed': False},
    ], 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0, 'at': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 2.0, 'at': 1.0},
    ]}
    failures = smoke_ad_kodi.validate_observation(result)
    assert any('pause' in failure and 'effect' in failure for failure in failures)
    assert any('seek' in failure and 'effect' in failure for failure in failures)


def test_kodi_smoke_fails_when_expected_beacons_are_missing():
    result = {'source': 'finite',
              'expected_beacons': {'placement-1/impression': 1,
                                   'placement-1/start': 1,
                                   'placement-1/complete': 1},
              'fixture': {'beacons': {'placement-1/impression': 1,
                                      'placement-1/start': 1}},
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.6, 'at': 0.6},
              ]}
    assert any('beacon counts' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_fails_when_a_requested_action_was_never_attempted():
    result = {'source': 'finite', 'requested_actions': ['pause', 'resume'],
              'actions': [{'action': 'pause', 'accepted': True, 'state_observed': True}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.6, 'at': 0.6},
              ]}
    assert any('attempt every requested action' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_fails_when_stop_is_accepted_but_player_remains_active():
    result = {'source': 'normal', 'requested_actions': ['stop'],
              'actions': [{'action': 'stop', 'accepted': True, 'state_observed': False}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 0.0, 'at': 0.5},
              ]}
    assert any('stop' in failure and 'effect' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_fails_when_clock_advances_between_pause_and_resume():
    result = {'source': 'finite', 'requested_actions': ['pause', 'resume'],
              'actions': [
                  {'action': 'pause', 'at': 1.0, 'accepted': True, 'state_observed': True},
                  {'action': 'resume', 'at': 4.0, 'accepted': True, 'state_observed': True},
              ], 'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 1.0, 'speed': 0, 'at': 1.5},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 2.0, 'speed': 0, 'at': 2.5},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 3.0, 'speed': 0, 'at': 3.5},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 3.5, 'speed': 1, 'at': 4.5},
              ]}
    assert any('clock advanced while paused' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_does_not_count_natural_progress_as_a_seek():
    result = {'source': 'finite', 'requested_actions': ['seek'],
              'actions': [{'action': 'seek', 'at': 8.0, 'accepted': True,
                           'state_observed': True,
                           'seek_evidence': {'before_position': 11.4,
                                             'after_position': 11.9,
                                             'total_seconds': 16.0,
                                             'target_percentage': 80,
                                             'elapsed_seconds': .5}}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 11.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 11.6, 'at': 0.6},
              ]}
    assert any('seek' in failure and 'effect' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_does_not_count_natural_progress_near_seek_target():
    result = {'source': 'finite', 'requested_actions': ['seek'],
              'actions': [{'action': 'seek', 'at': 8.0, 'accepted': True,
                           'state_observed': True,
                           'seek_evidence': {'before_position': 9.4,
                                             'after_position': 11.9,
                                             'total_seconds': 16.0,
                                             'target_percentage': 80,
                                             'elapsed_seconds': 2.5}}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 9.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 9.6, 'at': 0.6},
              ]}
    assert any('seek' in failure and 'effect' in failure
               for failure in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_accepts_seek_jump_beyond_elapsed_playback():
    result = {'source': 'finite', 'requested_actions': ['seek'],
              'actions': [{'action': 'seek', 'at': 8.0, 'accepted': True,
                           'state_observed': True,
                           'seek_evidence': {'before_position': 8.2,
                                             'after_position': 12.833,
                                             'total_seconds': 16.042,
                                             'target_percentage': 80,
                                             'elapsed_seconds': 1.5}}],
              'timeline': [
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 8.0, 'at': 0.0},
                  {'playing': True, 'item_matches_expected': True,
                   'time_seconds': 8.6, 'at': 0.6},
              ]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_decoder_probe_reports_counts_before_fixture_validation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools import generate_ad_audio

    results = iter((
        SimpleNamespace(stdout=json.dumps({
            'streams': [{'codec_name': 'aac', 'sample_rate': '48000', 'channels': 2}],
            'packets': [{'pts': '0'}, {'pts': '200'}]})),
        SimpleNamespace(stdout=json.dumps({'frames': [{'nb_samples': '1024'}]})),
    ))
    monkeypatch.setattr(generate_ad_audio, 'run', lambda *_args, **_kwargs: next(results))
    info = generate_ad_audio.probe(tmp_path / 'short.ts', 'fake-ffprobe')
    assert info['aac_frames'] == 2
    assert info['decoded_frames'] == 1
    assert not info['pts_steps_ok']
    with pytest.raises(ValueError, match='94'):
        generate_ad_audio.validate_probe(info, tmp_path / 'short.ts')


def test_kodi_smoke_accepts_only_loopback_relay_media_item():
    from tools.smoke_ad_kodi import _item_matches_expected

    expected = 'plugin://plugin.audio.kodi_donor_radio/?action=play'
    route = 'http://127.0.0.1:8767/media/' + 'a' * 48 + '/' + 'b' * 48 + '.ts'
    assert _item_matches_expected(route, expected)
    assert not _item_matches_expected(expected, expected, previous_file=expected)
    assert not _item_matches_expected(route, expected, previous_file=route)
    assert _item_matches_expected(route, expected, previous_file='http://127.0.0.1:8767/old-item')
    assert not _item_matches_expected(route.replace('127.0.0.1', 'example.com'), expected)
    assert not _item_matches_expected('http://127.0.0.1:8767/media/not-an-id', expected)


@pytest.mark.parametrize(('source', 'fixture_id'), [
    ('normal', 'fixture-30045'), ('mutation', 'fixture-30046'),
    ('expired-window', 'fixture-30047'), ('stall', 'fixture-30048'),
    ('finite', 'fixture-30049'),
])
def test_kodi_smoke_menu_matches_expected_fixture_id(monkeypatch, source, fixture_id):
    expected = 'plugin://plugin.audio.kodi_donor_radio/?mode=play_fixture&id=' + fixture_id
    wrong = 'plugin://plugin.audio.kodi_donor_radio/?mode=play_fixture&id=fixture-30067'
    requests = []
    def rpc(method, params, **kwargs):
        requests.append((method, params, kwargs))
        return {'files': [{'file': wrong, 'label': 'Wrong fixture'},
                          {'file': expected, 'label': 'Expected fixture'}]}
    monkeypatch.setattr(smoke_ad_kodi, 'rpc', rpc)
    assert smoke_ad_kodi._menu_file(9090, source) == (expected, 'Expected fixture')
    assert requests[0][1]['directory'] == 'plugin://plugin.audio.kodi_donor_radio/?mode=tests'


def test_kodi_smoke_menu_rejects_unknown_source(monkeypatch):
    monkeypatch.setattr(smoke_ad_kodi, 'rpc',
                        lambda *_args, **_kwargs: pytest.fail('rpc before source validation'))
    with pytest.raises(ValueError, match='Unknown controlled source'):
        smoke_ad_kodi._menu_file(9090, 'unexpected')


def test_kodi_rpc_ignores_async_notification_before_matching_response(monkeypatch):
    class Socket:
        def __init__(self):
            self.parts = [b'{"jsonrpc":"2.0","method":"Player.OnPause","params":{}}\n'
                          b'{"jsonrpc":"2.0","id":1,"result":{"Player.Caching":true}}\n']
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, *_): pass
        def sendall(self, *_): pass
        def recv(self, *_): return self.parts.pop(0) if self.parts else b''
    monkeypatch.setattr(smoke_ad_kodi.socket, 'create_connection', lambda *_args, **_kwargs: Socket())
    assert smoke_ad_kodi.rpc('XBMC.GetInfoBooleans') == {'Player.Caching': True}
