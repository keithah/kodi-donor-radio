# SPDX-License-Identifier: GPL-2.0-or-later
"""Safe display records, private profile stores, and pure geographic helpers."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playback_arbiter import profile_file_lock

MAX_FAVORITES = 500
MAX_PLACES = 50
MAX_FILE_BYTES = 2 * 1024 * 1024


def station_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid station identifier')
    return value


def _text(value, limit, required=False):
    if (not isinstance(value, str) or len(value) > limit or
            any(ord(c) < 32 for c in value) or (required and not value.strip())):
        raise ValueError('Invalid display text')
    return value


def bounded_text(value, limit):
    """Normalize untrusted directory text before it enters display records."""
    return ''.join(c for c in value if ord(c) >= 32)[:limit] if isinstance(value, str) else ''


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Invalid numeric value')
    # Compare integers before calling isfinite: JSON can contain arbitrarily
    # large integers, and math.isfinite() raises OverflowError when converting
    # those to a C double. Floats still need the explicit NaN/inf check.
    if isinstance(value, int):
        if not low <= value <= high:
            raise ValueError('Invalid numeric value')
    elif not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Invalid numeric value')
    return value


def coordinates(latitude, longitude):
    return _number(latitude, -90, 90), _number(longitude, -180, 180)


def safe_favicon(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return ''
    try:
        parts = urlsplit(value)
        return value if parts.scheme in ('http', 'https') and parts.hostname and not parts.username and not parts.password else ''
    except ValueError:
        return ''


def _timestamp(value):
    _text(value, 40)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Timestamp requires timezone')
        except ValueError as error:
            raise ValueError('Invalid played timestamp') from error


@dataclass(frozen=True)
class StationSummary:
    station_uuid: str
    name: str
    city: str = ''
    country: str = ''
    country_code: str = ''
    codec: str = ''
    tags: str = ''
    favicon: str = ''
    latitude: object = None
    longitude: object = None
    bitrate: int = 0
    last_played: str = ''
    state: str = ''
    click_count: int = 0

    def __post_init__(self):
        station_identifier(self.station_uuid)
        _text(self.name, 512, required=True)
        for name in ('city', 'state', 'country'):
            _text(getattr(self, name), 256)
        _text(self.country_code, 2)
        _text(self.codec, 64)
        _text(self.tags, 1024)
        _text(self.favicon, 2048)
        if self.favicon and safe_favicon(self.favicon) != self.favicon:
            raise ValueError('Invalid favicon')
        if self.latitude is not None or self.longitude is not None:
            coordinates(self.latitude, self.longitude)
        if type(self.bitrate) is not int or not 0 <= self.bitrate <= 100000:
            raise ValueError('Invalid bitrate')
        if type(self.click_count) is not int or not 0 <= self.click_count <= 2**63 - 1:
            raise ValueError('Invalid click count')
        _timestamp(self.last_played)

    @classmethod
    def from_station(cls, station):
        return cls(station.uuid, station.name, **{
            name: getattr(station, name) for name in (
                'city', 'state', 'country', 'country_code', 'codec', 'tags', 'favicon',
                'latitude', 'longitude', 'bitrate', 'click_count')})

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, record):
        if not isinstance(record, dict) or set(record) - {f.name for f in fields(cls)}:
            raise ValueError('Invalid station summary schema')
        try:
            return cls(**record)
        except TypeError as error:
            raise ValueError('Invalid station summary schema') from error


@dataclass(frozen=True)
class SavedPlace:
    place_id: str
    name: str
    latitude: float
    longitude: float
    radius_km: float
    is_default: bool = False
    country_code: str = ''

    def __post_init__(self):
        station_identifier(self.place_id)
        _text(self.name, 128, required=True)
        coordinates(self.latitude, self.longitude)
        _number(self.radius_km, 0.001, 20040)
        if not isinstance(self.country_code, str) or (self.country_code and
                not re.fullmatch(r'[A-Z]{2}', self.country_code)):
            raise ValueError('Invalid country code')
        if type(self.is_default) is not bool:
            raise ValueError('Invalid default place flag')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate profile key')
        result[key] = value
    return result


class _Store:
    def __init__(self, profile_path):
        self.profile = Path(profile_path)
        self.path = self.profile / self.filename

    def list(self):
        try:
            with self.path.open('rb') as source:
                raw = source.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError('Profile file too large')
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ValueError('Invalid profile JSON') from error
        if (not isinstance(document, dict) or set(document) != {'schema_version', 'items'} or
                type(document['schema_version']) is not int or document['schema_version'] != 1 or
                not isinstance(document['items'], list) or len(document['items']) > self.limit):
            raise ValueError('Invalid profile schema')
        try:
            items = [self.decode(row) for row in document['items']]
        except TypeError as error:
            raise ValueError('Invalid profile item') from error
        ids = [getattr(item, self.key) for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate profile item')
        if self.key == 'place_id' and sum(p.is_default for p in items) > 1:
            raise ValueError('Multiple default places')
        return items

    @contextmanager
    def _mutation(self):
        """Serialize profile read-modify-write across Kodi's addon processes."""
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        with profile_file_lock(self.profile / ('.' + self.filename + '.lock')):
            yield

    def _write(self, items):
        if len(items) > self.limit:
            raise ValueError('Profile item limit reached')
        data = json.dumps({'schema_version': 1, 'items': [asdict(item) for item in items]},
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('Profile file too large')
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + self.filename, dir=str(self.profile))
        try:
            with os.fdopen(descriptor, 'wb') as target:
                # chmod(path) is available on the add-on's older Windows Python
                # targets, while os.fchmod is not consistently exposed there.
                os.chmod(temporary, 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, item_id):
        station_identifier(item_id)
        with self._mutation():
            self._write([item for item in self.list() if getattr(item, self.key) != item_id])


