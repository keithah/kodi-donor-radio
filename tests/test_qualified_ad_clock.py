"""Success evidence requires the owned, explicit fixture launch and stable reads."""
from dataclasses import replace

import pytest

from test_continuous_ad_clock import scenario


def qualify(s, av=True):
    s.session.fixture_clock_qualified = True
    s.adapter.bind(s.session.id)
    if av:
        s.adapter.on_av_started(s.session.id)


def events(s):
    return [item[1] for item in s.queued]


def test_qualified_owned_start_tracks_once(scenario):
    s = scenario
    qualify(s)
    for clock in (0, .25, .5, .75, 1, 1.25, 1.5, 1.75):
        s.sample(clock)
    assert events(s) == ['impression', 'start', 'firstQuartile', 'midpoint',
                         'thirdQuartile', 'complete']
    sample = s.sample(2)
    assert sample['candidate_only'] is False and sample['tracking_enabled'] is True


def test_reservation_without_owned_av_start_cannot_track(scenario):
    s = scenario
    qualify(s, av=False)
    for clock in (0, .25, .5, .75, 1, 1.25, 1.5):
        s.sample(clock)
    assert not events(s)


@pytest.mark.parametrize('callback', ['on_av_started', 'on_playback_started'])
def test_restart_of_same_url_revokes_qualification(scenario, callback):
    s = scenario
    qualify(s)
    s.sample(0)
    s.sample(.25)
    getattr(s.adapter, callback)(s.session.id)
    for clock in (.5, .75, 1, 1.25, 1.5):
        assert not s.sample(clock)['tracking_enabled']
    assert not events(s)


@pytest.mark.parametrize('fault', ['url', 'session', 'cancel', 'slow', 'pause', 'close'])
def test_changed_observation_cannot_emit_crossing(scenario, fault):
    s = scenario
    qualify(s)
    s.sample(0)
    s.sample(.25)
    def clock():
        if fault == 'url': s.state.path = s.session.playback_url
        if fault == 'session': s.adapter.manager.get = lambda _: None
        if fault == 'cancel': s.session.cancel.set()
        if fault == 'slow': s.state.now += 1
        if fault == 'pause': s.adapter.on_pause(s.session.id)
        if fault == 'close': s.adapter.close()
        return .5
    s.player.getTime = clock
    s.sample(.5)
    assert not events(s)
    assert not s.adapter.mapping_valid


def test_pause_taints_interrupted_ad_but_later_clean_ad_can_track(scenario):
    s = scenario
    first = s.state.presentation.placements[0]
    later = replace(first, id='later', start_sample=120000, end_sample=168000)
    s.state.presentation = replace(s.state.presentation, placements=(first, later))
    qualify(s)
    for clock in (0, .25, .5, .75): s.sample(clock)
    assert events(s) == ['impression', 'start', 'firstQuartile']
    s.adapter.on_pause(s.session.id)
    s.sample(.8, elapsed=3)
    s.adapter.on_resume(s.session.id)
    for clock in (.85, .95, 1.2, 1.45, 1.7, 1.95, 2.2, 2.45, 2.5, 2.75, 3, 3.25, 3.5):
        s.sample(clock)
    assert [item[1] for item in s.queued if item[0] == first.id] == [
        'impression', 'start', 'firstQuartile']
    assert [item[1] for item in s.queued if item[0] == later.id] == [
        'impression', 'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete']


def test_qualified_zero_start_preroll_still_suppressed(scenario):
    s = scenario
    placement = replace(s.state.presentation.placements[0], start_sample=0, end_sample=48000)
    s.state.presentation = replace(s.state.presentation, placements=(placement,))
    qualify(s)
    for clock in (0, .25, .5, .75, 1): s.sample(clock)
    assert not events(s)


def test_runtime_distinguishes_playback_start_from_owned_av_start(kodi):
    from types import SimpleNamespace
    runtime, _, _ = kodi
    player = runtime.RelayPlayer(SimpleNamespace(stop=lambda _: None, get=lambda _: None), 32123)
    identity = 'a' * 48
    player.file = 'http://127.0.0.1:32123/media/' + identity + '/' + 'b' * 48 + '.aac'
    player.playing = True
    calls = []
    player.ads.on_playback_started = lambda session: calls.append(('start', session))
    player.ads.on_av_started = lambda session: calls.append(('av', session))
    player.onPlayBackStarted()
    assert calls == [('start', identity)]
    player.onAVStarted()
    assert calls == [('start', identity), ('av', identity)]


