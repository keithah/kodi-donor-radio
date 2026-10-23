# SPDX-License-Identifier: GPL-2.0-or-later
"""One cancellable playback session with opaque, bounded media resources."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from url_policy import origin

from cues import CueTracker
from http_client import HttpClient
from icy_markers import IcyMarkerTracker
from models import Event
from station_resolver import resolve_station


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


class PlaybackSession:
    def __init__(self, station, source, cancel, base_url, sink, resource_limit,
                 ad_config=None, ad_client=None, cache_root=None):
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
        self.last_used = time.monotonic()
        self._claimed = False
        self.tracker = CueTracker(self.id, station.uuid, sink,
                                  lambda: datetime.now(timezone.utc), time.monotonic)
        self.icy_tracker = IcyMarkerTracker(self.id, station.uuid, self.sink.emit,
                                            lambda marker: None)
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
            from ad_session import AdSession
            try:
                self.ad = AdSession(ad_config, ad_client, cache_root or Path.cwd(), cancel,
                                    resource_limit, source.final_url, self._record_ad_error)
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

    def observe_icy_metadata(self, metadata, observed_at, monotonic_at):
        with self._lock:
            self.sink.emit(Event('metadata_observed', self.id, self.station.uuid,
                                 'icy', 'icy', metadata, observed_at, monotonic_at))
            self.icy_tracker.observe(metadata, observed_at, monotonic_at)

    def latest_icy_candidate(self):
        with self._lock:
            candidate = self.icy_tracker.latest_candidate()
            return candidate[:256] if candidate is not None else None

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
        return self.ad.presentation() if self.ad is not None else None

    def active_cached_ids(self):
        with self._lock:
            return {resource.cache_id for resource in self._cache_routes.values()
                    if resource.active}

    def cached_route(self, cache_id):
        with self._lock:
            resource = self._cache_routes.get(cache_id)
            if resource is None:
                if len(self._resources) >= self.resource_limit:
                    self.retire_cached()
                if len(self._resources) >= self.resource_limit:
                    raise ResourceLimit('Session resource limit')
                resource = Resource(secrets.token_hex(24), 'cache:///%s.ts' % cache_id, 'cached')
                resource.cache_id = cache_id
                self._resources[resource.id] = resource
                self._cache_routes[cache_id] = resource
            return self.route(resource.id)

    def retire_cached(self):
        if self.ad is None:
            return
        retained = self.ad.published_ids
        with self._lock:
            for cache_id, resource in list(self._cache_routes.items()):
                if cache_id not in retained and not resource.active:
                    self._resources.pop(resource.id, None)
                    del self._cache_routes[cache_id]

    def route(self, resource_id):
        resource = self._resources[resource_id]
        suffix = ''
        if resource.kind in ('hls', 'playlist'):
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
                         if key != self._root and not resource.active and resource.url not in requested]
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

    def close(self, reason='session_stopped'):
        self.cancel.set()
        if self.source.response is not None:
            self.source.response.close()
        if self.ad is not None:
            self.ad.close()
        self.tracker.finish(reason)


class SessionManager:
    def __init__(self, client=None, sink=None, resolver=resolve_station,
                 resource_limit=2048, idle_timeout=120, ad_config=None, cache_root=None):
        self.client = client or HttpClient()
        self.sink = SafeSink(sink)
        self.resolver, self.resource_limit, self.idle_timeout = resolver, resource_limit, idle_timeout
        self.ad_config, self.cache_root = ad_config, cache_root
        self.base_url = ''
        self._lock, self._create_lock = threading.RLock(), threading.Lock()
        self._session, self._pending, self._closed = None, None, False

    def create(self, station):
        # Register selection order before queuing behind an older resolver.
        # The event is also an ownership token: stale waiters cannot replace it.
        with self._lock:
            if self._closed:
                raise RuntimeError('Manager closed')
            if self._pending is not None:
                self._pending.set()
            cancel = self._pending = threading.Event()
            ad_config = self.ad_config
        with self._create_lock:
            source = None
            try:
                with self._lock:
                    if self._closed or self._pending is not cancel or cancel.is_set():
                        raise RuntimeError('Station selection cancelled')
                    old, self._session = self._session, None
                if old is not None:
                    old.close('station_changed')
                source = self.resolver(station.url, self.client, cancel)
                with self._lock:
                    if self._closed or self._pending is not cancel or cancel.is_set():
                        raise RuntimeError('Station selection cancelled')
                    session = PlaybackSession(station, source, cancel, self.base_url,
                                              self.sink, self.resource_limit,
                                              ad_config, self.client, self.cache_root)
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
        session.close()

    def expire(self):
        with self._lock:
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
            if self._pending is not None:
                self._pending.set()
            session, self._session = self._session, None
        if session is not None:
            session.close('service_shutdown')