_DIRECTORY_ROW_LIMITS = {'countries': 300, 'tags': 50, 'place': 200, 'map': 200}


def _directory_rows(kind, rows):
    if (not isinstance(kind, str) or kind not in _DIRECTORY_ROW_LIMITS or
            not isinstance(rows, (list, tuple)) or len(rows) > _DIRECTORY_ROW_LIMITS[kind]):
        raise ValueError('Invalid directory cache rows')
    result = []
    for row in rows:
        if kind == 'countries':
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError('Invalid cached country')
            name, code = row
            _text(name, 256, required=True)
            if not isinstance(code, str) or not re.fullmatch(r'[A-Z]{2}', code):
                raise ValueError('Invalid cached country code')
            result.append((name, code))
        elif kind == 'tags':
            result.append(_text(row, 256, required=True))
        else:
            if type(row) is not StationSummary:
                raise ValueError('Expected safe station summary')
            result.append(row)
    return tuple(result)


@dataclass(frozen=True)
class DirectoryCachePage:
    kind: str
    rows: tuple
    continuation: object
    stored_at: float
    fresh: bool

    def __post_init__(self):
        object.__setattr__(self, 'rows', _directory_rows(self.kind, self.rows))
        if ((self.kind == 'tags' and type(self.continuation) is not bool) or
                (self.kind == 'place' and self.continuation is not None and
                 (type(self.continuation) is not int or self.continuation < 0)) or
                (self.kind in ('countries', 'map') and self.continuation is not None)):
            raise ValueError('Invalid directory continuation')
        _number(self.stored_at, 0, float('inf'))
        if type(self.fresh) is not bool:
            raise ValueError('Invalid cache freshness')


@dataclass(frozen=True)
class _DirectoryCacheEntry:
    kind: str
    key_hash: str
    rows: tuple
    continuation: object
    stored_at: float

    @property
    def identity(self):
        return self.kind, self.key_hash


