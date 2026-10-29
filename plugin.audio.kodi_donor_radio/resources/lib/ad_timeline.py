# SPDX-License-Identifier: GPL-2.0-or-later
"""Session-owned, append-only composition of cached AAC HLS segments.

Published output records are immutable. Cache pin and release belong to the
caller; this module makes no network, filesystem, or tracking calls.
"""
from collections import OrderedDict
import math
import threading
import uuid

from ad_models import OutputSegment, Placement, Presentation


FRAME = 1024
RATE = 48000
MAX_EPOCH_ANCHORS = 16


class TimelineEpochLimit(ValueError):
    """A new source epoch cannot be distinguished within bounded history."""


def _supported(item):
    info = item.info
    return (info.codec == 'aac' and info.sample_rate == RATE and info.channels == 2
            and isinstance(info.samples, int) and info.samples > 0
            and info.samples % FRAME == 0)


def _duration(cue):
    raw = cue.attributes.get('DURATION')
    if raw is None:
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return False
    return value if math.isfinite(value) and value > 0 else False


class AdTimeline:
    """Compose a bounded session window while retaining monotonic output IDs.

    The first observed source segment is the session's sample-zero programme
    anchor, regardless of its upstream media sequence. A sequence-zero window
    with a new source URI begins a new epoch until the bounded URI history is
    full; output sequence/sample positions never reset.
    """
    def __init__(self, preroll, resource_limit):
        if not isinstance(resource_limit, int) or resource_limit < 1:
            raise ValueError('resource_limit must be positive')
        self._lock = threading.RLock()
        self._limit = resource_limit
        self._preroll = preroll
        self._offers = []
        self._sample_delta = 0
        self._segments = []
        self._placements = []
        self._source = OrderedDict()
        self._epoch = 0
        self._highwater = None
        self._zero_anchors = set()
        self._last_source_disc = None
        self._next_sequence = 0
        self._next_sample = 0
        self._output_disc = 0
        self._boundary_pending = False
        self._ended = False
        self._started = False
        self._initially_finite = False

    def offer(self, ad):
        with self._lock:
            if ad is not None and len(self._offers) < self._limit:
                self._offers.append(ad)

    def snapshot(self):
        with self._lock:
            return Presentation(tuple(self._segments), tuple(self._placements), self._ended,
                                self._initially_finite)

    def _publish(self, item, placement_id=None, discontinuity=False, source_disc=None):
        source_delta = (max(0, source_disc - self._last_source_disc)
                        if source_disc is not None and self._last_source_disc is not None
                        else 0)
        self._output_disc += max(source_delta, int(discontinuity or self._boundary_pending))
        self._boundary_pending = False
        if source_disc is not None:
            self._last_source_disc = source_disc
        record = OutputSegment(self._next_sequence, self._output_disc, item.id,
                               self._next_sample, item.info.samples, placement_id)
        self._segments.append(record)
        self._next_sequence += 1
        self._next_sample += item.info.samples
        return record

    def _trim(self):
        while len(self._segments) > self._limit:
            self._segments.pop(0)
        first = self._segments[0].start_sample if self._segments else self._next_sample
        self._placements = [p for p in self._placements if p.end_sample > first]
        while len(self._source) > self._limit:
            self._source.popitem(last=False)

    def _candidates(self, playlist, source_segments, published, unseen):
        """Select complete unexposed intervals without reusing an offer."""
        cues = [c for c in playlist.cues if c.source != 'EXT-X-DATERANGE']
        active = None
        intervals = []
        malformed = False
        for cue in cues:
            if cue.source == 'EXT-X-CUE-OUT':
                length = _duration(cue)
                if active is not None or length is False:
                    malformed = True
                active = (cue.sequence, cue.discontinuity, length)
            elif cue.source == 'EXT-X-CUE-OUT-CONT':
                if active is None:
                    malformed = True
                else:
                    attrs = cue.attributes
                    continued = _duration(cue)
                    if 'RAW' in attrs:
                        parts = attrs['RAW'].split('/')
                        if len(parts) != 2:
                            malformed = True
                        else:
                            try:
                                elapsed, continued = float(parts[0]), float(parts[1])
                                if not math.isfinite(elapsed) or elapsed < 0 or elapsed > continued:
                                    malformed = True
                            except ValueError:
                                malformed = True
                    if continued is False or (continued is not None and
                            (not math.isfinite(continued) or continued <= 0 or
                             (active[2] is not None and
                              abs(continued - active[2]) * RATE > FRAME))):
                        malformed = True
            elif cue.source == 'EXT-X-CUE-IN':
                if active is None or cue.sequence <= active[0]:
                    malformed = True
                else:
                    intervals.append((active[0], cue.sequence, active[1], active[2]))
                active = None
        if malformed:
            return []
        # A trailing open CUE-OUT is not eligible yet; it does not invalidate
        # complete intervals observed earlier in this same playlist window.
        by_sequence = {s.sequence: s for s in playlist.segments}
        unseen_sequences = {s.sequence for s in unseen}
        used_offers = set()
        cumulative = self._sample_delta
        selected = []
        for start, end, disc, declared in intervals:
            if (start not in by_sequence or
                    (end not in by_sequence and end != playlist.segments[-1].sequence + 1)
                    or any(n not in by_sequence or n in published or n not in unseen_sequences
                           for n in range(start, end))):
                continue
            segments = [by_sequence[n] for n in range(start, end)]
            if any(s.discontinuity != disc or s.uri not in source_segments
                   or not _supported(source_segments[s.uri]) for s in segments):
                continue
            samples = sum(source_segments[s.uri].info.samples for s in segments)
            if declared is not None and abs(samples - declared * RATE) > FRAME:
                continue
            for index, ad in enumerate(self._offers):
                delta = ad.samples - samples
                if (index not in used_offers and ad.segments
                        and ad.samples == sum(x.info.samples for x in ad.segments)
                        and all(_supported(x) for x in ad.segments)
                        and abs(delta) <= FRAME and abs(cumulative + delta) <= FRAME
                        and len(ad.segments) <= self._limit):
                    selected.append((start, end, index, ad))
                    used_offers.add(index)
                    cumulative += delta
                    break
        return selected

    def update(self, playlist, source_segments, source_finite=None):
        with self._lock:
            if playlist.is_master or not playlist.segments:
                return self.snapshot()
            incoming = playlist.segments
            overlap = any((self._epoch, s.sequence, s.discontinuity) in self._source
                          for s in incoming)
            zero = incoming[0].sequence == 0
            zero_uri = incoming[0].uri if zero else None
            reset = (self._highwater is not None and not overlap and zero
                     and self._highwater > 0 and zero_uri not in self._zero_anchors)
            if reset:
                if len(self._zero_anchors) >= MAX_EPOCH_ANCHORS:
                    raise TimelineEpochLimit('source_epoch_history_exhausted')
            candidate_epoch = self._epoch + int(reset)
            candidate_highwater = None if reset else self._highwater
            candidate_source = {} if reset else self._source
            # Validate a prospective epoch before committing the reset. Missing
            # media must not clear source history or make old segments publishable.
            unseen = [s for s in incoming
                      if (candidate_epoch, s.sequence, s.discontinuity) not in candidate_source
                      and (candidate_highwater is None or s.sequence > candidate_highwater)]
            if any(s.uri not in source_segments or not _supported(source_segments[s.uri]) for s in unseen):
                return self.snapshot()
            if reset:
                self._epoch = candidate_epoch
                self._highwater = None
                self._last_source_disc = None
                self._boundary_pending = True
                self._source.clear()
            # A changed URI on an overlapping reload is source mutation, not
            # a new epoch, and must not consume replay-suppression history.
            if zero and (reset or not self._started) and zero_uri not in self._zero_anchors:
                if len(self._zero_anchors) >= MAX_EPOCH_ANCHORS:
                    raise TimelineEpochLimit('source_epoch_history_exhausted')
                self._zero_anchors.add(zero_uri)
            if not self._started:
                self._started = True
                self._initially_finite = (source_finite if source_finite is not None else
                                          any(line.strip() == '#EXT-X-ENDLIST'
                                              for line in playlist.lines))
                pre = self._preroll
                if (pre is not None and pre.segments and len(pre.segments) + len(incoming) <= self._limit
                        and pre.samples == sum(x.info.samples for x in pre.segments)
                        and all(_supported(x) for x in pre.segments)):
                    ident = 'preroll-' + uuid.uuid4().hex
                    beginning = self._next_sample
                    for item in pre.segments:
                        self._publish(item, ident)
                    self._placements.append(Placement(ident, pre.ad, beginning, self._next_sample))
                    self._boundary_pending = True  # boundary to first programme segment
            published = {seq for (epoch, seq, _), _ in self._source.items() if epoch == self._epoch}
            candidates = (self._candidates(playlist, source_segments, published, unseen)
                          if unseen else [])
            replacements = {}
            for start, end, _, ad in candidates:
                source_samples = sum(source_segments[s.uri].info.samples
                                     for s in incoming if start <= s.sequence < end)
                self._sample_delta += ad.samples - source_samples
                for sequence in range(start, end):
                    replacements[sequence] = (start, end, ad)
            for _, _, index, _ in sorted(candidates, key=lambda item: item[2], reverse=True):
                self._offers.pop(index)
            used = set()
            for s in unseen:
                key = (self._epoch, s.sequence, s.discontinuity)
                replacement = replacements.get(s.sequence)
                if replacement:
                    if replacement[0] not in used:
                        ad = replacement[2]
                        ident = 'replacement-' + uuid.uuid4().hex
                        beginning = self._next_sample
                        for i, item in enumerate(ad.segments):
                            self._publish(item, ident, discontinuity=(i == 0),
                                          source_disc=s.discontinuity if i == 0 else None)
                        self._placements.append(Placement(ident, ad.ad, beginning, self._next_sample))
                        used.add(replacement[0])
                    self._source[key] = None
                    self._last_source_disc = s.discontinuity
                    if s.sequence == replacement[1] - 1:
                        self._boundary_pending = True  # boundary to resumed programme
                else:
                    self._source[key] = self._publish(source_segments[s.uri], source_disc=s.discontinuity)
                self._highwater = s.sequence
            if any('#EXT-X-ENDLIST' in line for line in playlist.lines):
                self._ended = True
            self._trim()
            return self.snapshot()
