# SPDX-License-Identifier: GPL-2.0-or-later
"""Playback-evidenced VAST tracking and bounded best-effort beacon delivery."""
import hashlib
import math
import queue
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from ad_models import PlaybackSample
from url_policy import origin
from vast import substitute_macros


MAX_GAP = .75
MAX_STATIONARY = .75
START_TOLERANCE = .25
EVENT_FRACTIONS = (("firstQuartile", .25), ("midpoint", .5), ("thirdQuartile", .75))
ERROR_CODES = frozenset((100, 101, 102, 200, 300, 301, 302, 303, 400, 401, 402, 403, 405, 900))
_UNSUPPORTED_AD = frozenset(("Unsupported VAST pod", "Unsupported required VAST feature",
                              "Unsupported VAST creative"))
_PLAYER_FAILURE = frozenset(("Creative request failed", "Creative cache is closed",
                             "Creative cache is full or pinned", "Creative cache is full or closed"))


def _opaque(value):
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class _Progress:
    started: bool = False
    emitted: set = None
    final_interval: float = 0
    ambiguous: bool = False

    def __post_init__(self):
        self.emitted = set()


class AdTracker:
    """Accept only mapped, continuously played position evidence.

    emit receives a mapping with kind, event, placement_id, creative_id,
    reason, and session_id. Identifiers are opaque hashes; no URL is emitted.
    """
    def __init__(self, session_id, emit, enqueue):
        self.session_id, self.emit, self.enqueue = session_id, emit, enqueue
        self._previous = None
        self._last_good = None
        self._stationary_since = None
        self._seen_sample = False
        self._progress = {}
        self._presentation = None
        self._finished = False

    def _report(self, placement, event):
        state = self._progress[placement.id]
        if event in state.emitted:
            return
        state.emitted.add(event)
        queued = False
        for record in dict.fromkeys(placement.ad.tracking):
            if record.event != event:
                continue
            url = substitute_macros(record.url)
            if url is not None:
                queued = bool(self.enqueue(placement.id, event, url)) or queued
        self.emit({"kind": "ad_tracking", "session_id": self.session_id,
                   "event": event, "placement_id": _opaque(placement.id),
                   "creative_id": _opaque(placement.ad.id),
                   "reason": "queued" if queued else "no_endpoint"})

    def _taint_ambiguous(self, presentation, previous, sample):
        """Disable later success milestones for placements crossed without evidence."""
        if not sample.mapped or not math.isfinite(sample.position_seconds):
            return
        current = sample.position_seconds
        low, high = ((min(previous.position_seconds, current),
                      max(previous.position_seconds, current)) if previous is not None
                     else (current, current))
        for placement in presentation.placements:
            start = placement.start_sample / 48000
            end = placement.end_sample / 48000
            if start <= high and end >= low:
                state = self._progress.setdefault(placement.id, _Progress())
                state.ambiguous = True
                state.final_interval = 0

    def observe(self, presentation, sample):
        if self._finished or sample.session_id != self.session_id:
            return
        self._presentation = presentation
        good = (sample.mapped and sample.playing and not sample.paused and
                not sample.seeking and math.isfinite(sample.position_seconds) and
                math.isfinite(sample.monotonic_seconds))
        previous = self._previous
        first_sample = not self._seen_sample
        self._seen_sample = True
        continuous = False
        interval = 0
        if (good and previous is None and self._last_good is not None and
                abs(sample.position_seconds - self._last_good.position_seconds) > START_TOLERANCE):
            # A clock move across an unevidenced gap cannot prove this ad played.
            self._taint_ambiguous(presentation, self._last_good, sample)
        if good and previous is not None:
            interval = sample.monotonic_seconds - previous.monotonic_seconds
            advance = sample.position_seconds - previous.position_seconds
            continuous = (previous.mapped and previous.playing and not previous.paused and
                          not previous.seeking and 0 < interval <= MAX_GAP and
                          0 < advance <= interval + START_TOLERANCE)
            if advance > 0:
                self._stationary_since = None
            elif advance == 0 and interval > 0:
                if self._stationary_since is None:
                    self._stationary_since = previous.monotonic_seconds
                if sample.monotonic_seconds - self._stationary_since >= MAX_STATIONARY:
                    self._taint_ambiguous(presentation, previous, sample)
            else:
                self._stationary_since = None
            if (sample.seeking or interval <= 0 or advance < 0 or
                    (interval > MAX_GAP and advance > 0) or
                    advance > interval + START_TOLERANCE):
                self._taint_ambiguous(presentation, previous, sample)
        elif sample.seeking and sample.mapped:
            self._taint_ambiguous(presentation, previous, sample)
        for placement in presentation.placements:
            start = placement.start_sample / 48000
            end = placement.end_sample / 48000
            state = self._progress.setdefault(placement.id, _Progress())
            if state.ambiguous:
                state.final_interval = 0
                continue
            if not good:
                state.final_interval = 0
                continue
            position = sample.position_seconds
            near_start = (start <= position <= min(end, start + START_TOLERANCE) and
                          first_sample)
            crossed_start = continuous and previous.position_seconds <= start <= position
            if not state.started and (near_start or crossed_start):
                state.started = True
                self._report(placement, "impression")
                self._report(placement, "start")
            if not state.started or not continuous:
                state.final_interval = 0
                continue
            state.final_interval = interval if end - interval <= position <= end else 0
            for event, fraction in EVENT_FRACTIONS:
                mark = start + (end - start) * fraction
                if previous.position_seconds < mark <= position:
                    self._report(placement, event)
            if previous.position_seconds < end <= position:
                self._report(placement, "complete")
        self._previous = sample if good else None
        if good:
            self._last_good = sample
        if not good:
            self._stationary_since = None

    def finish(self, reason):
        if self._finished:
            return
        if reason == "ad_end" and self._previous is not None and self._presentation is not None:
            position = self._previous.position_seconds
            for placement in self._presentation.placements:
                state = self._progress.get(placement.id)
                end = placement.end_sample / 48000
                if (state is not None and state.started and state.final_interval > 0 and
                        0 <= end - position <= state.final_interval):
                    self._report(placement, "complete")
        if reason != "source_end":
            self._finished = True


