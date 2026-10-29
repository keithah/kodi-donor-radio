# SPDX-License-Identifier: GPL-2.0-or-later
"""One cancellable playback session with opaque, bounded media resources."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from url_policy import origin

from cues import CueTracker
from http_client import HttpClient
from icy_markers import IcyMarkerTracker, adswizz_evidence
from icy_marker_store import normalize_marker
from models import Event
from station_resolver import resolve_station


# Each launch phase has its own finite deadline. Setup includes the 30-second
# create request budget plus preparation margin; publication then grants a fresh
# qualification window for the bounded composition preflight.
_FIXTURE_LAUNCH_QUEUE_TTL = 30.0
_FIXTURE_LAUNCH_CREATE_TTL = 40.0
_FIXTURE_LAUNCH_QUALIFY_TTL = 30.0
_LAUNCH_NONCE = re.compile(r'^[a-f0-9]{48}$')


@dataclass
class _FixtureLaunch:
    fixture_id: str
    origin: tuple
    deadline: float
    cancel: object = None
    session_id: object = None


def _icy_diagnostic_evidence(metadata):
    """Keep useful ICY fields but discard opaque Adswizz tracking context and IDs."""
    if not isinstance(metadata, dict):
        return {}
    evidence = {key: value for key, value in metadata.items()
                if isinstance(key, str) and key.lower() in ('streamtitle', 'streamurl')}
    evidence.update(adswizz_evidence(metadata))
    return evidence


class ResourceLimit(ValueError):
    pass


class SafeSink:
    """Observation failures must never interrupt audio or expose exception text."""
    def __init__(self, sink=None):
        self.sink = sink

    def emit(self, event):
        if self.sink is not None:
            try:
                self.sink.emit(event)
            except Exception:
                pass


@dataclass
class Resource:
    id: str
    url: str
    kind: str = 'media'
    active: int = 0
    playlist_lock: object = field(default_factory=threading.Lock)
    first_sequence: object = None
    epoch: int = 0


@dataclass(frozen=True)
class ContinuousAudioState:
    """Transport bounds only; delivered samples do not establish playback."""
    started_from_zero: bool = False
    next_sample: int = 0
    terminal: bool = False


class PlaybackSession:
    def __init__(self, station, source, cancel, base_url, sink, resource_limit,
                 ad_config=None, ad_client=None, cache_root=None, learned_lookup=None):
        if resource_limit < 1:
            raise ValueError('Resource limit must be positive')
        self.id = secrets.token_hex(24)
        self.station, self.source, self.cancel = station, source, cancel
        self.sink = sink
        self.resource_limit = resource_limit
        self._lock = threading.RLock()
        self._resources = OrderedDict()
        root = Resource(secrets.token_hex(24), source.final_url, source.protocol)
        self._root = root.id
        self._resources[root.id] = root
        self.playback_url = base_url + self.route(root.id)
        self.adaptive_playback_url = None
        self.continuous_playback_url = None
        self.continuous_route = None
        self._continuous_audio_state = ContinuousAudioState()
        self._continuous_sequence = 0
        self._fixture_launch_nonce = None
        self._fixture_clock_qualified = False
        self.last_used = time.monotonic()
        self._claimed = False
        self.is_preview = False
        self.tracker = CueTracker(self.id, station.uuid, sink,
                                  lambda: datetime.now(timezone.utc), time.monotonic)
        self.icy_tracker = IcyMarkerTracker(self.id, station.uuid, self.sink.emit,
                                            learned_lookup or (lambda marker: None))
        self.ad = None
        self.ad_enabled = False
        self._ad_errors = []
        self._cache_routes = {}
        if (ad_config is not None and ad_config.enabled and source.protocol == 'hls'
                and ad_config.fixture_origin and ad_config.vast_url
                and origin(source.final_url) in getattr(
                    getattr(ad_client, 'policy', None), 'test_origins', frozenset())
                and origin(ad_config.fixture_origin) in getattr(
                    getattr(ad_client, 'policy', None), 'test_origins', frozenset())
                and ad_config.fixture_origin.startswith('http://127.0.0.1:')
                and (ad_config.preroll or ad_config.midroll)):
            self.ad_enabled = True
            adaptive = station.uuid.startswith('fixture-') and resource_limit >= 3
            from ad_session import AdSession
            try:
                self.ad = AdSession(ad_config, ad_client, cache_root or Path.cwd(), cancel,
                                    resource_limit - int(adaptive), source.final_url,
                                    self._record_ad_error)
                self.ad.active_ids = self.active_cached_ids
                if ad_config.preroll and not ad_config.midroll and not self.ad.preroll_available:
                    self.ad.close()
                    self.ad = None
            except Exception as error:
                # No composed manifest has been published. Keep station playback.
                from ad_models import AdError
                if isinstance(error, AdError):
                    self._record_ad_error(error)
                self.ad = None
            if adaptive and self.ad is not None:
                # The master consumes one bounded, pinned slot. Its GET validates
                # the composition before declaring a codec; native playback keeps
                # its original root URL and unsupported-source fallback.
                master = Resource(secrets.token_hex(24), 'composition:///audio.m3u8',
                                  'audio_master')
                self._resources[master.id] = master
                self.adaptive_playback_url = base_url + self.route(master.id)
            if station.uuid.startswith('fixture-') and self.ad is not None:
                # An alternate representation of the pinned root consumes no
                # extra resource slot. Ordinary radio playback never offers it.
                self.continuous_route = '/media/%s/%s.aac' % (self.id, root.id)
                self.continuous_playback_url = base_url + self.continuous_route

    def observe_icy_metadata(self, metadata, observed_at, monotonic_at):
        with self._lock:
            self.sink.emit(Event('metadata_observed', self.id, self.station.uuid,
                                 'icy', 'icy', _icy_diagnostic_evidence(metadata),
                                 observed_at, monotonic_at))
            self.icy_tracker.observe(metadata, observed_at, monotonic_at)

    def latest_icy_candidate(self):
        with self._lock:
            candidate = self.icy_tracker.latest_candidate()
            if candidate is None or len(candidate) > 256:
                return None
            return candidate

    def label_icy_marker(self, marker, action, observed_at, monotonic_at):
        with self._lock:
            return self.icy_tracker.label(marker, action, observed_at, monotonic_at)

    def expire_icy_markers(self, monotonic_at):
        with self._lock:
            self.icy_tracker.expire(monotonic_at)

    def _record_ad_error(self, error):
        with self._lock:
            if not self.cancel.is_set():
                self._ad_errors.append(error)

    def take_ad_errors(self):
        with self._lock:
            errors, self._ad_errors = self._ad_errors, []
            return errors

    def presentation(self):
        ad = self.ad
        return ad.presentation() if ad is not None else None

    def active_cached_ids(self):
        with self._lock:
            return {resource.cache_id for resource in self._cache_routes.values()
                    if resource.active}

    def cached_route(self, cache_id):
        ad = self.ad
        retained = ad.published_ids if ad is not None else None
        with self._lock:
            resource = self._cache_routes.get(cache_id)
            if resource is None:
                if len(self._resources) >= self.resource_limit:
                    if retained is not None:
                        for old_id, old in list(self._cache_routes.items()):
                            if old_id not in retained and not old.active:
                                self._resources.pop(old.id, None)
                                del self._cache_routes[old_id]
                if len(self._resources) >= self.resource_limit:
                    raise ResourceLimit('Session resource limit')
                resource = Resource(secrets.token_hex(24), 'cache:///%s.ts' % cache_id, 'cached')
                resource.cache_id = cache_id
                self._resources[resource.id] = resource
                self._cache_routes[cache_id] = resource
            return self.route(resource.id)

    def retire_cached(self):
        ad = self.ad
        if ad is None:
            return
        retained = ad.published_ids
        with self._lock:
            for cache_id, resource in list(self._cache_routes.items()):
                if cache_id not in retained and not resource.active:
                    self._resources.pop(resource.id, None)
                    del self._cache_routes[cache_id]

    def route(self, resource_id):
        resource = self._resources[resource_id]
        suffix = ''
        if resource.kind in ('hls', 'playlist', 'audio_master'):
            suffix = '.m3u8'
        elif resource.kind != 'continuous':
            filename = urlsplit(resource.url).path.rsplit('/', 1)[-1]
            extension = filename.rsplit('.', 1)[-1].lower()
            if '.' in filename and extension in ('ts', 'aac', 'mp3', 'mp4', 'm4s', 'm4a', 'ac3', 'ec3'):
                suffix = '.' + extension
        return '/media/%s/%s%s' % (self.id, resource_id, suffix)

    def _id(self, route):
        return route.rsplit('/', 1)[-1].split('.', 1)[0]

    @property
    def resource_count(self):
        with self._lock:
            return len(self._resources)

    @property
    def active(self):
        with self._lock:
            return any(resource.active for resource in self._resources.values())

    def lookup(self, route):
        with self._lock:
            return self._resources.get(self._id(route))

    def register_many(self, urls, kinds=None):
        """Atomically register one manifest; never evict active or same-batch URLs."""
        urls = list(urls)
        kinds = list(kinds) if kinds is not None else ['media'] * len(urls)
        with self._lock:
            if self.cancel.is_set():
                raise ResourceLimit('Session stopped')
            by_url = {resource.url: resource for resource in self._resources.values()}
            requested = set(urls)
            missing = requested.difference(by_url)
            excess = len(self._resources) + len(missing) - self.resource_limit
            removable = [key for key, resource in self._resources.items()
                         if key != self._root and not resource.active and resource.url not in requested
                         and resource.kind not in ('cached', 'audio_master')]
            if excess > len(removable):
                raise ResourceLimit('Session resource limit')
            for key in removable[:max(0, excess)]:
                del self._resources[key]
            routes = []
            for url, kind in zip(urls, kinds):
                resource = by_url.get(url)
                if resource is None:
                    resource = Resource(secrets.token_hex(24), url, kind)
                    self._resources[resource.id] = resource
                    by_url[url] = resource
                elif kind == 'playlist':
                    resource.kind = kind
                self._resources.move_to_end(resource.id)
                routes.append(self.route(resource.id))
            return routes

    @contextmanager
    def acquire(self, route):
        with self._lock:
            resource = self._resources.get(self._id(route))
            if resource is None or self.cancel.is_set():
                raise KeyError('Unknown media resource')
            resource.active += 1
            self._resources.move_to_end(resource.id)
            self.last_used = time.monotonic()
        try:
            yield resource
        finally:
            with self._lock:
                resource.active -= 1
                self.last_used = time.monotonic()

    def claim_continuous(self):
        with self._lock:
            if self._claimed or self.cancel.is_set():
                return False
            self._claimed = True
            return True

    def continuous_audio_state(self):
        with self._lock:
            state = self._continuous_audio_state
            if self.cancel.is_set() and not state.terminal:
                return ContinuousAudioState(state.started_from_zero, state.next_sample, True)
            return state

    @property
    def fixture_clock_qualified(self):
        """An explicit fixture launch contract, never an active-core attestation."""
        with self._lock:
            ad = self.ad
            return (self._fixture_clock_qualified and not self.cancel.is_set()
                    and ad is not None and not ad.terminal)

    @property
    def fixture_launch_nonce(self):
        with self._lock:
            return self._fixture_launch_nonce

    def matches_fixture_launch(self, nonce):
        with self._lock:
            return self._fixture_launch_nonce == nonce

    def bind_fixture_launch(self, nonce):
        with self._lock:
            if self._fixture_launch_nonce is not None:
                return False
            self._fixture_launch_nonce = nonce
            return True

    def mark_fixture_clock_qualified(self, nonce):
        """Atomically qualify only this live session's explicit fixture launch."""
        with self._lock:
            ad = self.ad
            if (self._fixture_launch_nonce != nonce or self.cancel.is_set()
                    or not self.ad_enabled or self.continuous_playback_url is None
                    or ad is None or ad.terminal):
                return False
            self._fixture_clock_qualified = True
            return True

    def record_continuous_delivery(self, segment):
        """Advance the exclusive ceiling only after a complete socket write."""
        with self._lock:
            state = self._continuous_audio_state
            if self.cancel.is_set() or state.terminal:
                return False
            if (not self._claimed or segment.sequence != self._continuous_sequence
                    or segment.start_sample != state.next_sample or segment.samples <= 0):
                raise ValueError('Nonconsecutive continuous delivery')
            self._continuous_sequence += 1
            self._continuous_audio_state = ContinuousAudioState(
                True, segment.start_sample + segment.samples, False)
            return True

    def finish_continuous_audio(self):
        with self._lock:
            state = self._continuous_audio_state
            self._continuous_audio_state = ContinuousAudioState(
                state.started_from_zero, state.next_sample, True)

    def close(self, reason='session_stopped'):
        self.cancel.set()
        if self.source.response is not None:
            self.source.response.close()
        ad = self.ad
        if ad is not None:
            ad.close()
        self.tracker.finish(reason)


