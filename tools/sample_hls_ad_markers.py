# SPDX-License-Identifier: GPL-2.0-or-later
"""Discover and sample bounded Radio Browser HLS cue observations."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

_LIBRARY = str(Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio' /
               'resources' / 'lib')
if _LIBRARY not in sys.path:
    sys.path.insert(0, _LIBRARY)

from cues import CueTracker
from hls import Playlist, UnsupportedPlaylist, parse_playlist
from http_client import BODY_BYTES, HttpClient, HttpError, read_bounded
from models import Event
from models import Station
from radio_browser import RadioBrowser
from url_policy import UnsafeUrl


PAGE_SIZE = 50
EVENT_KINDS = frozenset(('cue_observed', 'break_start', 'break_update',
                         'break_end', 'break_unresolved'))
EVENT_DETAILS = ('media_sequence', 'discontinuity', 'media_position',
                 'duration', 'classification', 'reason')
EVENT_LIMIT = 1024
HLS_CONTENT_TYPES = frozenset((
    'application/vnd.apple.mpegurl', 'application/x-mpegurl',
    'application/mpegurl', 'audio/x-mpegurl', 'audio/mpegurl',
))
OUTCOMES = frozenset((
    'marker_observed', 'no_marker_observed', 'unsupported', 'policy_rejection',
    'reachability_failure', 'parser_failure', 'cancelled',
))


class _DirectoryDeadlineClient:
    """Give Radio Browser requests the same total deadline as playlist reads."""
    def __init__(self, client: HttpClient):
        self.client = client

    def open(self, url, headers, cancel):
        return self.client.open(url, headers, cancel, deadline=time.monotonic() + 15)


def _station_name(name):
    """Bound directory text and remove obvious embedded URLs and control text."""
    clean = re.sub(r'(?i)(?:https?://|//)\S+', '[redacted]', str(name))
    clean = re.sub(r'(?i)(?:[?&][A-Za-z0-9_-]+=|\b(?:adswizzContext|token|key)=)\S+',
                   '[redacted]', clean)
    clean = re.sub(r'[\x00-\x1f\x7f]', ' ', clean)
    return clean[:128]


class JsonLineWriter:
    """Serialize bounded records and account for dropped station marker events."""
    def __init__(self, stream):
        self.stream = stream
        self._lock = threading.Lock()
        self._counts = {}
        self._truncated = set()

    def emit_record(self, record: Mapping[str, object]) -> None:
        station_uuid = record.get('station_uuid')
        marker = record.get('kind') in EVENT_KINDS
        line = json.dumps(dict(record), ensure_ascii=True, separators=(',', ':'))
        if len(line) > 4096:
            raise ValueError('Output record exceeds size limit')
        with self._lock:
            if marker:
                count = self._counts.get(station_uuid, 0)
                if count >= EVENT_LIMIT:
                    self._truncated.add(station_uuid)
                    return
                self._counts[station_uuid] = count + 1
            self.stream.write(line + '\n')

    def station_count(self, station_uuid: str) -> int:
        with self._lock:
            return self._counts.get(station_uuid, 0)

    def station_truncated(self, station_uuid: str) -> bool:
        with self._lock:
            return station_uuid in self._truncated


class _StationEventSink:
    def __init__(self, station: Station, writer: JsonLineWriter):
        self.station = station
        self.writer = writer

    def emit(self, event: Event) -> None:
        if event.kind not in EVENT_KINDS:
            return
        record = {'station_uuid': self.station.uuid,
                  'station_name': _station_name(self.station.name),
                  'kind': event.kind, 'source': event.source,
                  'observed_at': event.observed_at}
        for key in EVENT_DETAILS:
            value = event.details.get(key)
            if value is not None:
                record[key] = value
        self.writer.emit_record(record)


def _fetch_playlist(url: str, client: HttpClient, cancel: threading.Event) -> Playlist:
    with client.open(url, {'Accept': 'application/vnd.apple.mpegurl'}, cancel,
                     deadline=time.monotonic() + 15) as response:
        if response.status != 200:
            raise HttpError('Playlist HTTP error')
        content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if content_type not in HLS_CONTENT_TYPES:
            raise UnsupportedPlaylist('Non-playlist content type')
        prefix = b''
        signature = b'#EXTM3U'
        bom = b'\xef\xbb\xbf'
        while len(prefix) < len(signature) + len(bom):
            block = response.read(len(signature) + len(bom) - len(prefix))
            if not block:
                break
            prefix += block
            candidate = prefix[len(bom):] if prefix.startswith(bom) else prefix
            if candidate.startswith(signature):
                break
            if not signature.startswith(candidate) and not bom.startswith(prefix):
                raise UnsupportedPlaylist('Missing HLS signature')
        candidate = prefix[len(bom):] if prefix.startswith(bom) else prefix
        if not candidate.startswith(signature):
            raise UnsupportedPlaylist('Missing HLS signature')
        body = read_bounded(response, BODY_BYTES, prefix)
        if b'#EXT-X-' not in body:
            raise UnsupportedPlaylist('Not an HLS playlist')
        return parse_playlist(body.decode('utf-8-sig'), response.final_url)


def load_media_playlist(url: str, client: HttpClient, cancel: threading.Event,
                        max_depth: int = 3) -> Playlist:
    """Choose the first declared variant, never a segment or media asset."""
    for depth in range(max_depth):
        playlist = _fetch_playlist(url, client, cancel)
        if not playlist.is_master:
            return playlist
        references = [ref for ref in playlist.uri_references if ref.kind == 'playlist']
        if not references:
            raise ValueError('Master playlist has no variant')
        url = references[0].url
    raise ValueError('Master playlist depth exceeded')


def _outcome(error):
    if isinstance(error, UnsafeUrl):
        return 'policy_rejection'
    if isinstance(error, UnsupportedPlaylist):
        return 'unsupported'
    if isinstance(error, (ValueError, UnicodeError)):
        return 'parser_failure'
    return 'reachability_failure'


def sample_station(station: Station, client: HttpClient, writer: JsonLineWriter,
                   duration_seconds: int, cancel: threading.Event,
                   monotonic: Callable, wait: Callable) -> Dict[str, object]:
    """Poll one selected media playlist through one cue tracker."""
    started = None
    polls = 0
    outcome = 'no_marker_observed'
    tracker = CueTracker('sampler', station.uuid, _StationEventSink(station, writer),
                         lambda: datetime.now(timezone.utc), monotonic)
    try:
        if cancel.is_set():
            outcome = 'cancelled'
        else:
            playlist = load_media_playlist(station.url, client, cancel)
            started = monotonic()
            media_url = playlist.final_url
            rendition_id = 'selected-media'
            epoch = 0
            previous_range = None
            while True:
                if cancel.is_set():
                    outcome = 'cancelled'
                    break
                if polls and monotonic() - started >= duration_seconds:
                    break
                if polls:
                    playlist = _fetch_playlist(media_url, client, cancel)
                    if playlist.is_master:
                        raise ValueError('Selected media changed to master')
                first = playlist.media_sequence
                end = first + len(playlist.segments) - 1
                if previous_range is not None and first < previous_range[0] and end < previous_range[0]:
                    epoch += 1
                tracker.observe(playlist, rendition_id, epoch)
                previous_range = (first, end)
                polls += 1
                remaining = duration_seconds - (monotonic() - started)
                if remaining <= 0:
                    break
                interval = max(2.0, min(10.0, (playlist.target_duration or 4.0) / 2.0))
                pause_until = monotonic() + min(interval, remaining)
                while not cancel.is_set() and monotonic() < pause_until:
                    wait(min(0.25, pause_until - monotonic()))
            if outcome != 'cancelled' and writer.station_count(station.uuid):
                outcome = 'marker_observed'
    except Exception as error:
        outcome = 'cancelled' if cancel.is_set() else _outcome(error)
    finally:
        tracker.finish('cancelled' if cancel.is_set() else 'sampling_complete')
    if outcome == 'no_marker_observed' and writer.station_count(station.uuid):
        outcome = 'marker_observed'
    return {'station_uuid': station.uuid, 'station_name': _station_name(station.name),
            'outcome': outcome,
            'elapsed_seconds': max(0.0, monotonic() - started) if started is not None else 0.0,
            'poll_count': polls, 'event_count': writer.station_count(station.uuid),
            'truncated': writer.station_truncated(station.uuid)}


def host_is_cdnstream1(url: str) -> bool:
    """Return whether an HTTP(S) URL has cdnstream1.com as its host boundary."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port  # Access validates the port syntax and range.
        authority = parsed.netloc.rsplit('@', 1)[-1]
        if authority.endswith(':'):
            return False
    except ValueError:
        return False
    if parsed.scheme.lower() not in ('http', 'https') or not hostname:
        return False
    hostname = hostname.lower()
    return hostname == 'cdnstream1.com' or hostname.endswith('.cdnstream1.com')


