"""Continuous fixture clocks remain exact-route diagnostic candidates."""
from types import SimpleNamespace
import json
import threading

import pytest

from ad_models import OutputSegment, Placement, Presentation, ResolvedAd, TrackingUrl
from events import JsonEventSink
from kodi_ad_player import KodiAdPlayback
from sessions import ContinuousAudioState


@pytest.fixture
def scenario(monkeypatch):
    state = SimpleNamespace(clock=.25, now=10.0, caching=False, playing=True,
                            audio=ContinuousAudioState(True, 480000, False))
    records, queued = [], []
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event)
        for event in ('impression', 'start', 'firstQuartile', 'midpoint',
                      'thirdQuartile', 'complete')), ())
    state.presentation = Presentation(
        (OutputSegment(0, 0, 'source', 0, 480000, None),),
        (Placement('placement', ad, 24000, 72000),), False)
    session = SimpleNamespace(
        id='session', cancel=threading.Event(), ad_enabled=True,
        station=SimpleNamespace(uuid='fixture-continuous'),
        source=SimpleNamespace(protocol='hls'),
        presentation=lambda: state.presentation, take_ad_errors=lambda: [],
        playback_url='http://127.0.0.1:8765/media/session/root.m3u8',
        continuous_playback_url='http://127.0.0.1:8765/media/session/root.aac',
        continuous_audio_state=lambda: state.audio)
    state.path = session.continuous_playback_url
    player = SimpleNamespace(isPlaying=lambda: state.playing,
                             getTime=lambda: state.clock,
                             getPlayingFile=lambda: state.path)

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, *args): queued.append(args); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    monkeypatch.setattr('kodi_ad_player.time', SimpleNamespace(monotonic=lambda: state.now))
    manager = SimpleNamespace(get=lambda identity: session if identity == session.id else None,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin='http://127.0.0.1:8765',
                             is_caching=lambda: state.caching)

    def sample(clock, elapsed=.25):
        state.clock = clock
        state.now += elapsed
        adapter.sample()
        return [record.details for record in records if record.kind == 'ad_clock_sample'][-1]

    yield SimpleNamespace(state=state, session=session, player=player, adapter=adapter,
                          records=records, queued=queued, sample=sample)
    adapter.close()


def test_continuous_clock_is_candidate_only_even_across_complete_ad(scenario):
    s = scenario
    s.adapter.bind(s.session.id)
    observations = [s.sample(clock) for clock in (.25, .5, .75, 1, 1.25, 1.5)]
    assert [item['mapped_position'] for item in observations] == [.25, .5, .75, 1, 1.25, 1.5]
    assert all(item['mapped'] and item['candidate_only'] and not item['tracking_enabled']
               and item['reason'] == 'continuous_clock_unaccepted' for item in observations)
    assert s.adapter.mapping_valid is True
    assert s.adapter.last_sample.mapped is False
    assert s.queued == []
    assert 'http://' not in repr(observations)


def test_continuous_candidate_label_survives_json_event_serialization(scenario, tmp_path):
    s = scenario
    debug = []
    s.adapter.manager.sink = JsonEventSink(tmp_path, True, debug.append)
    s.adapter.bind(s.session.id)
    s.adapter.sample()
    record = json.loads((tmp_path / 'events.jsonl').read_text())
    assert record == json.loads(debug[0])
    assert record['kind'] == 'ad_clock_sample'
    assert record['details']['candidate_only'] is True
    assert record['details']['mapped_position'] == .25
    assert record['details']['tracking_enabled'] is False
    assert record['details']['reason'] == 'continuous_clock_unaccepted'


def test_continuous_sample_zero_survives_retired_hls_window(scenario):
    s = scenario
    s.state.audio = ContinuousAudioState(True, 300 * 48000, False)
    s.state.presentation = Presentation(
        (OutputSegment(100, 4, 'retained', 200 * 48000, 96256, None),), (), False)
    s.adapter.bind(s.session.id)
    assert s.sample(201)['mapped_position'] == 201
    s.state.presentation = Presentation(
        (OutputSegment(101, 4, 'new-retained', 202 * 48000, 96256, None),), (), False)
    assert s.sample(201.25)['mapped_position'] == 201.25
    assert s.adapter.mapping_valid is True
    assert s.queued == []


def test_same_session_hls_never_upgrades_when_another_reader_starts_aac(scenario):
    s = scenario
    s.state.path = s.session.playback_url
    s.adapter.bind(s.session.id)
    assert s.sample(.25)['reason'] == 'live_clock_unanchored'
    s.state.path = s.session.continuous_playback_url
    s.adapter.bind(s.session.id)
    observation = s.sample(.5)
    assert observation['mapped'] is False
    assert observation['reason'] == 'live_clock_unanchored'
    assert 'candidate_only' not in observation
    assert s.queued == []


