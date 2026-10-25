# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure ICY marker classification; metadata remains untrusted observation data."""
from dataclasses import dataclass
from collections import OrderedDict, deque
import re
import unicodedata
from typing import Optional

from models import Event
from icy_marker_store import normalize_marker


_COMMAND = re.compile(r'AD_START(?:_([0-9]{1,3}))?|AD_STOP|AD_END', re.ASCII)
_TILDE = re.compile(
    r'([^~^]+) ~ ([^~^]+) ~ ([0-9]{1,3}) ~ ([04])(?:( ~ I)(?: ~ ([1-9][0-9]{0,2}))?)? \^',
    re.ASCII)
_ATS_END = re.compile(r'##[^~^#]+ - [^~^#]+', re.ASCII)
_CANDIDATES = frozenset({
    'AD', 'AD BREAK', 'COMMERCIAL', 'SPOT', 'START', 'STOP',
    'AD START', 'AD STOP', 'AD END', 'BREAK START', 'BREAK END',
})
_WRAPPED_TOKEN = re.compile(
    r'(?P<frame>[#*_-])(?P<token>[A-Z0-9]+(?:[ _-][A-Z0-9]+)*)'
    r'(?P=frame)', re.ASCII)
_MAX_MARKER_BYTES = 256
_MAX_WRAPPED_TOKEN_CHARS = 64
_ADSWIZZ_DURATION_MS = re.compile(r'[0-9]{1,6}', re.ASCII)
_ADSWIZZ_INSERTION_TYPE = re.compile(r'[a-z][a-z0-9_-]{0,31}', re.ASCII)


def _punctuation_only(value):
    return bool(value) and all(unicodedata.category(character).startswith('P')
                               for character in value)


def _framed_token(value):
    match = _WRAPPED_TOKEN.fullmatch(value)
    if (match is None or len(match.group('token')) > _MAX_WRAPPED_TOKEN_CHARS
            or not re.search(r'[A-Z]', match.group('token'), re.ASCII)):
        return None
    return match


def adswizz_insertion_type(value):
    if not isinstance(value, str) or not _ADSWIZZ_INSERTION_TYPE.fullmatch(value.lower()):
        return None
    return value.lower()


def adswizz_duration_ms(value):
    if not isinstance(value, str) or not _ADSWIZZ_DURATION_MS.fullmatch(value):
        return None
    milliseconds = int(value)
    return milliseconds if 1 <= milliseconds <= 300000 else None


def adswizz_evidence(metadata):
    """Return only validated Adswizz fields; omit opaque context and ad IDs."""
    if not isinstance(metadata, dict):
        return {}
    evidence = {}
    ad_flag = metadata.get('adw_ad')
    if isinstance(ad_flag, str) and ad_flag.lower() in ('true', 'false'):
        evidence['adw_ad'] = ad_flag.lower()
    insertion_type = adswizz_insertion_type(metadata.get('insertionType'))
    if insertion_type is not None:
        evidence['insertionType'] = insertion_type
    duration = adswizz_duration_ms(metadata.get('durationMilliseconds'))
    if duration is not None:
        evidence['durationMilliseconds'] = str(duration)
    return evidence


