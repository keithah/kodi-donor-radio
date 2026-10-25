# SPDX-License-Identifier: GPL-2.0-or-later
"""Main-thread Kodi playback observations for one relay session at a time."""
from datetime import datetime, timezone
import math
import time
from urllib.parse import urlsplit

from ad_models import PlaybackSample
from ad_tracking import AdTracker, BeaconWorker, enqueue_ad_error
from models import Event


class KodiSessionClockAnchor:
    """Map Kodi media time to the immutable sample-zero session timeline.

    Composed fixture playlists explicitly start at TIME-OFFSET=0. Kodi's
    getTime() is media-relative, so the session origin is fixed at zero and is
    never recalculated from a sliding playlist window. A backward clock reset
    invalidates this anchor for the rest of the session.
    """
    BACKWARD_TOLERANCE = .05

    def __init__(self, session_id):
        self.session_id = session_id
        self.valid = True
        self.last_clock = None

    def invalidate(self):
        self.valid = False

    def __call__(self, session, _presentation, seconds):
        if (not self.valid or getattr(session, 'id', None) != self.session_id or
                not math.isfinite(seconds) or seconds < 0):
            return None
        if self.last_clock is not None:
            if seconds < self.last_clock - self.BACKWARD_TOLERANCE:
                self.invalidate()
                return None
            seconds = max(seconds, self.last_clock)
        self.last_clock = seconds
        return seconds


class KodiAdPlayback:
    """Map Kodi time only for opted-in loopback fixture sessions."""
    def __init__(self, manager, player, client=None, fixture_origin=None):
        self.manager, self.player = manager, player
        self.client = client or getattr(manager, 'client', None)
        self.fixture_origin = fixture_origin
        self.mapper = None
        self.mapping_valid = False
        self.session_id = None
        self.session = None
        self.tracker = None
        self.worker = None
        self.paused = False
        self.seeking = False
        self.last_sample = None

    def enable_mapping(self, mapper):
        """Install a fixture mapper returning output seconds or None."""
        self.mapper = mapper
        self.mapping_valid = False

    @staticmethod
    def _is_loopback_fixture(value):
        try:
            parts = urlsplit(value)
            return (parts.scheme == 'http' and parts.hostname == '127.0.0.1' and
                    parts.port is not None and parts.path == '' and not parts.query and
                    not parts.fragment and parts.username is None and parts.password is None and
                    value == 'http://127.0.0.1:%d' % parts.port)
        except (TypeError, ValueError):
            return False

    def _emit(self, record):
        if self.session is None:
            return
        self.manager.sink.emit(Event(record['kind'], self.session_id,
                                     self.session.station.uuid, self.session.source.protocol,
                                     'kodi_player', record['event'],
                                     datetime.now(timezone.utc).isoformat(), time.monotonic(),
                                     {key: record[key] for key in ('placement_id', 'creative_id', 'reason')}))

    def bind(self, session_id):
        if session_id == self.session_id:
            return
        self.close()
        session = self.manager.get(session_id) if hasattr(self.manager, 'get') else None
        if session is None:
            return
        self.session_id, self.session = session_id, session
        self.paused = self.seeking = False
        self.mapping_valid = False
        self.mapper = None
        if self._is_loopback_fixture(self.fixture_origin) and getattr(session, 'ad_enabled', False):
            self.worker = BeaconWorker(self.client, session.cancel, self.fixture_origin)
            self.tracker = AdTracker(session_id, self._emit, self.worker.enqueue)
            # The sample-zero mapping is deliberately limited to explicit
            # loopback fixtures; normal Radio Browser sessions stay unmapped.
            self.enable_mapping(KodiSessionClockAnchor(session_id))

    def sample(self):
        session = self.session
        if session is None or self.manager.get(self.session_id) is not session or session.cancel.is_set():
            self.close()
            return
        self._flush_errors()
        presentation = session.presentation()
        if presentation is None or self.tracker is None:
            return
        now = time.monotonic()
        try:
            playing = bool(self.player.isPlaying())
            clock = float(self.player.getTime()) if playing else None
            position = self.mapper(session, presentation, clock) if self.mapper and clock is not None else None
            mapped = position is not None and math.isfinite(position) and position >= 0
        except (RuntimeError, ValueError, TypeError, OverflowError):
            playing, position, mapped = False, None, False
        self.mapping_valid = mapped
        self.last_sample = PlaybackSample(self.session_id, position if mapped else 0.0,
                                          now, playing, self.paused, self.seeking, mapped)
        self.tracker.observe(presentation, self.last_sample)
        self.seeking = False

    def _flush_errors(self):
        session = self.session
        if self.worker is None or session is None or session.cancel.is_set():
            return
        for error in session.take_ad_errors():
            enqueue_ad_error(self.worker, self.session_id, error)
            self.manager.sink.emit(Event('ad_error', self.session_id, session.station.uuid,
                                         session.source.protocol, 'vast', error.code,
                                         datetime.now(timezone.utc).isoformat(), time.monotonic(),
                                         {'reason': error.reason}))

    def on_seek(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self.seeking = True
            self.mapping_valid = False
            if hasattr(self.mapper, 'invalidate'):
                self.mapper.invalidate()

    def on_pause(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self.paused = True

    def on_resume(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self.paused = False

    def on_end(self, reason, session_id=None):
        if session_id is not None and session_id != self.session_id:
            return
        self._flush_errors()
        if self.tracker is not None:
            self.tracker.finish(reason)
        self.close()

    def close(self):
        if self.tracker is not None:
            self.tracker.finish('cancelled')
        if self.worker is not None:
            self.worker.close()
        self.session_id = self.session = self.tracker = self.worker = None
        self.mapper = None
        self.mapping_valid = False
        self.last_sample = None
        self.paused = self.seeking = False