@pytest.mark.parametrize('changed_path', [
    'http://127.0.0.1:8765/media/session/root.m3u8',
    'http://127.0.0.1:8765/media/session/root.aac?other=1',
    'http://127.0.0.1:8765/media/other/root.aac',
])
def test_continuous_route_change_permanently_invalidates_candidate(scenario, changed_path):
    s = scenario
    s.adapter.bind(s.session.id)
    assert s.sample(.25)['mapped'] is True
    s.state.path = changed_path
    assert s.sample(.5)['reason'] == 'continuous_route_changed'
    s.state.path = s.session.continuous_playback_url
    observation = s.sample(.75)
    assert observation['mapped'] is False
    assert observation['reason'] == 'continuous_route_changed'


def test_unreadable_playing_file_cannot_bind_continuous_anchor(scenario):
    s = scenario
    s.player.getPlayingFile = lambda: (_ for _ in ()).throw(RuntimeError('not playing'))
    s.adapter.bind(s.session.id)
    s.player.getPlayingFile = lambda: s.session.continuous_playback_url
    observation = s.sample(.25)
    assert observation['mapped'] is False
    assert observation['reason'] == 'live_clock_unanchored'


def test_continuous_candidate_waits_for_first_complete_delivery(scenario):
    s = scenario
    s.state.audio = ContinuousAudioState(False, 0, False)
    s.adapter.bind(s.session.id)
    observation = s.sample(.25)
    assert observation['mapped'] is False
    assert observation['reason'] == 'continuous_audio_pending'
    s.state.audio = ContinuousAudioState(True, 96256, False)
    assert s.sample(.5)['mapped_position'] == .5


@pytest.mark.parametrize('seconds, mapped', [(1, True), (1 + 1024 / 48000, True), (1.022, False)])
def test_delivered_ceiling_allows_only_one_aac_frame_tolerance(scenario, seconds, mapped):
    s = scenario
    s.state.audio = ContinuousAudioState(True, 48000, False)
    s.adapter.bind(s.session.id)
    observation = s.sample(seconds)
    assert observation['mapped'] is mapped
    if not mapped:
        assert observation['reason'] == 'continuous_clock_ahead_of_media'
        s.state.audio = ContinuousAudioState(True, 96000, False)
        assert s.sample(1.1)['mapped'] is False


def test_terminal_transport_cannot_continue_candidate_mapping(scenario):
    s = scenario
    s.adapter.bind(s.session.id)
    assert s.sample(.25)['mapped'] is True
    s.state.audio = ContinuousAudioState(True, 480000, True)
    observation = s.sample(.5)
    assert observation['mapped'] is False
    assert observation['reason'] == 'continuous_audio_terminal'


def test_delivered_ceiling_cannot_rewind(scenario):
    s = scenario
    s.adapter.bind(s.session.id)
    assert s.sample(.25)['mapped'] is True
    s.state.audio = ContinuousAudioState(True, 96000, False)
    observation = s.sample(.5)
    assert observation['mapped'] is False
    assert observation['reason'] == 'continuous_media_rewound'


def test_backward_clock_after_confirmed_start_is_permanently_rejected(scenario):
    s = scenario
    s.adapter.bind(s.session.id)
    s.sample(.25)
    s.sample(.5)
    observation = s.sample(.3)
    assert observation['mapped'] is False
    assert observation['reason'] == 'continuous_clock_reset'
    assert s.sample(.75)['mapped'] is False


def test_seek_invalidates_continuous_anchor(scenario):
    s = scenario
    s.adapter.bind(s.session.id)
    s.sample(.25)
    s.adapter.on_seek(s.session.id)
    assert s.sample(.5)['mapped'] is False
    assert s.sample(.75)['mapped'] is False


@pytest.mark.parametrize('transition', ['pause', 'cache', 'speed'])
def test_continuous_candidate_never_bypasses_playback_gates(scenario, transition):
    s = scenario
    s.adapter.bind(s.session.id)
    s.sample(.25)
    s.sample(.5)
    if transition == 'pause':
        s.adapter.on_pause(s.session.id)
    elif transition == 'cache':
        s.state.caching = True
    else:
        s.adapter.on_speed_changed(.5, s.session.id)
    observation = s.sample(.75)
    assert observation['tracking_enabled'] is False
    assert s.adapter.last_sample.mapped is False
    assert s.queued == []
