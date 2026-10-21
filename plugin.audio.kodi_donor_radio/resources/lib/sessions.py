# SPDX-License-Identifier: GPL-2.0-or-later
"""One cancellable playback session with opaque, bounded media resources."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import threading
import time
from urllib.parse import urlsplit

from cues import CueTracker
from http_client import HttpClient
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
    def __init__(self, station, source, cancel, base_url, sink, resource_limit):
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
        self.tracker.finish(reason)


class SessionManager:
    def __init__(self, client=None, sink=None, resolver=resolve_station,
                 resource_limit=2048, idle_timeout=120):
        self.client = client or HttpClient()
        self.sink = SafeSink(sink)
        self.resolver, self.resource_limit, self.idle_timeout = resolver, resource_limit, idle_timeout
        self.base_url = ''
        self._lock, self._create_lock = threading.RLock(), threading.Lock()
        self._session, self._pending, self._closed = None, None, False

    def create(self, station):
        with self._create_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError('Manager closed')
                old, self._session = self._session, None
                cancel = self._pending = threading.Event()
            if old is not None:
                old.close('station_changed')
            source = None
            try:
                source = self.resolver(station.url, self.client, cancel)
                with self._lock:
                    if self._closed or cancel.is_set():
                        raise RuntimeError('Manager closed')
                    session = PlaybackSession(station, source, cancel, self.base_url,
                                              self.sink, self.resource_limit)
                    self._session = session
                    return session
            except Exception:
                cancel.set()
                if source is not None and source.response is not None:
                    source.response.close()
                raise
            finally:
                with self._lock:
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
            if session is None or session.active or time.monotonic() - session.last_used < self.idle_timeout:
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
