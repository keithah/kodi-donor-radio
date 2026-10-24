# SPDX-License-Identifier: GPL-2.0-or-later
"""Session-local controlled HLS composition and immutable media retention."""
import math
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import replace

from ad_media import CreativeCache, inspect_ts
from ad_timeline import AdTimeline, TimelineEpochLimit
from http_client import Cancelled, read_bounded
from url_policy import origin
from vast import VastResolver

SOURCE_BYTES = 32 * 1024 * 1024
SOURCE_SEGMENTS = 192
SOURCE_FETCH_SECONDS = 15


class CompositionFailure(ValueError):
    pass


class SourceUnavailable(CompositionFailure):
    """A temporary source fetch failure that can succeed on the next reload."""
    pass


class AdSession:
    def __init__(self, config, client, cache_root, cancel, resource_limit, source_origin,
                 on_error=None):
        self.config, self.client, self.cancel = config, client, cancel
        self.source_origin = origin(source_origin)
        self.on_error = on_error
        self.creative = CreativeCache(cache_root, client, config.fixture_origin)
        self.resolver = VastResolver(client, config.fixture_origin)
        self.timeline = None
        self._lock = threading.RLock()
        self._render_lock = threading.Lock()
        self._source = {}  # URL -> (CachedSegment, immutable bytes)
        self._source_by_id = {}
        self._published = set()
        self._published_order = OrderedDict()
        self._prepared = []
        self._initial = True
        self._committed = False
        self._terminal = False
        self._closed = False
        self.active_ids = lambda: set()
        # Reserve one cache window for new source data and two for recent output.
        self._limit = min(SOURCE_SEGMENTS // 3, max(1, resource_limit - 1))
        self._midroll = None
        self._midroll_placement_id = None
        self._midroll_done = threading.Event()
        self._worker = None
        pre = self._prepare() if config.preroll else None
        self.preroll_available = pre is not None
        self.timeline = AdTimeline(pre, self._limit)
        if pre is not None:
            self._prepared.append(pre)
        if config.midroll:
            self._worker = threading.Thread(target=self._prepare_midroll, daemon=True)
            self._worker.start()

    def _prepare(self):
        try:
            ad = self.resolver.resolve(self.config.vast_url, self.cancel)
            return self.creative.prepare(ad, self.cancel) if ad is not None else None
        except Exception as error:
            from ad_models import AdError
            if isinstance(error, AdError) and self.on_error is not None and not self.cancel.is_set():
                self.on_error(error)
            return None

    def _prepare_midroll(self):
        try:
            offer = self._prepare()
            with self._lock:
                if offer is not None:
                    if self._closed:
                        for item in offer.segments:
                            try:
                                self.creative.unpin(item.id)
                            except KeyError:
                                # close() may already have cleared the cache.
                                pass
                    else:
                        self._midroll = offer
                        self._prepared.append(offer)
                        self.timeline.offer(offer)
        finally:
            self._midroll_done.set()

    @property
    def committed(self):
        with self._lock:
            return self._committed

    @property
    def terminal(self):
        with self._lock:
            return self._terminal

    @property
    def published_ids(self):
        with self._lock:
            return frozenset(self._published)

    def terminate(self):
        with self._lock:
            self._terminal = True

    def presentation(self):
        return self.timeline.snapshot()

    def _make_source_room(self, required_bytes, needed):
        """Retire oldest inactive published source routes before the cache fills.

        The caller holds ``_lock``. Routes in the incoming playlist and active
        HTTP reads stay pinned; older published routes may expire under pressure.
        """
        active = self.active_ids()
        while (len(self._source) >= SOURCE_SEGMENTS or
               sum(len(value[1]) for value in self._source.values()) + required_bytes > SOURCE_BYTES):
            victim = None
            for resource_id in self._published_order:
                if resource_id in active:
                    continue
                for url, (item, _data) in self._source.items():
                    if item.id == resource_id and url not in needed:
                        victim = resource_id, url
                        break
                if victim is not None:
                    break
            if victim is None:
                raise CompositionFailure('source_cache_full')
            resource_id, url = victim
            self._published_order.pop(resource_id, None)
            self._published.discard(resource_id)
            item, _data = self._source.pop(url)
            self._source_by_id.pop(item.id, None)

    def _cache_window(self, playlist):
        from ad_models import CachedSegment
        from pathlib import Path
        forbidden = ('#EXT-X-MAP', '#EXT-X-BYTERANGE', '#EXT-X-KEY',
                     '#EXT-X-STREAM-INF', '#EXT-X-I-FRAMES-ONLY')
        if (playlist.is_master or not playlist.segments or
                any(line.startswith(forbidden) for line in playlist.lines) or
                len(playlist.segments) > self._limit):
            raise CompositionFailure('unsupported_source_playlist')
        needed = {segment.uri for segment in playlist.segments}
        with self._lock:
            if self._closed or self._terminal:
                raise CompositionFailure('session_closed')
            active = self.active_ids()
            # Current published resources stay pinned. Old, unpublished entries can go.
            for url, (item, data) in list(self._source.items()):
                if url not in needed and item.id not in self._published and item.id not in active:
                    del self._source[url]
                    del self._source_by_id[item.id]
        for segment in playlist.segments:
            with self._lock:
                if self._closed or self._terminal:
                    raise CompositionFailure('session_closed')
                if segment.uri in self._source:
                    continue
            deadline = time.monotonic() + SOURCE_FETCH_SECONDS
            try:
                with self.client.open(segment.uri, {}, self.cancel, deadline=deadline,
                                      allowed_origin=playlist.final_url) as response:
                    if response.status != 200:
                        raise SourceUnavailable('source_unavailable')
                    data = read_bounded(response, SOURCE_BYTES)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Source segment fetch timed out')
            except Cancelled:
                raise
            except SourceUnavailable:
                raise
            except OSError as error:
                if self.cancel.is_set():
                    raise Cancelled('Request cancelled') from error
                raise SourceUnavailable('source_unavailable') from error
            info = inspect_ts(data)
            if abs(info.samples / 48000 - segment.duration) > 1024 / 48000:
                raise CompositionFailure('source_duration_mismatch')
            item = CachedSegment(uuid.uuid4().hex, Path(''), info)
            with self._lock:
                if self._closed or self._terminal:
                    raise CompositionFailure('session_closed')
                if segment.uri in self._source:
                    continue
                self._make_source_room(len(data), needed)
                self._source[segment.uri] = (item, data)
                self._source_by_id[item.id] = data
        with self._lock:
            if self._closed or self._terminal:
                raise CompositionFailure('session_closed')
            return {url: value[0] for url, value in self._source.items()}

    def render(self, playlist, route):
        with self._render_lock:
            with self._lock:
                if self._closed or self._terminal:
                    raise CompositionFailure('session_closed')
                if origin(playlist.final_url) != self.source_origin:
                    raise CompositionFailure('source_origin_changed')
                # A complete break may appear in the first source window before
                # asynchronous midroll preparation finishes. Publish the safe
                # programme prefix, then expose the interval on a later reload.
                if self.config.midroll and not self._midroll_done.is_set():
                    starts = [cue.sequence for cue in playlist.cues if cue.source == 'EXT-X-CUE-OUT']
                    if starts:
                        boundary = min(starts)
                        prefix = tuple(s for s in playlist.segments if s.sequence < boundary)
                        if prefix:
                            playlist = replace(playlist, segments=prefix,
                                               cues=tuple(c for c in playlist.cues if c.sequence < boundary),
                                               lines=tuple(line for line in playlist.lines
                                                           if not line.startswith('#EXT-X-ENDLIST')))
            # Keep network I/O outside the state lock. Media reads for already
            # published immutable segments can continue while this window loads.
            sources = self._cache_window(playlist)
            with self._lock:
                if self._closed or self._terminal:
                    raise CompositionFailure('session_closed')
                return self._compose_locked(playlist, route, sources)

    def _compose_locked(self, playlist, route, sources):
        try:
            presentation = self.timeline.update(playlist, sources)
        except TimelineEpochLimit as error:
            raise CompositionFailure(str(error)) from error
        if not presentation.segments:
            raise CompositionFailure('empty_composition')
        self._committed = True
        for segment in presentation.segments:
            self._published_order[segment.resource_id] = None
            self._published_order.move_to_end(segment.resource_id)
        # Keep enough published IDs for the current output window plus an
        # incoming source window, while allowing old source bytes and creative
        # pins to be released during long-running radio sessions.
        retention_limit = max(1, min(2 * self._limit, SOURCE_SEGMENTS - self._limit))
        while len(self._published_order) > retention_limit:
            self._published_order.popitem(last=False)
        self._published = set(self._published_order)
        if self._midroll is not None:
            for placement in presentation.placements:
                if placement.ad is self._midroll.ad:
                    self._midroll_placement_id = placement.id
            if (self._midroll_placement_id is not None
                    and all(p.id != self._midroll_placement_id for p in presentation.placements)
                    and all(item.id not in self._published and item.id not in self.active_ids()
                            for item in self._midroll.segments)):
                for item in self._midroll.segments:
                    self.creative.unpin(item.id)
                self._prepared.remove(self._midroll)
                self._midroll = None
        # The timeline has retired old output. Release its cache handoff pins.
        for url, (item, data) in list(self._source.items()):
            if (item.id not in self._published and item.id not in self.active_ids()
                    and url not in {s.uri for s in playlist.segments}):
                del self._source[url]
                del self._source_by_id[item.id]
        for ad in list(self._prepared):
            if (all(item.id not in self._published and item.id not in self.active_ids()
                    for item in ad.segments) and ad is not self._midroll):
                for item in ad.segments:
                    self.creative.unpin(item.id)
                self._prepared.remove(ad)
        first = presentation.segments[0]
        longest = max(s.samples for s in presentation.segments)
        lines = ['#EXTM3U', '#EXT-X-VERSION:3',
                 '#EXT-X-TARGETDURATION:%d' % math.ceil(longest / 48000),
                 '#EXT-X-MEDIA-SEQUENCE:%d' % first.sequence,
                 '#EXT-X-DISCONTINUITY-SEQUENCE:%d' % first.discontinuity]
        if self._initial:
            # The Kodi adapter maps media-relative getTime() from the
            # first output segment. Request an explicit sample-zero start
            # for this fixture-only composed presentation.
            lines.append('#EXT-X-START:TIME-OFFSET=0,PRECISE=YES')
        last_disc = first.discontinuity
        for index, segment in enumerate(presentation.segments):
            if index and segment.discontinuity != last_disc:
                lines.append('#EXT-X-DISCONTINUITY')
            lines.append('#EXTINF:%.6f,' % (segment.samples / 48000))
            lines.append(route(segment.resource_id))
            last_disc = segment.discontinuity
        if presentation.ended:
            lines.append('#EXT-X-ENDLIST')
        self._initial = False
        return ('\n'.join(lines) + '\n').encode()

    def read(self, resource_id):
        # Source bytes are immutable after publication. Avoid making playback
        # segment requests wait while a playlist reload fetches its next window.
        data = self._source_by_id.get(resource_id)
        if data is not None:
            return data
        return self.creative.read(resource_id)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for ad in self._prepared:
                for item in ad.segments:
                    try:
                        self.creative.unpin(item.id)
                    except KeyError:
                        pass
            self._prepared.clear()
            self._source.clear()
            self._source_by_id.clear()
            self._published.clear()
            self._published_order.clear()
            self.creative.close()