class DirectoryCacheStore(_Store):
    """Bounded display-only directory pages, never resolved stream records."""
    filename, key, limit = 'directory_cache.json', 'identity', 100

    def __init__(self, profile_path, clock=time.time):
        super().__init__(profile_path)
        self.clock = clock

    @staticmethod
    def _identity(kind, key):
        if not isinstance(kind, str) or kind not in _DIRECTORY_ROW_LIMITS or not isinstance(key, str):
            raise ValueError('Invalid directory cache key')
        return kind, hashlib.sha256(key.encode('utf-8')).hexdigest()

    def decode(self, row):
        if not isinstance(row, dict) or set(row) != {f.name for f in fields(_DirectoryCacheEntry)}:
            raise ValueError('Invalid directory cache entry')
        kind = row['kind']
        key_hash = row['key_hash']
        if not isinstance(key_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', key_hash):
            raise ValueError('Invalid directory cache hash')
        rows = row['rows']
        if kind in ('place', 'map'):
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError('Invalid directory station rows')
            rows = [StationSummary.from_dict(item) for item in rows]
        page = DirectoryCachePage(kind, rows, row['continuation'], row['stored_at'], False)
        _number(page.stored_at, 0, self.clock())
        return _DirectoryCacheEntry(kind, key_hash, page.rows, page.continuation, page.stored_at)

    def get(self, kind, key, ttl_seconds, allow_stale=False):
        identity = self._identity(kind, key)
        _number(ttl_seconds, 0, float('inf'))
        if type(allow_stale) is not bool:
            raise ValueError('Invalid stale cache flag')
        entry = next((item for item in self.list() if item.identity == identity), None)
        if entry is None:
            return None
        fresh = self.clock() - entry.stored_at < ttl_seconds
        if not fresh and not allow_stale:
            return None
        return DirectoryCachePage(entry.kind, entry.rows, entry.continuation, entry.stored_at, fresh)

    def put(self, kind, key, rows, continuation=None, stored_at=None):
        identity = self._identity(kind, key)
        timestamp = self.clock() if stored_at is None else stored_at
        _number(timestamp, 0, self.clock())
        page = DirectoryCachePage(kind, rows, continuation, timestamp, True)
        entry = _DirectoryCacheEntry(kind, identity[1], page.rows, continuation, timestamp)
        with self._mutation():
            items = [item for item in self.list() if item.identity != identity]
            items.append(entry)
            # Retain the newest pages, including on backdated writes.
            items.sort(key=lambda item: item.stored_at, reverse=True)
            self._write(items[:self.limit])


class PlaceStore(_Store):
    filename, key = 'places.json', 'place_id'

    @property
    def limit(self):
        return MAX_PLACES

    @staticmethod
    def decode(row):
        required = {f.name for f in fields(SavedPlace)} - {'country_code'}
        if not isinstance(row, dict) or set(row) not in (required, required | {'country_code'}):
            raise ValueError('Invalid place schema')
        return SavedPlace(**row)

    def save(self, name, latitude, longitude, radius_km, make_default=False, country_code=''):
        place = SavedPlace(str(uuid.uuid4()), name, latitude, longitude, radius_km, make_default, country_code)
        with self._mutation():
            items = self.list()
            if make_default:
                items = [replace(item, is_default=False) for item in items]
            self._write(items + [place])
        return place

    def save_many(self, records):
        """Validate the complete setup before atomically appending any places."""
        if not isinstance(records, list) or not records or len(records) > self.limit:
            raise ValueError('Invalid setup places')
        with self._mutation():
            items = self.list()
            default_exists = any(place.is_default for place in items)
            added = []
            for record in records:
                if not isinstance(record, dict) or set(record) - {
                        'name', 'latitude', 'longitude', 'radius_km', 'country_code'}:
                    raise ValueError('Invalid setup place')
                try:
                    added.append(SavedPlace(str(uuid.uuid4()),
                                            is_default=not default_exists and not added, **record))
                except TypeError as error:
                    raise ValueError('Invalid setup place') from error
            self._write(items + added)
        return added

    def remove(self, place_id):
        station_identifier(place_id)
        with self._mutation():
            items = self.list()
            removed_default = any(p.place_id == place_id and p.is_default for p in items)
            remaining = [p for p in items if p.place_id != place_id]
            if removed_default and remaining:
                remaining[0] = replace(remaining[0], is_default=True)
            self._write(remaining)

    def set_default(self, place_id):
        station_identifier(place_id)
        with self._mutation():
            items = self.list()
            if not any(p.place_id == place_id for p in items):
                raise ValueError('Place not found')
            self._write([replace(p, is_default=p.place_id == place_id) for p in items])

    def set_radius(self, place_id, radius_km):
        station_identifier(place_id)
        _number(radius_km, 0.001, 20040)
        with self._mutation():
            items = self.list()
            if not any(p.place_id == place_id for p in items):
                raise ValueError('Place not found')
            self._write([replace(p, radius_km=radius_km) if p.place_id == place_id else p for p in items])


class FavoriteStore(_Store):
    filename, key = 'favorites.json', 'station_uuid'
    decode = staticmethod(StationSummary.from_dict)

    @property
    def limit(self):
        return MAX_FAVORITES

    def add(self, station_summary):
        if type(station_summary) is not StationSummary:
            raise ValueError('Expected safe station summary')
        with self._mutation():
            items = self.list()
            for index, item in enumerate(items):
                if item.station_uuid == station_summary.station_uuid:
                    items[index] = replace(station_summary, last_played=item.last_played)
                    break
            else:
                items.append(station_summary)
            self._write(items)

    def refresh(self, summaries):
        """Atomically merge display fields into current favorites, preserving user state."""
        if (not isinstance(summaries, (list, tuple)) or len(summaries) > self.limit or
                any(type(summary) is not StationSummary for summary in summaries)):
            raise ValueError('Expected safe station summaries')
        updates = {summary.station_uuid: summary for summary in summaries}
        with self._mutation():
            items = self.list()
            refreshed = [replace(updates[item.station_uuid], last_played=item.last_played)
                         if item.station_uuid in updates else item for item in items]
            if refreshed != items:
                self._write(refreshed)

    def move(self, station_uuid, index):
        station_identifier(station_uuid)
        with self._mutation():
            items = self.list()
            if type(index) is not int or not 0 <= index < len(items):
                raise ValueError('Invalid favorite position')
            found = next((i for i, item in enumerate(items) if item.station_uuid == station_uuid), None)
            if found is None:
                raise ValueError('Favorite not found')
            items.insert(index, items.pop(found))
            self._write(items)

    def mark_played(self, station_uuid, played_at):
        station_identifier(station_uuid)
        _timestamp(played_at)
        with self._mutation():
            items = self.list()
            if not any(item.station_uuid == station_uuid for item in items):
                raise ValueError('Favorite not found')
            self._write([replace(item, last_played=played_at) if item.station_uuid == station_uuid
                         else item for item in items])


def _required_instant(value):
    _text(value, 40, required=True)
    _timestamp(value)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError('Invalid timestamp instant') from error


@dataclass(frozen=True)
class RecentSearch:
    query: str
    scope: str
    timestamp: str

    def __post_init__(self):
        _text(self.query, 256, required=True)
        if '\x7f' in self.query or self.scope not in ('name', 'tag', 'country', 'language'):
            raise ValueError('Invalid recent search')
        _required_instant(self.timestamp)

    @property
    def identity(self):
        return self.query.strip().casefold(), self.scope


@dataclass(frozen=True)
class HistoryEntry:
    station_uuid: str
    play_count: int
    last_played: str

    def __post_init__(self):
        station_identifier(self.station_uuid)
        if type(self.play_count) is not int or self.play_count < 1:
            raise ValueError('Invalid play count')
        _required_instant(self.last_played)


def _profile_record(record_type, row):
    if not isinstance(row, dict) or set(row) != {f.name for f in fields(record_type)}:
        raise ValueError('Invalid profile item schema')
    return record_type(**row)


class RecentSearchStore(_Store):
    filename, key, limit = 'recent_searches.json', 'identity', 10

    @staticmethod
    def decode(row):
        return _profile_record(RecentSearch, row)

    def list(self):
        return sorted(super().list(), key=lambda item: _required_instant(item.timestamp), reverse=True)

    def record(self, query, scope, timestamp):
        record = RecentSearch(query, scope, timestamp)
        with self._mutation():
            items = [item for item in self.list() if item.identity != record.identity]
            items.insert(0, record)
            items.sort(key=lambda item: _required_instant(item.timestamp), reverse=True)
            self._write(items[:self.limit])


class HistoryStore(_Store):
    filename, key, limit = 'history.json', 'station_uuid', 500

    @staticmethod
    def decode(row):
        return _profile_record(HistoryEntry, row)

    @staticmethod
    def _ordered(items):
        # Stable sorts preserve ascending station IDs among equal UTC instants.
        return sorted(sorted(items, key=lambda item: item.station_uuid),
                      key=lambda item: _required_instant(item.last_played), reverse=True)

    def list(self):
        return self._ordered(super().list())

    def record_play(self, station_uuid, played_at):
        record = HistoryEntry(station_uuid, 1, played_at)
        with self._mutation():
            items = self.list()
            previous = next((item for item in items if item.station_uuid == station_uuid), None)
            if previous is not None:
                record = replace(record, play_count=previous.play_count + 1)
            items = [item for item in items if item.station_uuid != station_uuid]
            self._write(self._ordered(items + [record])[:self.limit])


def distance_km(lat1, lon1, lat2, lon2):
    lat1, lon1 = coordinates(lat1, lon1)
    lat2, lon2 = coordinates(lat2, lon2)
    delta_lat, delta_lon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(delta_lat / 2) ** 2 + math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) * math.sin(delta_lon / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def stations_in_radius(stations, place, radius_km=None):
    if place.latitude is None or place.longitude is None:
        return []
    coordinates(place.latitude, place.longitude)
    radius = place.radius_km if radius_km is None else radius_km
    _number(radius, 0.001, 20040)
    found = []
    for station in stations:
        if station.latitude is None or station.longitude is None:
            continue
        try:
            distance = distance_km(place.latitude, place.longitude, station.latitude, station.longitude)
        except ValueError:
            continue
        if distance <= radius:
            identifier = station.uuid if hasattr(station, 'uuid') else station.station_uuid
            found.append((distance, station.name.casefold(), identifier, station))
    return [item[3] for item in sorted(found, key=lambda item: item[:3])]
