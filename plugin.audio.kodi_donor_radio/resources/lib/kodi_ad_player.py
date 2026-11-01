# SPDX-License-Identifier: GPL-2.0-or-later
"""Main-thread Kodi playback observations for one relay session at a time."""
from datetime import datetime, timezone
import math
import time
from urllib.parse import urlsplit

from ad_models import PlaybackSample
from ad_tracking import (MAX_GAP, START_TOLERANCE, AdTracker, BeaconWorker,
                         enqueue_ad_error)
from models import Event


class KodiSessionClockAnchor:
    """Candidate sample-zero mapping for initially finite loopback fixtures.

    Live fixtures can start partway through the presentation and their public
    clock can freeze while buffered audio continues. Neither TIME-OFFSET=0 nor
    a later ENDLIST proves their origin. Only a presentation first published
    with ENDLIST and retaining sample zero may use this experimental candidate.
    It is never a proven mapping for ordinary station sessions. A backward
    clock reset invalidates it for the rest of the session.
    """
    BACKWARD_TOLERANCE = .05

    def __init__(self, session_id):
        self.session_id = session_id
        self.valid = True
        self.last_clock = None
        self.startup_pending = True
        self.reason = None

    def invalidate(self, reason=None):
        self.valid = False
        if self.reason is None:
            self.reason = reason

    def confirm_startup(self):
        """Make later backward jumps permanent clock failures."""
        self.startup_pending = False

    def _backward_reset(self):
        if self.startup_pending:
            # Kodi may reset a just-started stream from zero to a negative
            # pre-roll value. Let the adapter's progress gate re-anchor safely.
            self.last_clock = None
            return True
        self.invalidate()
        return False

    def __call__(self, session, presentation, seconds):
        if (not self.valid or getattr(session, 'id', None) != self.session_id or
                not math.isfinite(seconds)):
            return None
        if presentation is None or not presentation.segments:
            return None
        if not presentation.initially_finite or not presentation.ended:
            self.invalidate('live_clock_unanchored')
            return None
        if presentation.segments[0].start_sample != 0:
            self.invalidate('sample_zero_unavailable')
            return None
        return self._clock_position(seconds)

    def _clock_position(self, seconds):
        if seconds < 0:
            if (self.last_clock is not None and
                    seconds < self.last_clock - self.BACKWARD_TOLERANCE):
                self._backward_reset()
                return None
            if self.last_clock is None:
                return None
            seconds = self.last_clock
        if self.last_clock is not None:
            if seconds < self.last_clock - self.BACKWARD_TOLERANCE:
                self._backward_reset()
                return None
            seconds = max(seconds, self.last_clock)
        self.last_clock = seconds
        return seconds


class KodiContinuousClockAnchor(KodiSessionClockAnchor):
    """Diagnostic sample-zero candidate for the exact continuous AAC transport.

    A completed socket write only bounds possible playback. It does not prove
    that Kodi played those frames or establish success-beacon eligibility.
    """
    DELIVERED_TOLERANCE = 1024 / 48000  # One AAC frame, in seconds.

    def __init__(self, session_id):
        super().__init__(session_id)
        self.last_next_sample = 0
        self.pending_reason = None

    def _backward_reset(self):
        if not self.startup_pending:
            self.invalidate('continuous_clock_reset')
            return False
        return super()._backward_reset()

    def __call__(self, session, presentation, seconds):
        if not self.valid or getattr(session, 'id', None) != self.session_id:
            return None
        self.pending_reason = None
        if not math.isfinite(seconds):
            self.invalidate('continuous_clock_invalid')
            return None
        try:
            state = session.continuous_audio_state()
            started, ceiling, terminal = state.started_from_zero, state.next_sample, state.terminal
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self.invalidate('continuous_audio_unavailable')
            return None
        if type(started) is not bool or type(terminal) is not bool or type(ceiling) is not int:
            self.invalidate('continuous_audio_invalid')
            return None
        if terminal:
            self.invalidate('continuous_audio_terminal')
            return None
        if ceiling < self.last_next_sample:
            self.invalidate('continuous_media_rewound')
            return None
        if not started and ceiling == 0:
            self.pending_reason = 'continuous_audio_pending'
            return None
        if not started or ceiling <= 0:
            self.invalidate('continuous_audio_invalid')
            return None
        if seconds > ceiling / 48000 + self.DELIVERED_TOLERANCE:
            self.invalidate('continuous_clock_ahead_of_media')
            return None
        self.last_next_sample = ceiling
        return self._clock_position(seconds)


