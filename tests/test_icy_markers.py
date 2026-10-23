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


@pytest.mark.parametrize('title,kind', [
    ('#AD_START#', 'start'), ('*ad_end*', 'end'), ('-AD_STOP-', 'end'),
])
def test_exactly_wrapped_commands_are_explicit(title, kind):
    signal = parse_marker(title)
    assert signal == MarkerSignal(kind, title.strip().upper(),
                                  'ad_command', 'explicit', None)


@pytest.mark.parametrize('title', [
    '##AD_START#', '# AD_START #', '#AD_START# trailing',
    '#XAD_START#', '#AD_START_EXTRA#', '#AD_START_301#',
])
def test_wrapped_commands_require_one_exact_known_command(title):
    assert parse_marker(title) is None


def test_wrapped_command_is_explicit_in_tracker():
    instance, events = tracker()
    observe(instance, '#AD_START#', 1)
    assert [event.kind for event in events] == ['break_start']
    assert events[0].details['classification'] == 'explicit'
    assert events[0].evidence['StreamTitle'] == '#AD_START#'


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
                                lambda marker: (learned if learned is not None else {}).get(marker))
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


def test_new_learned_label_applies_to_next_identical_metadata_block():
    learned = {}
    instance, events = tracker(learned)
    observe(instance, '-', 1)
    learned['-'] = 'end'
    observe(instance, '-', 2)
    assert [event.kind for event in events] == [
        'icy_marker_candidate', 'break_start', 'break_end']
    assert events[-1].details['classification'] == 'learned'


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


@pytest.mark.parametrize('title', ['#LOCAL_BREAK#', '*CUSTOM_AD*', '-SPOT-'])
def test_unknown_visibly_framed_tokens_are_candidates_with_original_evidence(title):
    instance, events = tracker()
    observe(instance, title, 1)
    assert [event.kind for event in events] == ['icy_marker_candidate', 'break_start']
    assert events[0].details['classification'] == 'candidate'
    assert events[1].details['classification'] == 'inferred'
    assert instance.latest_candidate() == title.upper()
    assert events[0].evidence['StreamTitle'] == title


def test_bare_marker_punctuation_stays_direction_neutral():
    for title in ('-', '#', '*'):
        assert parse_marker(title) is None
        instance, events = tracker()
        observe(instance, title, 1)
        assert [event.kind for event in events] == ['icy_marker_candidate', 'break_start']
        assert all(event.details['classification'] != 'explicit' for event in events)


@pytest.mark.parametrize('title', [
    'The #AD_START# Song', 'Artist - #AD_END#', 'Commercial Breakers',
    'My *CUSTOM_AD* Track', 'Song about #LOCAL_BREAK#',
])
def test_marker_words_inside_ordinary_artist_title_are_not_candidates(title):
    instance, events = tracker()
    observe(instance, title, 1)
    assert events == []
    assert instance.latest_candidate() is None


def test_oversized_framed_unknown_token_is_not_candidate():
    instance, events = tracker()
    observe(instance, '#' + ('X' * 256) + '#', 1)
    assert events == []
    assert instance.latest_candidate() is None


@pytest.mark.parametrize('title', ['#' + ('A' * 65) + '#', '#12345#'])
def test_framed_value_must_be_short_and_have_marker_text(title):
    instance, events = tracker()
    observe(instance, title, 1)
    assert events == []
    assert instance.latest_candidate() is None


def test_retention_is_bounded_by_age_and_count():
    instance, _ = tracker()
    for number in range(100):
        observe(instance, 'Song %s' % number, number)
    assert len(instance.records) == 64
    instance.expire(500)
    assert len(instance.records) == 0


def test_duplicate_suppression_title_expires_with_history():
    instance, _ = tracker()
    observe(instance, 'An ordinary title', 1)
    instance.expire(302)
    assert len(instance.records) == 0
    assert instance._last_title is None
    observe(instance, 'An ordinary title', 303)
    assert len(instance.records) == 1


@pytest.mark.parametrize('action,expected', [('start', ['icy_marker_labeled']),
                                            ('end', ['icy_marker_labeled', 'break_end'])])
def test_manual_label_updates_immediately_without_duplicate_boundary(action, expected):
    instance, events = tracker()
    observe(instance, '-', 1)
    before = len(events)
    assert instance.label('-', action, '2026-10-23T00:00:00Z', 2)
    assert [event.kind for event in events[before:]] == expected
    assert events[before].details['reason'] == action
    observe(instance, '-', 3)
    instance.expire(305)
    assert [event.kind for event in events[before:]] == expected
    assert events[before].details['classification'] == 'learned'


def test_transient_label_overrides_persisted_lookup_and_is_exact():
    instance, events = tracker()
    observe(instance, '-', 1)
    assert instance.label('-', 'end', 'now', 2)
    instance.learned_lookup = lambda marker: 'start' if marker == '-' else None
    observe(instance, '#', 3)
    observe(instance, '-', 4)
    assert events[-1].kind == 'break_end'
    assert events[-1].details['classification'] == 'learned'
    replacement, replacement_events = tracker()
    observe(replacement, '-', 5)
    assert replacement_events[-1].details['classification'] == 'inferred'


def test_manual_label_rejects_stale_and_nonexact_candidate():
    instance, events = tracker()
    observe(instance, '-', 1)
    observe(instance, '#', 2)
    before = len(events)
    assert not instance.label('-', 'end', 'now', 3)
    assert not instance.label(' # ', 'end', 'now', 3)
    assert not instance.label('#', 'end', 'now', 303)
    assert not any(event.kind == 'icy_marker_labeled' for event in events[before:])


def test_relabeling_same_direction_emits_label_but_no_duplicate_transition():
    instance, events = tracker()
    observe(instance, '-', 1)
    assert instance.label('-', 'end', 'now', 2)
    assert instance.label('-', 'end', 'now', 3)
    assert [event.kind for event in events] == [
        'icy_marker_candidate', 'break_start', 'icy_marker_labeled', 'break_end',
        'icy_marker_labeled']


def test_session_override_count_is_bounded_and_recent_labels_survive():
    instance, events = tracker()
    for index in range(65):
        marker = '-' * (index + 1)
        observe(instance, marker, index * 2)
        assert instance.label(marker, 'end', 'now', index * 2 + 1)
    observe(instance, '-', 132)
    assert events[-1].kind == 'break_start'
    assert events[-1].details['classification'] == 'inferred'
    observe(instance, '--', 133)
    assert events[-1].kind == 'break_end'
    assert events[-1].details['classification'] == 'learned'
