# SPDX-License-Identifier: GPL-2.0-or-later
"""Serialize explicit HLS cue evidence without making advertisement claims.

Times describe reception or the upstream media timeline, never Kodi audibility.
SCTE payload validation is syntactic only; command payloads are not decoded.
"""
import base64
import binascii
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import threading
from typing import Callable, Optional

from hls import CueObservation, Playlist, parse_iso_datetime
from models import Event, EventSink


DEDUP_LIMIT = 4096
STREAM_LIMIT = 128


def _number(value):
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        raise ValueError('invalid_duration')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('invalid_duration')
    return number


def _date(value):
    if value is None:
        return None
    parsed = parse_iso_datetime(value)
    if 'T' not in value or parsed.tzinfo is None:
        raise ValueError('invalid_date')
    return parsed.astimezone(timezone.utc)


def _payload(value, hex_only=False):
    if value.startswith(('0x', '0X')):
        raw = value[2:]
        if not raw or len(raw) % 2 or not re.fullmatch('[a-fA-F0-9]+', raw):
            raise ValueError('invalid_payload')
        data = bytes.fromhex(raw)
    elif not hex_only:
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('invalid_payload')
    else:
        raise ValueError('invalid_payload')
    # SCTE-35 splice_info_section table_id. This does not decode splice commands.
    if not data or data[0] != 0xfc:
        raise ValueError('invalid_payload')
    return data.hex()


@dataclass
class _Break:
    cue: CueObservation
    rendition: str
    epoch: int
    cue_id: str
    family: str
    position: Optional[float]
    duration: Optional[float]
    semantic: tuple
    last_sequence: int
    expected_origin: Optional[float]
    closed: bool = False
    unresolved_reported: bool = False