class KodiAdPlayback:
    """Map Kodi time only for opted-in loopback fixture sessions."""
    def __init__(self, manager, player, client=None, fixture_origin=None, is_caching=None):
        self.manager, self.player = manager, player
        self.client = client or getattr(manager, 'client', None)
        self.fixture_origin = fixture_origin
        self.is_caching = is_caching or (lambda: False)
        self.mapper = None
        self.mapping_valid = False
        self.session_id = None
        self.session = None
        self.tracker = None
        self.worker = None
        self.paused = False
        self.seeking = False
        self.speed = 1.0
        self.speed_changed = False
        self.last_sample = None
        self._clock_ready = False
        self._startup_samples = []
        self._continuous_playback_url = None
        self._continuous_qualified = False
        self._av_started = False
        self._observation_revision = 0

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

    def _continuous_route_matches(self, expected):
        try:
            return (isinstance(expected, str) and bool(expected) and
                    getattr(self.session, 'continuous_playback_url', None) == expected and
                    self.player.getPlayingFile() == expected)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def bind(self, session_id):
        if session_id == self.session_id:
            return
        self.close()
        session = self.manager.get(session_id) if hasattr(self.manager, 'get') else None
        if session is None:
            return
        self.session_id, self.session = session_id, session
        self.paused = self.seeking = False
        self.speed = 1.0
        self.speed_changed = False
        self.mapping_valid = False
        self.mapper = None
        self._clock_ready = False
        self._startup_samples = []
        if self._is_loopback_fixture(self.fixture_origin) and getattr(session, 'ad_enabled', False):
            self.worker = BeaconWorker(self.client, session.cancel, self.fixture_origin)
            # Startup confirmation proves a running clock, but the withheld
            # sample at zero cannot prove entry into a preroll.
            self.tracker = AdTracker(session_id, self._emit, self.worker.enqueue,
                                     allow_preroll_start=False)
            # The sample-zero mapping is deliberately limited to explicit
            # loopback fixtures; normal Radio Browser sessions stay unmapped.
            continuous_url = getattr(session, 'continuous_playback_url', None)
            if self._continuous_route_matches(continuous_url):
                self._continuous_playback_url = continuous_url
                self._continuous_qualified = getattr(session, 'fixture_clock_qualified', False) is True
                self.enable_mapping(KodiContinuousClockAnchor(session_id))
            else:
                self.enable_mapping(KodiSessionClockAnchor(session_id))

    def on_playback_started(self, session_id):
        if session_id != self.session_id:
            return
        self._observation_revision += 1
        if self._continuous_playback_url is not None and self._av_started:
            self._continuous_qualified = False
            self.mapper.invalidate('continuous_clock_restarted')

    def on_av_started(self, session_id):
        if session_id != self.session_id:
            return
        self._observation_revision += 1
        if self._av_started and self._continuous_playback_url is not None:
            self._continuous_qualified = False
            self.mapper.invalidate('continuous_clock_restarted')
        self._av_started = True
        self._clock_ready = False
        self._startup_samples = []

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
        revision = self._observation_revision
        clock = None
        try:
            route_before = (self._continuous_playback_url is None or
                            self._continuous_route_matches(self._continuous_playback_url))
            playing = bool(self.player.isPlaying())
            clock = float(self.player.getTime()) if playing else None
            caching = bool(self.is_caching())
            if self.session is not session:
                return
            route_matches = route_before and (self._continuous_playback_url is None or
                             self._continuous_route_matches(self._continuous_playback_url))
            if self.session is not session:
                return
            if not route_matches and hasattr(self.mapper, 'invalidate'):
                self.mapper.invalidate('continuous_route_changed')
            if self._continuous_playback_url is not None and (
                    revision != self._observation_revision or self.session is not session
                    or self.manager.get(session.id) is not session or session.cancel.is_set()
                    or not 0 <= time.monotonic() - now <= MAX_GAP):
                if hasattr(self.mapper, 'invalidate'):
                    self.mapper.invalidate('continuous_observation_changed')
                route_matches = False
            if (self._continuous_playback_url is not None and self._continuous_qualified
                    and self._av_started and not self.mapper.startup_pending and caching):
                # Recovery can change the relationship between rendered audio
                # and Kodi's clock, including while pause/resume re-arms the
                # progress gate. Only a new session can qualify again.
                self._continuous_qualified = False
                self.mapper.invalidate('continuous_clock_cache')
            position = (self.mapper(session, presentation, clock)
                        if route_matches and self.mapper and clock is not None else None)
            mapped = position is not None and math.isfinite(position) and position >= 0
        except (RuntimeError, ValueError, TypeError, OverflowError):
            if (self.session is session and self._continuous_qualified
                    and self.mapper is not None and not self.mapper.startup_pending):
                # An unavailable read can hide the very buffering transition
                # that revokes the clock contract. Recovery cannot prove it away.
                self._continuous_qualified = False
                self.mapper.invalidate('continuous_observation_unavailable')
                if self.tracker is not None:
                    self.tracker.interrupt()
            playing, position, mapped, caching = False, None, False, True
        if self.session is not session:
            return
        self.mapping_valid = mapped
        raw_sample = PlaybackSample(self.session_id, position if mapped else 0.0,
                                    now, playing, self.paused, self.seeking, mapped, caching,
                                    self.speed, self.speed_changed)
        tracker_samples = [raw_sample]
        tracking_enabled = (mapped and playing and not self.paused and
                            not self.seeking and not caching)
        if self.mapper is not None and not self._clock_ready:
            tracking_enabled = False
            # Kodi can report zero before a startup clock reset. Buffer only two
            # consecutive usable samples so that reset cannot emit early beacons.
            if (mapped and playing and not self.paused and not self.seeking and not caching
                    and self.speed == 1 and not self.speed_changed):
                if self._startup_samples:
                    first = self._startup_samples[0]
                    elapsed = now - first.monotonic_seconds
                    advance = raw_sample.position_seconds - first.position_seconds
                    if (0 < elapsed <= MAX_GAP and 0 < advance <= elapsed + START_TOLERANCE):
                        self._clock_ready = True
                        if hasattr(self.mapper, 'confirm_startup'):
                            self.mapper.confirm_startup()
                        tracking_enabled = True
                        # Confirm progress, but do not retroactively track from
                        # the first sample: a reset can occur between polls.
                        tracker_samples = [raw_sample]
                        self._startup_samples = []
                    elif advance == 0 and 0 <= elapsed <= MAX_GAP:
                        # Identical samples have not established a running clock.
                        pass
                    else:
                        self._startup_samples = [raw_sample]
                else:
                    self._startup_samples = [raw_sample]
            else:
                self._startup_samples = []
            if not self._clock_ready:
                tracker_samples = [PlaybackSample(self.session_id, 0.0, now, playing,
                                                  self.paused, self.seeking, False, caching,
                                                  self.speed, self.speed_changed)]
        if not tracking_enabled:
            speed_evidence = self._clock_ready and (self.speed != 1 or self.speed_changed)
            cache_evidence = (not self.paused and caching and
                              (self._clock_ready or self.speed != 1 or self.speed_changed))
            if mapped and playing and not self.seeking and (speed_evidence or cache_evidence):
                # Retain cache plateaus and speed transitions as ambiguity
                # evidence, including speed changes while paused. The raw
                # pause/cache/speed flags still prevent played progress.
                tracker_samples = [raw_sample]
            else:
                tracker_samples = [PlaybackSample(self.session_id, 0.0, now, playing,
                                                  self.paused, self.seeking, False, caching,
                                                  self.speed, self.speed_changed)]
        # Preserve the mapped position in speed evidence so the tracker can
        # taint placements crossed at an untrusted rate.
        tracking_enabled = tracking_enabled and self.speed == 1 and not self.speed_changed
        reason = getattr(self.mapper, 'reason', None)
        continuous_candidate = self._continuous_playback_url is not None
        qualified = (continuous_candidate and self._continuous_qualified and self._av_started
                     and getattr(self.mapper, 'valid', False))
        if continuous_candidate and not qualified:
            # Keep the candidate visible for captured-audio comparison while
            # giving the success tracker no mapped playback evidence.
            tracking_enabled = False
            tracker_samples = [PlaybackSample(self.session_id, 0.0, now, playing,
                                              self.paused, self.seeking, False, caching,
                                              self.speed, self.speed_changed)]
            reason = (reason or getattr(self.mapper, 'pending_reason', None) or
                      ('continuous_clock_unaccepted' if mapped else 'continuous_clock_unavailable'))
        if self.tracker is not None:
            self.manager.sink.emit(Event(
                'ad_clock_sample', self.session_id, session.station.uuid,
                session.source.protocol, 'kodi_player', 'poll',
                datetime.now(timezone.utc).isoformat(), now,
                {'raw_clock': clock, 'mapped_position': position if mapped else None,
                 'mapped': mapped, 'tracking_enabled': tracking_enabled,
                 'playing': playing, 'paused': self.paused,
                 'seeking': self.seeking, 'caching': caching,
                 **({'candidate_only': not qualified} if continuous_candidate else {}),
                 **({'reason': reason} if reason else {})}))
        self.last_sample = tracker_samples[-1]
        for sample in tracker_samples:
            self.tracker.observe(presentation, sample)
        self.seeking = False
        self.speed_changed = False

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

    def on_speed_changed(self, speed, session_id=None):
        if session_id is not None and session_id != self.session_id:
            return
        self._observation_revision += 1
        try:
            speed = float(speed)
        except (TypeError, ValueError, OverflowError):
            speed = None
        if speed != self.speed:
            self.speed_changed = True
            self._startup_samples = []
        self.speed = speed

    def on_seek(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self._observation_revision += 1
            self.seeking = True
            self.mapping_valid = False
            self._clock_ready = False
            self._startup_samples = []
            if hasattr(self.mapper, 'invalidate'):
                if self._continuous_playback_url is not None:
                    self.mapper.invalidate('continuous_clock_seek')
                else:
                    self.mapper.invalidate()

    def on_pause(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self._observation_revision += 1
            self.paused = True
            if self._continuous_playback_url is not None:
                self._clock_ready = False
                if self.tracker is not None:
                    self.tracker.interrupt()
            if not self._clock_ready:
                self._startup_samples = []

    def on_resume(self, session_id=None):
        if session_id is None or session_id == self.session_id:
            self._observation_revision += 1
            self.paused = False
            if self._continuous_playback_url is not None:
                # An owned resume is interruption evidence even if Python saw
                # the pause callback only after Kodi had already resumed.
                self._clock_ready = False
                self._startup_samples = []
                if self.tracker is not None:
                    self.tracker.interrupt()

    def on_end(self, reason, session_id=None):
        if session_id is not None and session_id != self.session_id:
            return
        self._flush_errors()
        if self.tracker is not None:
            self.tracker.finish(reason)
        self.close()

    def close(self):
        self._observation_revision += 1
        if self.tracker is not None:
            self.tracker.finish('cancelled')
        if self.worker is not None:
            self.worker.close()
        self.session_id = self.session = self.tracker = self.worker = None
        self.mapper = None
        self.mapping_valid = False
        self.last_sample = None
        self.paused = self.seeking = False
        self.speed = 1.0
        self.speed_changed = False
        self._clock_ready = False
        self._startup_samples = []
        self._continuous_playback_url = None
        self._continuous_qualified = False
        self._av_started = False