class _CancelOrStop:
    def __init__(self, cancel, stop):
        self.cancel, self.stop = cancel, stop

    def is_set(self):
        return self.cancel.is_set() or self.stop.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.stop.wait(.02 if remaining is None else min(.02, remaining))
        return True


class BeaconWorker:
    """One bounded worker; ambiguous transport failure is never retried."""
    def __init__(self, client, cancel, fixture_origin):
        self.client, self.fixture_origin = client, fixture_origin
        self._cancel = cancel
        self._stop = threading.Event()
        self._request_cancel = _CancelOrStop(cancel, self._stop)
        self._queue = queue.Queue(maxsize=128)
        self._seen = set()
        self._lock = threading.Lock()
        self.failures = 0
        self.overflow = 0
        self._thread = threading.Thread(target=self._run, name="ad-beacons", daemon=True)
        self._thread.start()

    def enqueue(self, placement_id, event, url):
        try:
            parts = urlsplit(url)
            if (origin(url) != origin(self.fixture_origin) or len(url) > 8192 or
                    parts.scheme not in ("http", "https") or
                    parts.username is not None or parts.password is not None or
                    any(ord(char) < 33 or ord(char) == 127 for char in url)):
                return False
        except ValueError:
            return False
        key = (placement_id, event, url)
        with self._lock:
            if self._stop.is_set() or self._cancel.is_set() or key in self._seen:
                return False
            self._seen.add(key)
            try:
                self._queue.put_nowait(key)
            except queue.Full:
                self.overflow += 1
                return False
        return True

    def _run(self):
        while not self._stop.is_set() and not self._cancel.is_set():
            try:
                _, _, url = self._queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                if not self._request_cancel.is_set():
                    with self.client.open(url, {}, self._request_cancel,
                                          deadline=time.monotonic() + 2,
                                          allowed_origin=self.fixture_origin) as response:
                        response.read(1)
                        if response.status < 200 or response.status >= 300:
                            raise OSError("Beacon response failed")
            except Exception:
                with self._lock:
                    self.failures += 1
            finally:
                self._queue.task_done()

    def close(self):
        # Allow fast sends to drain, then cancel any slow request and discard
        # pending work. Every request has its own two-second transport deadline.
        deadline = time.monotonic() + .2
        while (not self._cancel.is_set() and self._queue.unfinished_tasks and
               time.monotonic() < deadline):
            time.sleep(.005)
        self._stop.set()
        self._thread.join(timeout=2.1)


def enqueue_ad_error(worker, placement_id, error):
    """Dispatch inherited VAST Error URLs with a verified fixed code."""
    code = error.code if error.code in ERROR_CODES else 900
    # Resolver/media errors predate the beacon dispatcher. Normalize their
    # broad internal categories to the narrower VAST 4.2 error definitions.
    if code == 403:
        if error.reason in _UNSUPPORTED_AD:
            code = 200
        elif error.reason == "Invalid VAST duration":
            code = 101
        elif error.reason != "No supported VAST media":
            code = (900 if error.reason == "Advertising origin rejected" else
                    400 if error.reason in _PLAYER_FAILURE else 405)
    elif code == 100:
        if error.reason == "Unsupported VAST document":
            code = 900
        elif error.reason in ("Invalid advertising URL", "Invalid VAST ad"):
            code = 101
        elif error.reason == "Advertising origin rejected":
            code = 900
    elif code == 302 and error.reason == "Wrapper following disabled":
        code = 300
    count = 0
    for raw in dict.fromkeys(error.error_urls):
        url = substitute_macros(raw, error_code=code)
        if url is not None and worker.enqueue(placement_id, "error", url):
            count += 1
    return count