class SessionManager:
    def __init__(self, client=None, sink=None, resolver=resolve_station,
                 resource_limit=2048, idle_timeout=120, ad_config=None, cache_root=None,
                 icy_marker_store=None):
        self.client = client or HttpClient()
        self.sink = SafeSink(sink)
        self.resolver, self.resource_limit, self.idle_timeout = resolver, resource_limit, idle_timeout
        self.ad_config, self.cache_root = ad_config, cache_root
        self.icy_marker_store = icy_marker_store
        self.base_url = ''
        self._lock, self._create_lock = threading.RLock(), threading.Lock()
        self._session, self._pending, self._closed = None, None, False
        self._fixture_launches = {}

    def _expire_fixture_launches(self):
        now = time.monotonic()
        for nonce, launch in tuple(self._fixture_launches.items()):
            if launch.deadline <= now:
                del self._fixture_launches[nonce]
                if launch.cancel is not None and launch.session_id is None:
                    launch.cancel.set()

    def _discard_fixture_launches(self):
        for launch in self._fixture_launches.values():
            if launch.cancel is not None and launch.session_id is None:
                launch.cancel.set()
        self._fixture_launches.clear()

    def reserve_fixture_launch(self, fixture_id):
        with self._lock:
            self._expire_fixture_launches()
            config = self.ad_config
            if (self._closed or not isinstance(fixture_id, str)
                    or re.fullmatch(r'fixture-[0-9]{5}', fixture_id) is None
                    or config is None or not config.enabled
                    or not (config.preroll or config.midroll)
                    or re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', config.fixture_origin) is None
                    or not 1 <= urlsplit(config.fixture_origin).port <= 65535):
                raise ValueError('Fixture launch unavailable')
            # One outstanding launch is also the latest explicit selection.
            # A queued resolver from an older click cannot reclaim playback.
            self._discard_fixture_launches()
            nonce = secrets.token_hex(24)
            self._fixture_launches[nonce] = _FixtureLaunch(
                fixture_id, origin(config.fixture_origin), time.monotonic() + _FIXTURE_LAUNCH_QUEUE_TTL)
            return nonce

    def qualify_fixture_clock(self, session_id, nonce):
        """Accept the resolver's exact selected transport for its spent launch ticket.

        The trusted launcher requests VideoPlayer explicitly. This control record
        cannot report which internal core is active; the player adapter still
        requires owned AV startup, exact URL checks and valid clock observations.
        """
        with self._lock:
            self._expire_fixture_launches()
            launch = self._fixture_launches.get(nonce)
            session = self._session
            ad = session.ad if session is not None else None
            if (launch is None or session is None or launch.session_id != session_id
                    or session.id != session_id
                    or launch.cancel is not session.cancel or session.cancel.is_set()
                    or session.station.uuid != launch.fixture_id or not session.ad_enabled
                    or session.continuous_playback_url is None or ad is None
                    or ad.terminal or origin(session.source.final_url) != launch.origin
                    or not session.mark_fixture_clock_qualified(nonce)):
                return False
            del self._fixture_launches[nonce]
            return True

    def cancel_fixture_launch(self, nonce):
        with self._lock:
            launch = self._fixture_launches.pop(nonce, None)
            if launch is not None and launch.cancel is not None:
                launch.cancel.set()
            session = self._session
            if session is None or not session.matches_fixture_launch(nonce):
                return
            self._session = None
        session.close('fixture_launch_cancelled')

    def create(self, station, fixture_launch_nonce=None):
        # Register selection order before queuing behind an older resolver.
        # The event is also an ownership token: stale waiters cannot replace it.
        with self._lock:
            if self._closed:
                raise RuntimeError('Manager closed')
            launch = None
            if fixture_launch_nonce is not None:
                self._expire_fixture_launches()
                if not isinstance(fixture_launch_nonce, str) or not _LAUNCH_NONCE.fullmatch(fixture_launch_nonce):
                    raise ValueError('Invalid fixture launch')
                launch = self._fixture_launches.get(fixture_launch_nonce)
                if launch is None or launch.fixture_id != station.uuid or launch.cancel is not None:
                    raise ValueError('Invalid fixture launch')
            else:
                self._discard_fixture_launches()
            if self._pending is not None:
                self._pending.set()
            cancel = self._pending = threading.Event()
            if launch is not None:
                launch.cancel = cancel
                launch.deadline = time.monotonic() + _FIXTURE_LAUNCH_CREATE_TTL
            ad_config = self.ad_config
        with self._create_lock:
            source = None
            try:
                with self._lock:
                    self._expire_fixture_launches()
                    if self._closed or self._pending is not cancel or cancel.is_set():
                        raise RuntimeError('Station selection cancelled')
                    old, self._session = self._session, None
                if old is not None:
                    old.close('station_changed')
                source = self.resolver(station.url, self.client, cancel)
                # PlaybackSession may synchronously resolve and cache a preroll.
                # Keep that bounded network work outside the manager lock so stop,
                # expiry, lookup, and shutdown can service other requests.
                session = PlaybackSession(station, source, cancel, self.base_url,
                                          self.sink, self.resource_limit,
                                          ad_config, self.client, self.cache_root,
                                          lambda marker: self.icy_marker_store.lookup(station.uuid, marker)
                                          if self.icy_marker_store is not None else None)
                session.is_preview = False
                if fixture_launch_nonce is not None and not session.bind_fixture_launch(fixture_launch_nonce):
                    session.close('fixture_launch_binding_failed')
                    source = None
                    raise RuntimeError('Fixture launch binding failed')
                with self._lock:
                    self._expire_fixture_launches()
                    if self._closed or self._pending is not cancel or cancel.is_set():
                        session.close('station_changed')
                        source = None  # The unpublished session closed its source.
                        raise RuntimeError('Station selection cancelled')
                    self._session = session
                    if launch is not None:
                        launch.session_id = session.id
                        launch.deadline = time.monotonic() + _FIXTURE_LAUNCH_QUALIFY_TTL
                    return session
            except Exception:
                cancel.set()
                if launch is not None:
                    with self._lock:
                        self._fixture_launches.pop(fixture_launch_nonce, None)
                if source is not None and source.response is not None:
                    source.response.close()
                raise
            finally:
                with self._lock:
                    if self._pending is cancel:
                        self._pending = None

    def create_preview(self, station):
        """Create a preview session only while no deliberate or pending choice exists.

        This check and creation share the same serialization lock as deliberate
        creates. Preview preparation therefore cannot evict the station currently
        playing or cancel a deliberate selection that raced with it.
        """
        with self._create_lock:
            with self._lock:
                current = self._session
                if (self._closed or self._pending is not None or
                        (current is not None and not current.is_preview)):
                    raise RuntimeError('Preview unavailable while a station is selected')
                self._session = None
                cancel = self._pending = threading.Event()
                ad_config = self.ad_config
            source = None
            try:
                if current is not None:
                    current.close('station_changed')
                if cancel.is_set():
                    raise RuntimeError('Preview cancelled')
                source = self.resolver(station.url, self.client, cancel)
                session = PlaybackSession(station, source, cancel, self.base_url,
                                          self.sink, self.resource_limit, ad_config,
                                          self.client, self.cache_root,
                                          lambda marker: self.icy_marker_store.lookup(station.uuid, marker)
                                          if self.icy_marker_store is not None else None)
                session.is_preview = True
                with self._lock:
                    if self._closed or self._pending is not cancel or cancel.is_set() or self._session is not None:
                        session.close('station_changed')
                        source = None
                        raise RuntimeError('Preview cancelled')
                    self._session = session
                    return session
            except Exception:
                cancel.set()
                if source is not None and source.response is not None:
                    source.response.close()
                raise
            finally:
                with self._lock:
                    if self._pending is cancel:
                        self._pending = None

    def current_icy_candidate(self):
        self.expire()
        with self._lock:
            session = self._session
            if session is None or session.cancel.is_set():
                return None
            marker = session.latest_icy_candidate()
            try:
                normalize_marker(marker)
            except (ValueError, UnicodeError):
                return None
            return {'session_id': session.id, 'station_uuid': session.station.uuid,
                    'marker': marker}

    def learn_icy_marker(self, session_id, marker, action):
        return self.label_icy_marker(session_id, marker, action, True)

    def label_icy_marker(self, session_id, marker, action, remember):
        normalize_marker(marker)
        if action not in ('start', 'end') or type(remember) is not bool:
            raise ValueError('Invalid marker label')
        self.expire()
        with self._lock:
            session = self._session
            if session is None or session.id != session_id or session.cancel.is_set():
                return False
            with session._lock:
                monotonic_at = time.monotonic()
                observed_at = datetime.now(timezone.utc).isoformat()
                session.expire_icy_markers(monotonic_at)
                if marker != session.latest_icy_candidate():
                    return False
        if remember:
            if self.icy_marker_store is None:
                raise ValueError('Marker store unavailable')
            # Persist without relay/session locks so slow profile I/O cannot
            # interrupt the continuous audio path or block other requests.
            self.icy_marker_store.learn(session.station.uuid, marker, action)
        with self._lock:
            if (self._session is not session or session.id != session_id or
                    session.cancel.is_set()):
                # A remembered station rule is durable even if playback ended
                # while its profile write was in progress. Report that durable
                # success to the caller; transient labels still require the
                # original session to remain active.
                return remember
            with session._lock:
                session.expire_icy_markers(monotonic_at)
                if marker != session.latest_icy_candidate():
                    return remember
                # Keep the original acceptance time: disk latency must not
                # expire a marker that was current when the user accepted it.
                return session.label_icy_marker(marker, action, observed_at, monotonic_at)

    def get(self, session_id):
        self.expire()
        with self._lock:
            session = self._session
            return session if session is not None and session.id == session_id and not session.cancel.is_set() else None

    def stop(self, session_id):
        with self._lock:
            session = self._session
            if session is None or session.id != session_id:
                return
            self._session = None
            launch_nonce = session.fixture_launch_nonce
            if launch_nonce is not None:
                self._fixture_launches.pop(launch_nonce, None)
        session.close()

    def expire(self):
        with self._lock:
            self._expire_fixture_launches()
            session = self._session
            if session is None:
                return
            monotonic_at = time.monotonic()
            session.expire_icy_markers(monotonic_at)
            if session.active or monotonic_at - session.last_used < self.idle_timeout:
                return
            self._session = None
        session.close('idle_timeout')

    def close(self):
        with self._lock:
            self._closed = True
            self._fixture_launches.clear()
            if self._pending is not None:
                self._pending.set()
            session, self._session = self._session, None
        if session is not None:
            session.close('service_shutdown')