class CueTracker:
    def __init__(self, session_id: str, station_uuid: str, sink: EventSink,
                 utc_now: Callable, monotonic_now: Callable):
        self.session_id = session_id
        self.station_uuid = station_uuid
        self.sink = sink
        self.utc_now = utc_now
        self.monotonic_now = monotonic_now
        self._lock = threading.Lock()
        self._seen = OrderedDict()
        self._breaks = OrderedDict()
        self._streams = OrderedDict()
        self._finished = False

    def _emit(self, kind, cue, rendition, epoch, classification='signaled',
              position=None, duration=None, cue_id=None, reason=None):
        now = self.utc_now()
        if isinstance(now, datetime):
            now = now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        self.sink.emit(Event(
            kind, self.session_id, self.station_uuid, 'hls', cue.source,
            dict(cue.attributes), now, self.monotonic_now(),
            {'rendition_id': rendition, 'epoch': epoch, 'media_sequence': cue.sequence,
             'discontinuity': cue.discontinuity, 'media_position': position,
             'duration': duration, 'cue_id': cue_id, 'classification': classification,
             'reason': reason}))

    def _unresolved(self, record, reason, terminal=True):
        if not record.closed:
            record.closed = terminal
            if not record.unresolved_reported:
                record.unresolved_reported = True
                self._emit('break_unresolved', record.cue, record.rendition, record.epoch,
                           position=record.position, duration=record.duration,
                           cue_id=record.cue_id, reason=reason)

    def _close_matching(self, rendition, reason, predicate=lambda record: True):
        for record in self._breaks.values():
            if record.rendition == rendition and predicate(record):
                self._unresolved(record, reason)

    def _remember(self, key):
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > DEDUP_LIMIT:
            self._seen.popitem(last=False)
        return True

    def _semantics(self, cue):
        attrs = {key.upper(): value for key, value in cue.attributes.items()}
        date_range = cue.source == 'EXT-X-DATERANGE'
        if date_range and not any(k in attrs for k in ('SCTE35-OUT', 'SCTE35-IN', 'SCTE35-CMD')):
            return None
        duration = _number(attrs.get('DURATION', attrs.get('PLANNED-DURATION')))
        elapsed = _number(attrs.get('ELAPSEDTIME'))
        if cue.source == 'EXT-X-CUE-OUT-CONT' and 'RAW' in attrs:
            pieces = attrs['RAW'].split('/')
            if len(pieces) != 2:
                raise ValueError('invalid_continuation')
            elapsed, duration = (_number(value) for value in pieces)
        start, end = _date(attrs.get('START-DATE')), _date(attrs.get('END-DATE'))
        payloads = tuple((key, _payload(attrs[key], date_range))
                         for key in ('SCTE35-OUT', 'SCTE35-IN', 'SCTE35-CMD', 'SCTE35')
                         if key in attrs)
        if date_range and not attrs.get('ID'):
            raise ValueError('missing_cue_id')
        if start and end and end < start:
            raise ValueError('invalid_date_order')
        if date_range:
            action = ('end' if 'SCTE35-IN' in attrs else
                      'start' if 'SCTE35-OUT' in attrs else 'opaque')
        else:
            action = {'EXT-X-CUE-OUT': 'start', 'EXT-X-CUE-OUT-CONT': 'progress',
                      'EXT-X-CUE-IN': 'end'}.get(cue.source, 'opaque')
        # Unknown attributes are evidence, not meaningful lifecycle updates.
        semantic = (action, duration, elapsed, start.isoformat() if start else None,
                    end.isoformat() if end else None, payloads)
        return attrs, action, duration, elapsed, start, semantic

    def _timeline(self, playlist, state, rendition):
        segments = playlist.segments
        if not segments:
            return None, None
        positions = state['positions']
        offset = 0.0 if state['last'] is None else None
        for segment in segments:
            previous = positions.get((segment.sequence, segment.discontinuity))
            if previous is not None:
                offset = previous - segment.media_position
                break
        if state['last'] is not None and segments[0].sequence == state['last'] + 1:
            offset = state['end']
        if state['last'] is not None and segments[0].sequence > state['last'] + 1:
            self._close_matching(rendition, 'timeline_gap')
        for segment in segments:
            positions[(segment.sequence, segment.discontinuity)] = (
                offset + segment.media_position if offset is not None else None)
        while len(positions) > DEDUP_LIMIT:
            positions.popitem(last=False)
        last = segments[-1]
        end = offset + last.media_position + last.duration if offset is not None else None
        if state['last'] is None or last.sequence >= state['last']:
            state['last'], state['end'] = last.sequence, end
        return offset, end

    def _advance_discontinuity(self, rendition, epoch, state, disc, bridge=None):
        keep = None
        if bridge is not None and disc == state['disc'] + 1:
            try:
                parsed = self._semantics(bridge)
            except ValueError:
                parsed = None
            if parsed is not None and parsed[1] in ('progress', 'end'):
                candidates = [r for r in self._breaks.values()
                              if r.rendition == rendition and r.epoch == epoch
                              and r.family == 'cue' and not r.closed
                              and r.cue.discontinuity < disc and r.last_sequence <= bridge.sequence
                              and (not parsed[0].get('ID') or parsed[0]['ID'] == r.cue_id)]
                if len(candidates) == 1:
                    keep = candidates[0]
                    # Identity survives explicit continuation, not a timestamp assumption.
                    # A CONT with explicit elapsed time may establish a fresh origin.
                    keep.expected_origin = None
        self._close_matching(rendition, 'discontinuity',
                             lambda r: r.family == 'cue' and r.cue.discontinuity < disc and r is not keep)
        state['disc'] = disc

    def observe(self, playlist: Playlist, rendition_id: str, epoch: int) -> None:
        with self._lock:
            if self._finished or playlist.is_master:
                return
            state = self._streams.get(rendition_id)
            if state is not None and epoch < state['epoch']:
                return  # A late response from a retired caller epoch cannot revive it.
            if state is None or state['epoch'] != epoch:
                self._close_matching(rendition_id, 'epoch_changed')
                state = {'epoch': epoch, 'disc': -1, 'last': None, 'end': None,
                         'positions': OrderedDict()}
                self._streams[rendition_id] = state
                if len(self._streams) > STREAM_LIMIT:
                    retired, _ = self._streams.popitem(last=False)
                    self._close_matching(retired, 'retention_limit')
            self._streams.move_to_end(rendition_id)
            offset, end = self._timeline(playlist, state, rendition_id)
            first_segments, first_cues = {}, {}
            for segment in playlist.segments:
                first_segments.setdefault(segment.discontinuity, segment.sequence)
            for cue in playlist.cues:
                if cue.source != 'EXT-X-DATERANGE':
                    first_cues.setdefault(cue.discontinuity, cue)
            for cue in playlist.cues:
                if cue.discontinuity > state['disc']:
                    bridge = first_cues.get(cue.discontinuity)
                    if bridge is not None and bridge.sequence != first_segments.get(cue.discontinuity, bridge.sequence):
                        bridge = None
                    self._advance_discontinuity(rendition_id, epoch, state, cue.discontinuity, bridge)
                self._observe_cue(cue, rendition_id, epoch, state, offset)
            max_disc = max((s.discontinuity for s in playlist.segments), default=state['disc'])
            if max_disc > state['disc']:
                self._advance_discontinuity(rendition_id, epoch, state, max_disc)
            if end is not None:
                for record in self._breaks.values():
                    if (record.rendition == rendition_id and record.epoch == epoch
                            and record.expected_origin is not None and record.duration is not None
                            and end >= record.expected_origin + record.duration):
                        # Expected expiry is evidence of a missing boundary,
                        # not terminal: a later published IN can still match.
                        self._unresolved(record, 'expected_duration_elapsed', terminal=False)

    def _observe_cue(self, cue, rendition, epoch, state, offset):
        position = (cue.media_position + offset
                    if cue.media_position is not None and offset is not None else None)
        try:
            parsed = self._semantics(cue)
        except ValueError:
            fingerprint = hashlib.sha256(json.dumps(sorted(cue.attributes.items())).encode()).digest()
            if self._remember((rendition, epoch, cue.discontinuity, cue.source, cue.sequence, fingerprint)):
                self._emit('cue_observed', cue, rendition, epoch, 'invalid', position=position)
            return
        if parsed is None:
            return
        attrs, action, duration, elapsed, start, semantic = parsed
        family = 'daterange' if cue.source == 'EXT-X-DATERANGE' else 'cue'
        scope = (rendition, epoch, None if family == 'daterange' else cue.discontinuity, family)
        cue_id = attrs.get('ID', str(cue.sequence))
        key = scope + (cue_id,)
        scoped = [r for r in self._breaks.values()
                  if r.rendition == rendition and r.epoch == epoch and r.family == family]
        active = [r for r in scoped if not r.closed]
        stale = family == 'cue' and any(r.last_sequence > cue.sequence for r in scoped)
        compatible = [r for r in active if r.last_sequence <= cue.sequence]
        record = self._breaks.get(key)
        if family == 'cue' and action in ('progress', 'end') and compatible:
            record = max(compatible, key=lambda r: r.last_sequence)
            key = scope + (record.cue_id,)
            cue_id = record.cue_id
        # CONT/IN observation identity must not change when their linked break closes.
        identity = cue.sequence if family == 'cue' and action in ('progress', 'end') else cue_id
        fingerprint = hashlib.sha256(repr(semantic).encode()).digest()
        if not self._remember(scope + (cue.source, identity, fingerprint)):
            return
        classification = 'opaque' if action == 'opaque' else 'signaled'
        now = self.utc_now()
        past_start = (start is not None and isinstance(now, datetime)
                      and start < now.astimezone(timezone.utc))
        if action == 'progress' or (action == 'start' and past_start and record is None):
            classification = 'in_progress'
        self._emit('cue_observed', cue, rendition, epoch, classification, position, duration, cue_id)
        if action == 'opaque' or stale or (family == 'cue' and cue.discontinuity < state['disc']):
            return
        if record is not None:
            record.last_sequence = max(record.last_sequence, cue.sequence)
        if action == 'end':
            if record is not None and not record.closed:
                record.closed = True
                self._emit('break_end', cue, rendition, epoch, position=position,
                           duration=duration if duration is not None else record.duration, cue_id=cue_id)
            return
        if record is not None:
            if not record.closed:
                # Continuation elapsed time is evidence, not a new boundary.
                changed = ((duration is not None and duration != record.duration)
                           or semantic[3:5] != record.semantic[3:5])
                record.semantic = semantic
                record.cue = cue
                if action == 'progress' and elapsed is not None and position is not None:
                    record.expected_origin = position - elapsed
                if duration is not None:
                    record.duration = duration
                if changed:
                    self._emit('break_update', cue, rendition, epoch, position=position,
                               duration=duration, cue_id=cue_id)
            return
        if family == 'cue':
            for old in active:
                self._unresolved(old, 'superseded')
        # Keep inferred origin separate from the observed continuation position.
        # It remains usable when duration is first supplied in a later update.
        expected_origin = position - (elapsed or 0.0) if position is not None else None
        record = _Break(cue, rendition, epoch, cue_id, family, position, duration,
                        semantic, cue.sequence, expected_origin)
        self._breaks[key] = record
        if len(self._breaks) > DEDUP_LIMIT:
            _, retired = self._breaks.popitem(last=False)
            self._unresolved(retired, 'retention_limit')
        if classification != 'in_progress':
            self._emit('break_start', cue, rendition, epoch, position=position,
                       duration=duration, cue_id=cue_id)

    def finish(self, reason: str) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            for record in self._breaks.values():
                self._unresolved(record, reason)
            self._streams.clear()
            self._seen.clear()
            self._breaks.clear()
