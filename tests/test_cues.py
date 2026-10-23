# SPDX-License-Identifier: GPL-2.0-or-later
"""Cue signals describe upstream evidence, never listener audibility or aired ads."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cues import CueTracker
from events import JsonEventSink
from hls import parse_playlist


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def tracker(sink=None):
    sink = sink or Sink()
    return CueTracker('session', 'station', sink,
                      lambda: datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc),
                      lambda: 123.5), sink


def playlist(body='', sequence=10, count=1, disc=0):
    return parse_playlist('#EXTM3U\n#EXT-X-TARGETDURATION:5\n'
                          '#EXT-X-MEDIA-SEQUENCE:%d\n#EXT-X-DISCONTINUITY-SEQUENCE:%d\n'
                          % (sequence, disc) + body + ''.join(
                              '#EXTINF:5,ordinary advertisement title\n%d.aac\n' % n
                              for n in range(sequence, sequence + count)),
                          'https://radio.example/live.m3u8')


def kinds(sink):
    return [event.kind for event in sink.events]


def events(sink, kind):
    return [event for event in sink.events if event.kind == kind]


def test_explicit_cue_out_in_repeated_windows_and_observation_clocks():
    t, sink = tracker()
    p = parse_playlist((Path(__file__).parent / 'fixtures/hls/cue-lifecycle.m3u8').read_text(),
                       'https://radio.example/live.m3u8')
    for _ in range(3):
        t.observe(p, 'audio', 2)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_end']
    start, end = events(sink, 'break_start')[0], events(sink, 'break_end')[0]
    assert start.details['classification'] == 'signaled'
    assert start.details['media_position'] == 0
    assert end.details['media_position'] == 5
    assert start.details['rendition_id'] == 'audio'
    assert start.details['epoch'] == 2
    assert start.observed_at == '2026-10-21T17:00:00Z'
    assert start.monotonic_at == 123.5
    assert start.protocol == 'hls' and start.session_id == 'session'


def test_join_mid_break_does_not_invent_start():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=10,Duration=30\n'), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 11), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'cue_observed', 'break_end']
    assert sink.events[0].details['classification'] == 'in_progress'


def test_missing_end_is_unresolved():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:30\n'), 'a', 0)
    t.finish('session_closed')
    t.finish('session_closed')
    assert kinds(sink) == ['cue_observed', 'break_start', 'break_unresolved']
    assert sink.events[-1].details['reason'] == 'session_closed'


def test_expected_duration_expiry_is_unresolved_not_explicit_end():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:10\n'), 'a', 0)
    t.observe(playlist(sequence=11, count=2), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'break_unresolved']
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'


def test_explicit_end_wins_over_expected_expiry_in_same_window():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:5\n#EXTINF:5,\n10.aac\n#EXT-X-CUE-IN\n', 10), 'a', 0)
    assert len(events(sink, 'break_end')) == 1
    assert not events(sink, 'break_unresolved')


def daterange(extra='', start='2026-10-21T17:01:00Z'):
    return '#EXT-X-DATERANGE:ID="ad-1",START-DATE="%s",SCTE35-OUT=0xFC00%s\n' % (start, extra)


def test_daterange_updates_are_normalized_and_only_meaningful():
    t, sink = tracker()
    for attrs in ['', ',X-NOTE="first"', ',DURATION=30', ',DURATION=30.0',
                  ',DURATION=30,SCTE35-IN=0xFC01,END-DATE="2026-10-21T17:01:30Z"']:
        t.observe(playlist(daterange(attrs)), 'a', 0)
    assert len(events(sink, 'break_start')) == 1
    assert len(events(sink, 'break_update')) == 1
    assert events(sink, 'break_update')[0].details['duration'] == 30
    assert len(events(sink, 'break_end')) == 1
    assert all('media_position' not in e.to_dict().get('details', {}) for e in sink.events)
    assert len(events(sink, 'cue_observed')) == 3


def test_active_daterange_is_observed_in_progress():
    t, sink = tracker()
    t.observe(playlist(daterange(',DURATION=30', start='2026-10-21T16:59:50Z')), 'a', 0)
    assert kinds(sink) == ['cue_observed']
    assert sink.events[0].details['classification'] == 'in_progress'
    t.finish('session_closed')
    assert sink.events[-1].kind == 'break_unresolved'


@pytest.mark.parametrize('body', [
    '#EXT-X-CUE-OUT:DURATION=bad\n',
    '#EXT-X-CUE-OUT:DURATION=30,SCTE35="??"\n',
    '#EXT-X-CUE-OUT:DURATION=30,SCTE35="Zm9v"\n',
    '#EXT-X-DATERANGE:ID="bad",SCTE35-OUT=0xZZ\n',
    '#EXT-X-DATERANGE:ID="bad",SCTE35-OUT=0xF\n',
    '#EXT-X-DATERANGE:ID="bad",SCTE35-IN=0xFC01,END-DATE="bad"\n',
])
def test_malformed_cue_is_invalid_observation_not_start(body):
    t, sink = tracker()
    t.observe(playlist(body), 'a', 0)
    assert kinds(sink) == ['cue_observed']
    assert sink.events[0].details['classification'] == 'invalid'


def test_cmd_is_opaque_even_if_encoded_payload_is_valid():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-DATERANGE:ID="cmd",SCTE35-CMD=0xFC00\n'), 'a', 0)
    assert kinds(sink) == ['cue_observed']
    assert sink.events[0].details['classification'] == 'opaque'


def test_generic_dateranges_titles_and_discontinuities_do_not_confirm_break():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-DATERANGE:ID="song",CLASS="advertisement"\n'
                       '#EXT-X-DISCONTINUITY\n'), 'a', 0)
    assert sink.events == []


def test_reused_id_after_epoch_is_distinct():
    t, sink = tracker()
    p = playlist(daterange())
    t.observe(p, 'a', 0)
    t.observe(p, 'a', 1)
    assert len(events(sink, 'break_start')) == 2
    assert len(events(sink, 'break_unresolved')) == 1
    assert sink.events[2].details['reason'] == 'epoch_changed'


def test_renditions_do_not_merge_breaks():
    t, sink = tracker()
    for rendition in ('a', 'b'):
        t.observe(playlist(daterange()), rendition, 0)
    t.observe(playlist(daterange(',SCTE35-IN=0xFC01')), 'a', 0)
    t.finish('closed')
    assert len(events(sink, 'break_start')) == 2
    assert events(sink, 'break_end')[0].details['rendition_id'] == 'a'
    assert events(sink, 'break_unresolved')[0].details['rendition_id'] == 'b'


def test_repeated_discontinuity_does_not_reset_epoch():
    t, sink = tracker()
    p = playlist('#EXT-X-DISCONTINUITY\n' + daterange())
    t.observe(p, 'a', 0)
    t.observe(p, 'a', 0)
    t.observe(playlist(daterange(), 11, disc=1), 'a', 0)
    assert len(events(sink, 'break_start')) == 1
    t.observe(playlist('#EXT-X-DISCONTINUITY\n' + daterange(), 12, disc=1), 'a', 0)
    assert len(events(sink, 'break_start')) == 1
    assert not events(sink, 'break_unresolved')


def test_overlapping_windows_align_positions_but_gap_leaves_timing_unknown():
    t, sink = tracker()
    t.observe(playlist(count=2), 'a', 0)
    t.observe(playlist('#EXTINF:5,\n11.aac\n#EXT-X-CUE-OUT:30\n', 11), 'a', 0)
    assert events(sink, 'break_start')[0].details['media_position'] == 10
    t.observe(playlist('#EXT-X-CUE-IN\n', 20), 'a', 0)
    assert events(sink, 'break_unresolved')[0].details['reason'] == 'timeline_gap'
    assert 'media_position' not in sink.events[-1].to_dict()['details']
    assert not events(sink, 'break_end')


def test_end_without_start_does_not_invent_break():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-IN\n'), 'a', 0)
    assert kinds(sink) == ['cue_observed']


def test_parallel_repeated_observations_are_serialized():
    t, sink = tracker()
    p = playlist('#EXT-X-CUE-OUT:30\n')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: t.observe(p, 'a', 0), range(40)))
    assert kinds(sink) == ['cue_observed', 'break_start']


def test_tracker_evidence_uses_existing_sanitized_sink(tmp_path):
    debug = []
    t, _ = tracker(JsonEventSink(tmp_path, True, debug.append))
    t.observe(playlist('#EXT-X-CUE-OUT:ID="token=private",DURATION=30\n'), 'a', 0)
    assert 'private' not in ''.join(debug)
    assert 'REDACTED' in ''.join(debug)


def test_end_date_update_is_meaningful_but_never_a_cue_in():
    t, sink = tracker()
    t.observe(playlist(daterange()), 'a', 0)
    t.observe(playlist(daterange(',END-DATE="2026-10-21T17:01:30Z"')), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_update']
    t.finish('closed')
    assert sink.events[-1].kind == 'break_unresolved'


def test_late_response_from_old_epoch_cannot_reset_new_epoch():
    t, sink = tracker()
    p = playlist(daterange())
    t.observe(p, 'a', 1)
    t.observe(p, 'a', 0)
    t.observe(p, 'a', 1)
    assert kinds(sink) == ['cue_observed', 'break_start']


def test_dedup_retains_recent_evidence_and_evicts_oldest_at_limit():
    t, sink = tracker()
    for sequence in range(4097):
        t.observe(playlist('#EXT-X-CUE-IN\n', sequence), 'a', 0)
    assert len(sink.events) == 4097
    t.observe(playlist('#EXT-X-CUE-IN\n', 4096), 'a', 0)
    assert len(sink.events) == 4097
    t.observe(playlist('#EXT-X-CUE-IN\n', 0), 'a', 0)
    assert len(sink.events) == 4098


def test_finish_prevents_late_observations_reviving_breaks():
    t, sink = tracker()
    t.finish('closed')
    t.observe(playlist(daterange()), 'a', 0)
    assert sink.events == []


def test_delayed_older_in_does_not_close_newer_out():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:60\n', 10, count=3), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT:60\n', 12), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 11, count=2), 'a', 0)
    assert not events(sink, 'break_end')
    assert sink.events[-1].kind == 'cue_observed'
    t.observe(playlist('#EXT-X-CUE-IN\n', 13), 'a', 0)
    assert len(events(sink, 'break_end')) == 1
    assert events(sink, 'break_end')[0].details['cue_id'] == '12'
    assert events(sink, 'break_end')[0].details['media_sequence'] == 13


@pytest.mark.parametrize('older', ['#EXT-X-CUE-OUT:90\n',
                                   '#EXT-X-CUE-OUT-CONT:ElapsedTime=5,Duration=90\n'])
def test_delayed_older_start_or_progress_does_not_mutate_newer_break(older):
    t, sink = tracker()
    t.observe(playlist(count=3), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT:60\n', 12), 'a', 0)
    t.observe(playlist(older, 11, count=2), 'a', 0)
    assert len(events(sink, 'break_start')) == 1
    assert not events(sink, 'break_update')
    assert not events(sink, 'break_unresolved')
    t.finish('closed')
    assert len(events(sink, 'break_unresolved')) == 1
    assert sink.events[-1].details['cue_id'] == '12'
    assert sink.events[-1].details['duration'] == 60


@pytest.mark.parametrize('continuation', ['ElapsedTime=25,Duration=30', '25/30'])
def test_initial_continuation_expires_after_remaining_duration(continuation):
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:%s\n' % continuation), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_unresolved']
    assert sink.events[0].details['classification'] == 'in_progress'
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'
    assert sink.events[-1].details['media_position'] == 0
    assert sink.events[-1].details['duration'] == 30


def test_duration_added_to_durationless_out_enables_original_boundary_expiry():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT:10\n', 10), 'a', 0)
    t.observe(playlist(sequence=11), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed',
                           'break_update', 'break_unresolved']
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'
    assert sink.events[-1].details['media_position'] == 0


def test_late_duration_correction_retains_original_known_start():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:ID="break"\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT:ID="break",DURATION=20\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT:ID="break",DURATION=10\n', 11), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_update',
                           'cue_observed', 'break_update', 'break_unresolved']
    assert sink.events[-1].details['duration'] == 10
    assert sink.events[-1].details['media_position'] == 0


def test_duration_added_to_continuation_retains_prior_elapsed_origin():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=25\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:Duration=30\n', 10), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'cue_observed', 'break_update', 'break_unresolved']
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'
    assert sink.events[-1].details['media_position'] == 0


@pytest.mark.parametrize('start', [
    '2026-10-21T17:01:00+0000',
    '2026-10-21T22:31:00+0530',
    '2026-10-21T10:01:00-0700',
])
def test_daterange_basic_timezone_normalizes_repeated_boundaries(start):
    t, sink = tracker()
    t.observe(playlist(daterange(start=start)), 'a', 0)
    t.observe(playlist(daterange(start='2026-10-21T17:01:00Z')), 'a', 0)
    t.observe(playlist(daterange(',DURATION=30,END-DATE="2026-10-21T17:01:30+0000",SCTE35-IN=0xFC01', start=start)), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_end']
    assert events(sink, 'break_end')[0].details['duration'] == 30


@pytest.mark.parametrize('offset', ['+2400', '+1260', '-9999', '+0000junk'])
def test_daterange_malformed_basic_timezone_remains_invalid(offset):
    t, sink = tracker()
    t.observe(playlist(daterange(start='2026-10-21T17:01:00' + offset)), 'a', 0)
    assert kinds(sink) == ['cue_observed']
    assert sink.events[0].details['classification'] == 'invalid'


def test_later_explicit_in_ends_expired_break_once_with_original_id():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:5\n'), 'a', 0)
    t.observe(playlist(sequence=10), 'a', 0)
    t.observe(playlist(sequence=11), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 12), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 13), 'a', 0)
    t.finish('closed')
    assert len(events(sink, 'break_unresolved')) == 1
    assert len(events(sink, 'break_end')) == 1
    assert events(sink, 'break_end')[0].details['cue_id'] == '10'
    assert events(sink, 'break_end')[0].details['media_sequence'] == 12
    assert sink.events[3].details['cue_id'] == '10'  # linked cue_observed


@pytest.mark.parametrize('boundary', ['superseded', 'epoch', 'gap'])
def test_expired_break_cannot_end_after_terminal_boundary(boundary):
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:5\n'), 'a', 0)
    if boundary == 'superseded':
        t.observe(playlist('#EXT-X-CUE-OUT:30\n', 11), 'a', 0)
        t.observe(playlist('#EXT-X-CUE-IN\n', 12), 'a', 0)
        assert [e.details['cue_id'] for e in events(sink, 'break_end')] == ['11']
    else:
        t.observe(playlist('#EXT-X-CUE-IN\n', 20 if boundary == 'gap' else 11),
                  'a', 1 if boundary == 'epoch' else 0)
        assert not events(sink, 'break_end')
    t.finish('closed')
    assert len([e for e in events(sink, 'break_unresolved') if e.details['cue_id'] == '10']) == 1


@pytest.mark.parametrize('fraction', ['1', '12', '1234', '12345', '123456789'])
def test_daterange_fractional_precision_remains_signaled_with_original_evidence(fraction):
    t, sink = tracker()
    start = '2026-10-21T17:01:00.' + fraction + 'Z'
    end = '2026-10-21T17:01:30.' + fraction + 'Z'
    t.observe(playlist(daterange(',DURATION=30', start=start)), 'a', 0)
    t.observe(playlist(daterange(',DURATION=30,SCTE35-IN=0xFC01,END-DATE="' + end + '"', start=start)), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_end']
    assert sink.events[0].evidence['START-DATE'] == start
    assert sink.events[-1].evidence['END-DATE'] == end


@pytest.mark.parametrize('join_mid_break', [False, True])
def test_advancing_continuations_keep_evidence_without_lifecycle_updates(join_mid_break):
    t, sink = tracker()
    initial = ('#EXT-X-CUE-OUT-CONT:ElapsedTime=0,Duration=30\n'
               if join_mid_break else '#EXT-X-CUE-OUT:30\n')
    t.observe(playlist(initial, 10), 'a', 0)
    for sequence, elapsed in [(11, 5), (12, 10), (13, 15)]:
        window = playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=%d,Duration=30\n' % elapsed, sequence)
        t.observe(window, 'a', 0)
        t.observe(window, 'a', 0)
    assert len(events(sink, 'cue_observed')) == 4
    assert [e.evidence['ElapsedTime'] for e in events(sink, 'cue_observed')[1:]] == ['5','10','15']
    assert len(events(sink, 'break_start')) == (0 if join_mid_break else 1)
    assert not events(sink, 'break_update')
    assert not events(sink, 'break_unresolved')
    t.observe(playlist(sequence=14, count=2), 'a', 0)
    assert len(events(sink, 'break_unresolved')) == 1
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'


def test_continuation_timing_refresh_survives_suppressed_update():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=0\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=10\n', 11), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:Duration=20\n', 12), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'cue_observed', 'cue_observed',
                           'break_update', 'break_unresolved']
    assert sink.events[-1].details['reason'] == 'expected_duration_elapsed'


def test_daterange_start_date_correction_is_meaningful():
    t, sink = tracker()
    t.observe(playlist(daterange()), 'a', 0)
    t.observe(playlist(daterange(start='2026-10-21T17:01:05Z')), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_update']


@pytest.mark.parametrize('rolling', [False, True])
@pytest.mark.parametrize('out_before_first_segment', [False, True])
def test_explicit_continuation_bridges_discontinuities_without_new_break(rolling,out_before_first_segment):
    t, sink = tracker()
    out = '#EXT-X-CUE-OUT:60\n'
    cont = '#EXT-X-CUE-OUT-CONT:ElapsedTime=5,Duration=60\n'
    end = '#EXT-X-CUE-IN\n'
    if rolling:
        windows = [playlist(out, 10, count=0 if out_before_first_segment else 1),
                   playlist(cont, 10 if out_before_first_segment else 11, disc=1),
                   playlist(end, 11 if out_before_first_segment else 12, disc=2)]
    else:
        prefix = '' if out_before_first_segment else '#EXTINF:5,\n10.aac\n'
        windows = [playlist(out + prefix + '#EXT-X-DISCONTINUITY\n' + cont +
                    '#EXTINF:5,\nad.aac\n#EXT-X-DISCONTINUITY\n' + end, 10)]
    for window in windows:
        t.observe(window, 'a', 0)
        t.observe(window, 'a', 0)
    assert len(events(sink, 'cue_observed')) == 3
    assert len(events(sink, 'break_start')) == 1
    assert len(events(sink, 'break_end')) == 1
    assert not events(sink, 'break_unresolved')
    assert not events(sink, 'break_update')
    assert events(sink, 'break_start')[0].details['cue_id'] == '10'
    assert events(sink, 'break_end')[0].details['cue_id'] == '10'


def test_daterange_identity_survives_uncued_discontinuities_with_unknown_media_timing():
    t, sink = tracker()
    t.observe(playlist(daterange(), 10), 'a', 0)
    t.observe(playlist(sequence=11, disc=1), 'a', 0)
    t.observe(playlist(daterange(',SCTE35-IN=0xFC01,END-DATE="2026-10-21T17:01:30Z"'), 12, disc=2), 'a', 0)
    assert kinds(sink) == ['cue_observed', 'break_start', 'cue_observed', 'break_end']
    assert all(e.details['cue_id'] == 'ad-1' for e in sink.events)
    assert all(e.details['media_position'] is None for e in sink.events)


@pytest.mark.parametrize('boundary', ['no-cue', 'malformed', 'skipped', 'late', 'different-id'])
def test_unproven_discontinuity_cannot_bridge_cue_association(boundary):
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:ID="old",DURATION=60\n', 10), 'a', 0)
    content = {'no-cue':'', 'malformed':'#EXT-X-CUE-OUT-CONT:ElapsedTime=bad\n',
               'skipped':'#EXT-X-CUE-IN\n',
               'late':'#EXTINF:5,\n11.aac\n#EXT-X-CUE-IN\n',
               'different-id':'#EXT-X-CUE-OUT-CONT:ID="other",ElapsedTime=5,Duration=60\n'}[boundary]
    t.observe(playlist(content, 11, disc=2 if boundary=='skipped' else 1), 'a', 0)
    assert len(events(sink, 'break_unresolved')) == 1
    assert events(sink, 'break_unresolved')[0].details['cue_id'] == 'old'
    assert not events(sink, 'break_end')


def test_expired_cue_can_match_explicit_in_at_next_discontinuity():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:5\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 11, disc=1), 'a', 0)
    assert len(events(sink, 'break_unresolved')) == 1
    assert len(events(sink, 'break_end')) == 1
    assert events(sink, 'break_end')[0].details['cue_id'] == '10'


def test_stale_cue_after_cross_discontinuity_continuation_cannot_end_it():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:60\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:ElapsedTime=5,Duration=60\n', 11, disc=1), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-IN\n', 10), 'a', 0)
    assert not events(sink, 'break_end')
    t.observe(playlist('#EXT-X-CUE-IN\n', 12, disc=2), 'a', 0)
    assert [e.details['cue_id'] for e in events(sink, 'break_end')] == ['10']


def test_discontinuity_continuation_without_elapsed_does_not_infer_expiry():
    t, sink = tracker()
    t.observe(playlist('#EXT-X-CUE-OUT:10\n', 10), 'a', 0)
    t.observe(playlist('#EXT-X-CUE-OUT-CONT:Duration=10\n', 11, disc=1), 'a', 0)
    assert not events(sink, 'break_unresolved')
    t.observe(playlist('#EXT-X-CUE-IN\n', 12, disc=2), 'a', 0)
    assert [e.details['cue_id'] for e in events(sink, 'break_end')] == ['10']


def test_new_out_reusing_id_across_discontinuity_is_a_new_generic_break():
    t, sink = tracker()
    out = '#EXT-X-CUE-OUT:ID="same",DURATION=60\n'
    t.observe(playlist(out, 10), 'a', 0)
    t.observe(playlist(out, 11, disc=1), 'a', 0)
    assert len(events(sink, 'break_start')) == 2
    assert len(events(sink, 'break_unresolved')) == 1
    assert events(sink, 'break_unresolved')[0].details['reason'] == 'discontinuity'


def test_repository_media_fixture_links_continuation_and_same_id_date_range_end():
    t, sink = tracker()
    text = (Path(__file__).parent/'fixtures/hls/media.m3u8').read_text()
    text = text.replace('#EXT-X-CUE-IN', '#EXT-X-DATERANGE:ID="break-1",START-DATE="2026-10-21T12:00:00Z",SCTE35-IN=0xFC01\n#EXT-X-CUE-IN')
    t.observe(parse_playlist(text, 'https://radio.example/live.m3u8'), 'a', 0)
    assert not events(sink, 'break_unresolved')
    assert [e.details['cue_id'] for e in events(sink, 'break_end')] == ['break-1','100']
    # The past DATERANGE start is in-progress evidence, not an invented start.
    assert [e.details['cue_id'] for e in events(sink, 'break_start')] == ['100']