def test_close_during_second_url_read_does_not_touch_retired_tracker(scenario):
    s = scenario
    qualify(s)
    s.sample(0)
    reads = []
    original = s.player.getPlayingFile
    def path():
        value = original()
        reads.append(value)
        if len(reads) == 2:
            s.adapter.close()
        return value
    s.player.getPlayingFile = path
    s.sample(.25)
    assert not events(s)
    assert s.adapter.session is None


def test_resume_without_observed_pause_taints_the_interrupted_ad(scenario):
    # Python callbacks can arrive after the current player state has resumed,
    # causing the runtime to reject the earlier pause-state observation.
    s = scenario
    qualify(s)
    for clock in (0, .25, .5, .75): s.sample(clock)
    before = events(s)
    s.adapter.on_resume(s.session.id)
    for clock in (1, 1.25, 1.5, 1.75): s.sample(clock)
    assert events(s) == before


def test_failed_native_clock_after_close_does_not_touch_retired_tracker(scenario):
    s = scenario
    qualify(s)
    def clock():
        s.adapter.close()
        raise RuntimeError('Kodi is not playing any media file')
    s.player.getTime = clock
    s.adapter.sample()
    assert s.adapter.session is None
    assert not events(s)


def test_cache_after_confirmed_progress_permanently_disables_current_and_later_ads(scenario):
    s = scenario
    first = s.state.presentation.placements[0]
    later = replace(first, id='later', start_sample=120000, end_sample=168000)
    s.state.presentation = replace(s.state.presentation, placements=(first, later))
    qualify(s)
    s.sample(0)
    s.sample(.25)
    s.state.caching = True
    observed = s.sample(.3)
    assert observed['raw_clock'] == .3
    assert observed['reason'] == 'continuous_clock_cache'
    assert observed['mapped'] is False and observed['mapped_position'] is None
    assert observed['candidate_only'] is True and observed['tracking_enabled'] is False
    s.state.caching = False
    s.adapter.on_resume(s.session.id)
    for clock in (.4, .5, .75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3, 3.25, 3.5):
        observed = s.sample(clock)
        assert observed['raw_clock'] == clock
        assert observed['reason'] == 'continuous_clock_cache'
        assert observed['mapped'] is False and observed['tracking_enabled'] is False
    assert not events(s)


def test_startup_cache_before_confirmed_progress_allows_later_clean_ad(scenario):
    s = scenario
    qualify(s)
    s.sample(0)
    s.state.caching = True
    buffered = s.sample(.1, elapsed=2)
    assert buffered['tracking_enabled'] is False
    assert buffered.get('reason') != 'continuous_clock_cache'
    s.state.caching = False
    for clock in (.1, .25, .5, .75, 1, 1.25, 1.5):
        s.sample(clock)
    assert events(s) == ['impression', 'start', 'firstQuartile', 'midpoint',
                         'thirdQuartile', 'complete']


@pytest.mark.parametrize('resumed', [False, True])
def test_cache_during_pause_or_resume_rearming_permanently_revokes_clock(scenario, resumed):
    s = scenario
    qualify(s)
    s.sample(0)
    s.sample(.25)
    s.adapter.on_pause(s.session.id)
    if resumed:
        s.adapter.on_resume(s.session.id)
    s.state.caching = True
    observed = s.sample(.3)
    assert observed['reason'] == 'continuous_clock_cache'
    assert observed['mapped'] is False and observed['tracking_enabled'] is False
    s.state.caching = False
    s.adapter.on_resume(s.session.id)
    for clock in (.4, .5, .75, 1, 1.25, 1.5):
        observed = s.sample(clock)
        assert observed['reason'] == 'continuous_clock_cache'
        assert observed['mapped'] is False and observed['tracking_enabled'] is False
    assert not events(s)


@pytest.mark.parametrize('reader', ['clock', 'cache'])
def test_failed_established_read_permanently_revokes_qualification(scenario, reader):
    s = scenario
    qualify(s)
    for clock in (0, .25, .5, .75): s.sample(clock)
    before = events(s)
    def unavailable():
        raise RuntimeError('Playback observation unavailable')
    original = s.player.getTime if reader == 'clock' else s.adapter.is_caching
    if reader == 'clock': s.player.getTime = unavailable
    else: s.adapter.is_caching = unavailable
    observed = s.sample(.8)
    assert observed['reason'] == 'continuous_observation_unavailable'
    assert not observed['mapped'] and not observed['tracking_enabled']
    if reader == 'clock': s.player.getTime = original
    else: s.adapter.is_caching = original
    for clock in (.9, 1, 1.25, 1.5, 1.75):
        observed = s.sample(clock)
        assert not observed['tracking_enabled']
    assert events(s) == before
