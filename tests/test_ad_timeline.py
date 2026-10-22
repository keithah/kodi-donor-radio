from pathlib import Path

import pytest

from ad_models import CachedSegment, MediaInfo, PreparedAd, ResolvedAd
from ad_timeline import AdTimeline
from hls import parse_playlist

URL = 'http://radio.test/live.m3u8'
FRAME = 1024


def media(name, frames=94, pts=0):
    return CachedSegment(name, Path('/unused/' + name),
                         MediaInfo('aac', 48000, 2, frames * FRAME, pts, pts + (frames - 1) * 1920))


def creative(name='ad', frames=(94, 94)):
    items = tuple(media(name + str(i), count) for i, count in enumerate(frames))
    return PreparedAd(ResolvedAd(name, URL, sum(i.info.samples for i in items) / 48000, (), ()),
                      items, sum(i.info.samples for i in items))


def playlist(names, cues=None, sequence=0, ended=False, disc=0):
    cues = cues or {}
    lines = ['#EXTM3U', '#EXT-X-TARGETDURATION:3', '#EXT-X-MEDIA-SEQUENCE:%d' % sequence,
             '#EXT-X-DISCONTINUITY-SEQUENCE:%d' % disc]
    for i, name in enumerate(names):
        lines.extend(cues.get(i, ()))
        lines.extend(('#EXTINF:2.005333,', name + '.ts'))
    if ended:
        lines.append('#EXT-X-ENDLIST')
    return parse_playlist('\n'.join(lines) + '\n', URL)


def sources(*names, frames=94):
    return {'http://radio.test/' + n + '.ts': media(n, frames) for n in names}


def ids(presentation):
    return [s.resource_id for s in presentation.segments]


