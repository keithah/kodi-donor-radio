# SPDX-License-Identifier: GPL-2.0-or-later
"""ICY marker syntax and bounded observational inference."""
import pytest

from icy_markers import IcyMarkerTracker, MarkerSignal, parse_marker


@pytest.mark.parametrize('title, kind, duration', [
    ('AD_START', 'start', None),
    (' ad_start_30 ', 'start', 30),
    ('AD_START_300', 'start', 300),
    ('AD_STOP', 'end', None),
    (' ad_end ', 'end', None),
])
def test_exact_commands_are_explicit(title, kind, duration):
    assert parse_marker(title) == MarkerSignal(kind, title.strip().upper(),
                                                'ad_command', 'explicit', duration)


@pytest.mark.parametrize('title', [
    'AD_START_0', 'AD_START_301', 'AD_START_-1', 'AD_START_1.5',
    'AD_START_999999999999999999999', 'XAD_START', 'AD_START_EXTRA',
    'AD_START now', 'A D_START', 'AD_ENDING',
])
def test_malformed_commands_are_not_explicit(title):
    assert parse_marker(title) is None


@pytest.mark.parametrize('title, kind, duration', [
    ('Break ~ Break ~ 60 ~ 4 ^', 'start', 60),
    ('Break ~ Break ~ 30 ~ 4 ~ I ^', 'start', 30),
    ('Break ~ Break ~ 30 ~ 4 ~ I ~ 3 ^', 'start', 30),
    ('Artist ~ Song ~ 246 ~ 0 ^', 'end', 246),
    ('##Ignored ~ Event ~ 300 ~ 0 ^', 'end', 300),
    ('##Ignored - Event', 'end', None),
])
def test_terminated_tildeats_and_provider_end_break(title, kind, duration):
    assert parse_marker(title) == MarkerSignal(kind, title, 'tildeats',
                                                'explicit', duration)


@pytest.mark.parametrize('title', [
    'Break ~ Break ~ 60 ~ 4', 'Break~Break~60~4^',
    'Break ~ Break ~ -1 ~ 4 ^', 'Break ~ Break ~ 301 ~ 4 ^',
    'Break ~ Break ~ 60 ~ 4 ~ X ^', 'Break ~ Break ~ 60 ~ 4 ~ I ~ 0 ^',
    'Artist ~ Song ~ 60 ~ 0 ~ I ^',
    'Break ~ Break ~ 60 ~ 11 ^', '##', '##Break ~ Break ~ 60 ~ 4',
    'Break ~ Break ~ 60 ~ 4 ^ trailing',
])
def test_malformed_or_unsupported_provider_forms_are_not_explicit(title):
    assert parse_marker(title) is None


def tracker(learned=None):
    events = []
    instance = IcyMarkerTracker('session-1', 'station-1', events.append,
                                lambda marker: (learned or {}).get(marker))
    return instance, events


def observe(instance, title, seconds):
    instance.observe({'StreamTitle': title}, '2026-10-23T00:00:00Z', seconds)


@pytest.mark.parametrize('title', ['-', '#', '*', '---', 'AD', 'AD BREAK',
                                   'COMMERCIAL', 'SPOT', 'START', 'STOP'])
def test_exact_marker_vocabulary_or_punctuation_is_candidate(title):
    instance, events = tracker()
    observe(instance, title, 1)
    assert [event.kind for event in events] == ['icy_marker_candidate', 'break_start']
    assert events[0].details['classification'] == 'candidate'
    assert events[1].details['classification'] == 'inferred'
    assert instance.latest_candidate() == title


@pytest.mark.parametrize('title', ['Song', 'A - Song', 'Great ad song',
                                   'Advertisement', 'SPOTLIGHT', '', '  '])
def test_ordinary_titles_and_substrings_do_not_start_breaks(title):
    instance, events = tracker()
    observe(instance, title, 1)
    assert events == []
    assert instance.latest_candidate() is None


def test_return_to_track_closes_inferred_break_inside_window():
    instance, events = tracker()
    observe(instance, 'AD BREAK', 1)
    observe(instance, 'Artist - Song', 300)
    assert [event.kind for event in events] == [
        'icy_marker_candidate', 'break_start', 'break_end']
    assert events[-1].details['classification'] == 'inferred'
    assert events[-1].session_id == 'session-1'
    assert events[-1].station_uuid == 'station-1'
    assert events[-1].protocol == 'icy'


def test_same_marker_after_intervening_marker_closes_break():
    instance, events = tracker()
    observe(instance, '-', 1)
    observe(instance, '#', 2)
    observe(instance, '-', 3)
    assert [event.kind for event in events].count('break_end') == 1
    assert events[-1].details['classification'] == 'inferred'


def test_consecutive_duplicate_title_cannot_close_or_restart_break():
    instance, events = tracker()
    observe(instance, '-', 1)
    observe(instance, '-', 2)
    observe(instance, 'Artist - Song', 3)
    observe(instance, 'Artist - Song', 4)
    assert [event.kind for event in events] == [
        'icy_marker_candidate', 'break_start', 'break_end']


def test_expiry_emits_unresolved_once_without_synthetic_end():
    instance, events = tracker()
    observe(instance, '-', 1)
    instance.expire(301)
    assert [event.kind for event in events] == ['icy_marker_candidate', 'break_start']
    instance.expire(302)
    observe(instance, 'Artist - Song', 303)
    instance.expire(900)
    assert [event.kind for event in events] == [
        'icy_marker_candidate', 'break_start', 'break_unresolved']
    assert events[-1].details['reason'] == 'timeout'


def test_learned_exact_marker_overrides_inference():
    instance, events = tracker({'-': 'end'})
    observe(instance, '-', 1)
    observe(instance, 'Artist - Song', 2)
    assert [event.kind for event in events] == ['break_end']
    assert events[0].details['classification'] == 'learned'
    assert instance.latest_candidate() is None


def test_repeated_explicit_boundaries_do_not_emit_duplicate_transitions():
    instance, events = tracker()
    for number, title in enumerate(('AD_START', 'Song', 'AD_START',
                                    'AD_END', 'Song 2', 'AD_END'), 1):
        observe(instance, title, number)
    assert [event.kind for event in events] == ['break_start', 'break_end']


def test_symbol_that_is_not_punctuation_is_not_a_candidate():
    instance, events = tracker()
    observe(instance, '🎵', 1)
    assert events == []


def test_retention_is_bounded_by_age_and_count():
    instance, _ = tracker()
    for number in range(100):
        observe(instance, 'Song %s' % number, number)
    assert len(instance.records) == 64
    instance.expire(500)
    assert len(instance.records) == 0
