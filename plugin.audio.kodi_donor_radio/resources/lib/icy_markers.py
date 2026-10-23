# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure ICY marker classification; metadata remains untrusted observation data."""
from dataclasses import dataclass
from collections import deque
import re
import unicodedata
from typing import Optional

from models import Event


_COMMAND = re.compile(r'AD_START(?:_([0-9]{1,3}))?|AD_STOP|AD_END', re.ASCII)
_TILDE = re.compile(
    r'([^~^]+) ~ ([^~^]+) ~ ([0-9]{1,3}) ~ ([04])(?:( ~ I)(?: ~ ([1-9][0-9]{0,2}))?)? \^',
    re.ASCII)
_ATS_END = re.compile(r'##[^~^#]+ - [^~^#]+', re.ASCII)
_CANDIDATES = frozenset({
    'AD', 'AD BREAK', 'COMMERCIAL', 'SPOT', 'START', 'STOP',
    'AD START', 'AD STOP', 'AD END', 'BREAK START', 'BREAK END',
})


def _punctuation_only(value):
    return bool(value) and all(unicodedata.category(character).startswith('P')
                               for character in value)


@dataclass(frozen=True)
class MarkerSignal:
    kind: str
    marker: str
    format: str
    confidence: str
    duration_seconds: Optional[int]


def parse_marker(title: str) -> Optional[MarkerSignal]:
    """Recognize complete command and Triton forms, never embedded fragments."""
    if not isinstance(title, str):
        return None
    marker = title.strip()
    command = marker.upper()
    match = _COMMAND.fullmatch(command)
    if match:
        duration = int(match.group(1)) if match.group(1) else None
        if duration is not None and not 1 <= duration <= 300:
            return None
        return MarkerSignal('end' if command in ('AD_STOP', 'AD_END') else 'start',
                            command, 'ad_command', 'explicit', duration)
    provider_end = marker.startswith('##')
    tilde_value = marker[2:] if provider_end else marker
    match = _TILDE.fullmatch(tilde_value)
    if match:
        duration = int(match.group(3))
        if duration > 300 or (match.group(6) and int(match.group(6)) > 300):
            return None
        if match.group(4) != '4' and match.group(5):
            return None
        return MarkerSignal('end' if provider_end or match.group(4) == '0' else 'start',
                            marker, 'tildeats', 'explicit', duration)
    if _ATS_END.fullmatch(marker):
        return MarkerSignal('end', marker, 'tildeats', 'explicit', None)
    return None


class IcyMarkerTracker:
    """Bounded, station-scoped metadata observations; never changes audio."""

    def __init__(self, session_id, station_uuid, emit, learned_lookup):
        self.session_id = session_id
        self.station_uuid = station_uuid
        self.emit = emit
        self.learned_lookup = learned_lookup
        self.records = deque(maxlen=64)
        self._candidate = None
        self._open_inferred = None
        self._last_title = None
        self._last_boundary = None

    def _event(self, kind, metadata, observed_at, monotonic_at, signal,
               reason=None):
        title = metadata.get('StreamTitle', '')
        evidence = {'StreamTitle': title[:256] if isinstance(title, str) else ''}
        details = {'classification': signal.confidence, 'marker_format': signal.format,
                   'confidence': signal.confidence, 'duration': signal.duration_seconds}
        if reason is not None:
            details['reason'] = reason
        self.emit(Event(kind, self.session_id, self.station_uuid, 'icy',
                        'icy_marker', evidence, observed_at, monotonic_at, details))

    def expire(self, monotonic_at):
        """Prune old observations and report an unanswered inferred break once."""
        while self.records and monotonic_at - self.records[0][0] > 300:
            self.records.popleft()
        if self._candidate and monotonic_at - self._candidate[0] > 300:
            self._candidate = None
        if self._open_inferred and monotonic_at - self._open_inferred[0] > 300:
            _, metadata, observed_at, signal = self._open_inferred
            self._open_inferred = None
            self._event('break_unresolved', metadata, observed_at, monotonic_at,
                        signal, reason='timeout')
            self._last_boundary = None

    def latest_candidate(self):
        return self._candidate[1] if self._candidate else None

    def observe(self, metadata, observed_at, monotonic_at):
        self.expire(monotonic_at)
        title = metadata.get('StreamTitle') if isinstance(metadata, dict) else None
        if not isinstance(title, str):
            return
        marker = title.strip()
        normalized = marker.upper()
        if normalized == self._last_title:
            return
        self._last_title = normalized
        self.records.append((monotonic_at, marker[:256]))
        if not marker:
            return

        learned = self.learned_lookup(normalized)
        if learned in ('start', 'end'):
            signal = MarkerSignal(learned, normalized, 'learned', 'learned', None)
        else:
            signal = parse_marker(marker)
        if signal is not None:
            if signal.kind != self._last_boundary:
                self._event('break_' + signal.kind, metadata, observed_at,
                            monotonic_at, signal)
                self._last_boundary = signal.kind
            self._open_inferred = None
            return

        candidate = _punctuation_only(marker) or normalized in _CANDIDATES
        if candidate:
            signal = MarkerSignal('candidate', normalized, 'heuristic', 'candidate', None)
            self._candidate = (monotonic_at, normalized)
            self._event('icy_marker_candidate', metadata, observed_at,
                        monotonic_at, signal)
            if self._open_inferred is None and self._last_boundary != 'start':
                inferred = MarkerSignal('start', normalized, 'heuristic', 'inferred', None)
                self._open_inferred = (monotonic_at, metadata, observed_at, inferred)
                self._event('break_start', metadata, observed_at, monotonic_at, inferred)
                self._last_boundary = 'start'
            elif (self._open_inferred is not None
                  and normalized == self._open_inferred[3].marker):
                inferred = MarkerSignal('end', normalized, 'heuristic', 'inferred', None)
                self._open_inferred = None
                self._event('break_end', metadata, observed_at, monotonic_at, inferred)
                self._last_boundary = 'end'
            return

        if self._open_inferred is not None:
            signal = MarkerSignal('end', marker, 'heuristic', 'inferred', None)
            self._open_inferred = None
            self._event('break_end', metadata, observed_at, monotonic_at, signal)
            self._last_boundary = 'end'