def _adswizz_signal(metadata):
    """Recognize Adswizz's explicit ICY ad-state field, without its context token."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get('adw_ad')
    if not isinstance(value, str) or value.lower() not in ('true', 'false'):
        return None
    duration = None
    milliseconds = adswizz_duration_ms(metadata.get('durationMilliseconds'))
    if milliseconds is not None:
        duration = milliseconds / 1000.0
    kind = 'start' if value.lower() == 'true' else 'end'
    return MarkerSignal(kind, 'adw_ad=' + value.lower(), 'adswizz', 'explicit', duration)


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
    command_value = command
    if match is None:
        wrapped = _framed_token(command)
        if wrapped is not None:
            command_value = wrapped.group('token')
            match = _COMMAND.fullmatch(command_value)
    if match:
        duration = int(match.group(1)) if match.group(1) else None
        if duration is not None and not 1 <= duration <= 300:
            return None
        return MarkerSignal('end' if command_value in ('AD_STOP', 'AD_END') else 'start',
                            command, 'ad_command', 'explicit', duration)
    provider_end = marker.startswith('##')
    tilde_value = marker[2:] if provider_end else marker
    match = _TILDE.fullmatch(tilde_value)
    if match:
        duration = int(match.group(3))
        is_ad_start = not provider_end and match.group(4) == '4'
        if is_ad_start and (duration > 300 or
                            (match.group(6) and int(match.group(6)) > 300)):
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
        self._last_adswizz = None
        self._last_boundary = None
        self._last_boundary_at = None
        self._session_overrides = OrderedDict()

    def _event(self, kind, metadata, observed_at, monotonic_at, signal,
               reason=None):
        title = metadata.get('StreamTitle')
        evidence = {'StreamTitle': title[:256]} if isinstance(title, str) else {}
        evidence.update(adswizz_evidence(metadata))
        details = {'classification': signal.confidence, 'marker_format': signal.format,
                   'confidence': signal.confidence, 'duration': signal.duration_seconds}
        insertion_type = adswizz_insertion_type(metadata.get('insertionType'))
        if signal.format == 'adswizz' and insertion_type is not None:
            details['insertion_type'] = insertion_type.lower()
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
        if self._last_title and monotonic_at - self._last_title[0] > 300:
            self._last_title = None
        if (self._last_boundary == 'start' and self._open_inferred is None
                and self._last_boundary_at is not None
                and monotonic_at - self._last_boundary_at > 300):
            self._last_boundary = None
            self._last_boundary_at = None
        if self._open_inferred and monotonic_at - self._open_inferred[0] > 300:
            _, metadata, observed_at, signal = self._open_inferred
            self._open_inferred = None
            self._event('break_unresolved', metadata, observed_at, monotonic_at,
                        signal, reason='timeout')
            self._last_boundary = None

    def _title_state(self, marker):
        normalized = marker.upper()
        learned = ((self._session_overrides.get(normalized) or self.learned_lookup(normalized))
                   if marker else None)
        signal = (MarkerSignal(learned, normalized, 'learned', 'learned', None)
                  if learned in ('start', 'end') else parse_marker(marker))
        framed = _framed_token(normalized) is not None
        candidate = (signal is None and len(marker.encode('utf-8')) <= _MAX_MARKER_BYTES and
                     (_punctuation_only(marker) or normalized in _CANDIDATES or framed))
        classification = ((signal.kind, signal.format, signal.confidence)
                          if signal is not None else ('candidate',) if candidate
                          else ('ordinary',))
        return normalized, signal, candidate, classification

    def latest_candidate(self):
        return self._candidate[1] if self._candidate else None

    def label(self, marker, action, observed_at, monotonic_at):
        normalized = normalize_marker(marker)
        if action not in ('start', 'end'):
            raise ValueError('Invalid marker action')
        self.expire(monotonic_at)
        if marker != self.latest_candidate():
            return False
        self._session_overrides[normalized] = action
        self._session_overrides.move_to_end(normalized)
        if len(self._session_overrides) > 64:
            self._session_overrides.popitem(last=False)
        signal = MarkerSignal(action, normalized, 'learned', 'learned', None)
        metadata = {'StreamTitle': marker}
        self._event('icy_marker_labeled', metadata, observed_at, monotonic_at,
                    signal, reason=action)
        if self._last_boundary != action:
            self._event('break_' + action, metadata, observed_at, monotonic_at, signal)
            self._last_boundary = action
        self._last_boundary_at = monotonic_at
        self._open_inferred = ((monotonic_at, metadata, observed_at, signal)
                               if action == 'start' else None)
        if self._last_title is not None and self._last_title[1] == normalized:
            self._last_title = (monotonic_at, normalized, (action, 'learned', 'learned'))
        return True

    def observe(self, metadata, observed_at, monotonic_at):
        self.expire(monotonic_at)
        adwizz = _adswizz_signal(metadata)
        if adwizz is not None:
            title = metadata.get('StreamTitle')
            if isinstance(title, str):
                normalized_title, _, _, title_classification = self._title_state(title.strip())
                self._last_title = (monotonic_at, normalized_title, title_classification)
            elif adwizz.kind == 'start':
                self._last_title = None
            insertion = metadata.get('insertionType', '')
            duration = metadata.get('durationMilliseconds', '')
            normalized = 'adswizz:%s:%s:%s' % (
                adwizz.kind, insertion.lower() if isinstance(insertion, str) else '', duration)
            classification = (adwizz.kind, adwizz.format, adwizz.confidence)
            signature = (normalized, classification)
            if self._last_adswizz == signature:
                if adwizz.kind == 'end':
                    return
                if self._open_inferred is not None:
                    self._open_inferred = (monotonic_at, metadata, observed_at, adwizz)
                    self._last_boundary_at = monotonic_at
                    return
                if self._last_boundary == 'start':
                    self._open_inferred = (monotonic_at, metadata, observed_at, adwizz)
                    self._last_boundary_at = monotonic_at
                    return
            self._last_adswizz = signature
            self.records.append((monotonic_at, normalized[:256]))
            if adwizz.kind == 'start':
                event_kind = 'break_update' if self._last_boundary == 'start' else 'break_start'
                self._event(event_kind, metadata, observed_at, monotonic_at, adwizz)
                self._last_boundary = 'start'
                self._last_boundary_at = monotonic_at
                self._open_inferred = (monotonic_at, metadata, observed_at, adwizz)
            elif self._last_boundary == 'start':
                self._event('break_end', metadata, observed_at, monotonic_at, adwizz)
                self._last_boundary = 'end'
                self._last_boundary_at = monotonic_at
                self._open_inferred = None
            return
        title = metadata.get('StreamTitle') if isinstance(metadata, dict) else None
        if not isinstance(title, str):
            return
        marker = title.strip()
        normalized, signal, candidate, classification = self._title_state(marker)
        if (self._last_title is not None
                and (normalized, classification) == self._last_title[1:]):
            return
        self._last_adswizz = None
        self._last_title = (monotonic_at, normalized, classification)
        self.records.append((monotonic_at, marker[:256]))
        if not marker:
            return

        if signal is not None:
            if signal.kind != self._last_boundary:
                self._event('break_' + signal.kind, metadata, observed_at,
                            monotonic_at, signal)
                self._last_boundary = signal.kind
            self._last_boundary_at = monotonic_at
            self._open_inferred = ((monotonic_at, metadata, observed_at, signal)
                                   if signal.confidence == 'learned'
                                   and signal.kind == 'start' else None)
            return

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
                self._last_boundary_at = monotonic_at
            elif (self._open_inferred is not None
                  and normalized == self._open_inferred[3].marker):
                inferred = MarkerSignal('end', normalized, 'heuristic', 'inferred', None)
                self._open_inferred = None
                self._event('break_end', metadata, observed_at, monotonic_at, inferred)
                self._last_boundary = 'end'
                self._last_boundary_at = monotonic_at
            return

        if self._open_inferred is not None:
            signal = MarkerSignal('end', marker, 'heuristic', 'inferred', None)
            self._open_inferred = None
            self._event('break_end', metadata, observed_at, monotonic_at, signal)
            self._last_boundary = 'end'
            self._last_boundary_at = monotonic_at