def discover_candidates(browser: RadioBrowser, page_limit: int,
                        candidate_limit: int) -> Tuple[List[Station], int]:
    """Read bounded directory pages and retain unique cdnstream1 HLS stations."""
    if (type(page_limit) is not int or not 1 <= page_limit <= 100 or
            type(candidate_limit) is not int or not 1 <= candidate_limit <= 20):
        raise ValueError('Invalid discovery limits')

    candidates = []
    seen_uuids = set()
    pages_read = 0
    for page_number in range(page_limit):
        page = browser.search('', '', '', page_number * PAGE_SIZE, PAGE_SIZE)
        pages_read += 1
        for station in page:
            if (station.hls and station.uuid not in seen_uuids and
                    host_is_cdnstream1(station.url)):
                seen_uuids.add(station.uuid)
                candidates.append(station)
                if len(candidates) >= candidate_limit:
                    return candidates, pages_read
        if len(page) < PAGE_SIZE:
            break
    return candidates, pages_read


def _bounded_integer(name: str, minimum: int, maximum: int) -> Callable:
    def convert(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('%s must be an integer' % name)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError('%s must be from %d to %d' %
                                             (name, minimum, maximum))
        return number
    return convert


def _station_summary(station: Station, result: Mapping[str, object]) -> Dict[str, object]:
    outcome = result.get('outcome')
    if outcome not in OUTCOMES:
        outcome = 'reachability_failure'
    return {'kind': 'station_summary', 'station_uuid': station.uuid,
            'station_name': _station_name(station.name), 'outcome': outcome,
            'elapsed_seconds': result.get('elapsed_seconds', 0.0),
            'poll_count': result.get('poll_count', 0),
            'event_count': result.get('event_count', 0),
            'truncated': bool(result.get('truncated', False))}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Sample Radio Browser HLS marker playlists')
    parser.add_argument('--pages', type=_bounded_integer('pages', 1, 100), default=20)
    parser.add_argument('--limit', type=_bounded_integer('limit', 1, 20), default=8)
    parser.add_argument('--workers', type=_bounded_integer('workers', 1, 4), default=4)
    parser.add_argument('--duration-seconds', type=_bounded_integer('duration-seconds', 1200, 7200),
                        default=1200)
    args = parser.parse_args(argv)

    client = HttpClient()
    discovery_cancel = threading.Event()
    writer = JsonLineWriter(sys.stdout)
    try:
        stations, _ = discover_candidates(RadioBrowser(client=_DirectoryDeadlineClient(client),
                                                       cancel=discovery_cancel),
                                          args.pages, args.limit)
    except KeyboardInterrupt:
        discovery_cancel.set()
        return 130
    except Exception:
        print('Station discovery failed', file=sys.stderr)
        return 1

    cancellations = [threading.Event() for _ in stations]
    interrupted = False
    failed = False
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        emitted = set()

        def emit_summary(future):
            nonlocal failed
            station = futures[future]
            try:
                result = future.result()
            except BaseException:
                result = {'outcome': 'cancelled' if interrupted else 'reachability_failure'}
            record = _station_summary(station, result)
            writer.emit_record(record)
            emitted.add(future)
            if record['outcome'] in ('policy_rejection', 'reachability_failure',
                                     'parser_failure', 'cancelled'):
                failed = True

        try:
            for station, cancel in zip(stations, cancellations):
                future = executor.submit(sample_station, station, client, writer,
                                         args.duration_seconds, cancel, time.monotonic, cancel.wait)
                futures[future] = station
            for future in as_completed(futures):
                emit_summary(future)
        except KeyboardInterrupt:
            interrupted = True
            discovery_cancel.set()
            for cancel in cancellations:
                cancel.set()
            for future in futures:
                if future not in emitted:
                    emit_summary(future)
            submitted = {station.uuid for station in futures.values()}
            for station in stations:
                if station.uuid not in submitted:
                    writer.emit_record(_station_summary(station, {'outcome': 'cancelled'}))
    return 130 if interrupted else 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