def test_preroll_replacement_order_and_exact_programme_boundaries():
    t = AdTimeline(creative('pre', (94,)), 16)
    t.offer(creative())
    p = t.update(playlist(['a', 'break0', 'break1', 'b'],
                          {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN']}, ended=True),
                 sources('a', 'break0', 'break1', 'b'))
    assert ids(p) == ['pre0', 'a', 'ad0', 'ad1', 'b']
    assert [s.start_sample for s in p.segments] == [0, 94*FRAME, 188*FRAME, 282*FRAME, 376*FRAME]
    assert [s.discontinuity for s in p.segments] == [0, 1, 2, 2, 3]
    assert [s.sequence for s in p.segments] == list(range(5))
    assert [(x.start_sample, x.end_sample) for x in p.placements] == [(0, 94*FRAME), (188*FRAME, 376*FRAME)]
    assert p.ended


def test_published_resources_and_positions_survive_reloads_sliding_and_late_offer():
    t = AdTimeline(None, 3)
    first = t.update(playlist(['a', 'break0']), sources('a', 'break0'))
    t.offer(creative())
    t.update(playlist(['a', 'break0', 'break1', 'b'],
                      {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN']}),
             sources('a', 'break0', 'break1', 'b'))
    assert ids(t.snapshot()) == ['break0', 'break1', 'b']
    assert t.snapshot().segments[0] == first.segments[1]
    again = t.update(playlist(['break1', 'b', 'c'], sequence=2), sources('break1', 'b', 'c'))
    assert ids(again) == ['break1', 'b', 'c']
    assert [s.sequence for s in again.segments] == [2, 3, 4]
    assert again.segments[-1].start_sample == 4 * 94 * FRAME


@pytest.mark.parametrize('cues', [
    {1: ['#EXT-X-CUE-OUT:4.010666']},
    {0: ['#EXT-X-CUE-OUT-CONT:1.0/4.010666'], 2: ['#EXT-X-CUE-IN']},
    {1: ['#EXT-X-CUE-OUT:bad'], 3: ['#EXT-X-CUE-IN']},
    {1: ['#EXT-X-CUE-OUT:4.010666'], 2: ['#EXT-X-CUE-OUT:2'], 3: ['#EXT-X-CUE-IN']},
    {1: ['#EXT-X-CUE-OUT:4.010666'], 2: ['#EXT-X-CUE-IN'], 3: ['#EXT-X-CUE-IN']},
    {1: ['#EXT-X-DATERANGE:ID="x",START-DATE="2026-01-01T00:00:00Z",SCTE35-OUT="0xFC"'],
     3: ['#EXT-X-DATERANGE:ID="x",START-DATE="2026-01-01T00:00:00Z",SCTE35-IN="0xFC"']},
])
def test_ineligible_intervals_retain_original(cues):
    t = AdTimeline(None, 16)
    t.offer(creative())
    p = t.update(playlist(['a', 'break0', 'break1', 'b'], cues),
                 sources('a', 'break0', 'break1', 'b'))
    assert ids(p) == ['a', 'break0', 'break1', 'b']
    assert not p.placements


def test_duration_mismatch_and_no_fill_retain_original():
    p = playlist(['a', 'break0', 'break1', 'b'],
                 {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN']})
    for ad in (None, creative(frames=(96, 94))):
        t = AdTimeline(None, 16)
        if ad: t.offer(ad)
        assert ids(t.update(p, sources('a', 'break0', 'break1', 'b'))) == ['a', 'break0', 'break1', 'b']


def test_source_mutation_cannot_change_published_bytes_or_identity():
    t = AdTimeline(None, 16)
    original = t.update(playlist(['a', 'b']), sources('a', 'b'))
    mutated = sources('a', 'b')
    mutated['http://radio.test/a.ts'] = media('different')
    assert t.update(playlist(['a', 'b']), mutated) == original


def test_sequence_reset_creates_new_epoch_and_never_reuses_output_numbers():
    t = AdTimeline(None, 16)
    a = t.update(playlist(['a', 'b'], sequence=50), sources('a', 'b'))
    b = t.update(playlist(['x', 'y'], sequence=0), sources('x', 'y'))
    assert ids(b) == ['a', 'b', 'x', 'y']
    assert [s.sequence for s in b.segments] == [0, 1, 2, 3]
    assert b.segments[2].start_sample == 2 * 94 * FRAME
    assert b.segments[2].discontinuity > a.segments[-1].discontinuity


def test_one_frame_tolerance_does_not_create_cumulative_drift():
    t = AdTimeline(None, 16)
    t.offer(creative(frames=(94, 95)))
    p = t.update(playlist(['a', 'break0', 'break1', 'b'],
                          {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN']}),
                 sources('a', 'break0', 'break1', 'b'))
    assert ids(p) == ['a', 'ad0', 'ad1', 'b']
    assert p.segments[-1].start_sample == (94+94+95)*FRAME


def test_second_one_frame_overrun_is_rejected_to_bound_total_drift():
    t = AdTimeline(None, 24)
    t.offer(creative('ad', (94, 95)))
    t.offer(creative('other', (94, 95)))
    first = t.update(playlist(['a', 'break0', 'break1', 'b'],
                              {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN']}),
                     sources('a', 'break0', 'break1', 'b'))
    second = t.update(playlist(['break2', 'break3', 'c'],
                               {0: ['#EXT-X-CUE-OUT:4.010666'], 2: ['#EXT-X-CUE-IN']}, sequence=4),
                      sources('break2', 'break3', 'c'))
    assert ids(first) == ['a', 'ad0', 'ad1', 'b']
    assert ids(second)[-3:] == ['break2', 'break3', 'c']
    assert second.segments[-1].start_sample == (94*6 + 1) * FRAME


def test_old_window_after_eviction_does_not_republish_source():
    t = AdTimeline(None, 2)
    t.update(playlist(['a', 'b', 'c'], sequence=10), sources('a', 'b', 'c'))
    current = t.snapshot()
    assert ids(current) == ['b', 'c']
    assert t.update(playlist(['a'], sequence=10), sources('a')) == current


def test_pts_wrap_has_no_effect_on_source_identity_or_sample_positions():
    t = AdTimeline(None, 4)
    cached = sources('a', 'b')
    cached['http://radio.test/a.ts'] = media('a', pts=(1 << 33) - 1920)
    cached['http://radio.test/b.ts'] = media('b', pts=0)
    p = t.update(playlist(['a', 'b']), cached)
    assert ids(p) == ['a', 'b']
    assert p.segments[1].start_sample == 94 * FRAME


def test_stale_sequence_zero_window_does_not_look_like_a_reset():
    t = AdTimeline(None, 2)
    first = playlist(['a', 'b'], sequence=0)
    t.update(first, sources('a', 'b'))
    t.update(playlist(['b', 'c', 'd'], sequence=1), sources('b', 'c', 'd'))
    current = t.snapshot()
    assert ids(current) == ['c', 'd']
    assert t.update(first, sources('a', 'b')) == current


def test_two_complete_opportunities_in_one_window_commit_independently():
    t = AdTimeline(None, 20)
    t.offer(creative('first'))
    t.offer(creative('second'))
    p = t.update(playlist(['a', 'x0', 'x1', 'b', 'y0', 'y1', 'c'],
                          {1: ['#EXT-X-CUE-OUT:4.010666'], 3: ['#EXT-X-CUE-IN'],
                           4: ['#EXT-X-CUE-OUT:4.010666'], 6: ['#EXT-X-CUE-IN']}),
                 sources('a', 'x0', 'x1', 'b', 'y0', 'y1', 'c'))
    assert ids(p) == ['a', 'first0', 'first1', 'b', 'second0', 'second1', 'c']
    assert len(p.placements) == 2


def test_retired_zero_anchor_cannot_reset_back_to_prior_epoch():
    t = AdTimeline(None, 2)
    old = playlist(['old0', 'old1'])
    new = playlist(['new0', 'new1'])
    t.update(old, sources('old0', 'old1'))
    t.update(playlist(['old1', 'old2', 'old3'], sequence=1), sources('old1', 'old2', 'old3'))
    t.update(new, sources('new0', 'new1'))
    t.update(playlist(['new1', 'new2', 'new3'], sequence=1), sources('new1', 'new2', 'new3'))
    current = t.snapshot()
    assert ids(current) == ['new2', 'new3']
    assert t.update(old, sources('old0', 'old1')) == current


def test_conflicting_continuation_duration_rejects_replacement():
    t = AdTimeline(None, 16)
    t.offer(creative())
    p = t.update(playlist(['a', 'x0', 'x1', 'b'],
                          {1: ['#EXT-X-CUE-OUT:4.010666'],
                           2: ['#EXT-X-CUE-OUT-CONT:1.0/9.0'],
                           3: ['#EXT-X-CUE-IN']}),
                 sources('a', 'x0', 'x1', 'b'))
    assert ids(p) == ['a', 'x0', 'x1', 'b']
