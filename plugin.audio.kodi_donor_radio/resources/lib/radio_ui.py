# SPDX-License-Identifier: GPL-2.0-or-later
"""Safe display records, private profile stores, and pure geographic helpers."""
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

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
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not low <= value <= high):
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

    def __post_init__(self):
        station_identifier(self.station_uuid)
        _text(self.name, 512, required=True)
        for name in ('city', 'country'):
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
        _timestamp(self.last_played)

    @classmethod
    def from_station(cls, station):
        return cls(station.uuid, station.name, **{
            name: getattr(station, name) for name in (
                'city', 'country', 'country_code', 'codec', 'tags', 'favicon',
                'latitude', 'longitude', 'bitrate')})

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
        except (ValueError, UnicodeError) as error:
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
                os.fchmod(target.fileno(), 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, item_id):
        station_identifier(item_id)
        self._write([item for item in self.list() if getattr(item, self.key) != item_id])


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
        items = self.list()
        if make_default:
            items = [replace(item, is_default=False) for item in items]
        self._write(items + [place])
        return place

    def set_default(self, place_id):
        station_identifier(place_id)
        items = self.list()
        if not any(p.place_id == place_id for p in items):
            raise ValueError('Place not found')
        self._write([replace(p, is_default=p.place_id == place_id) for p in items])

    def set_radius(self, place_id, radius_km):
        station_identifier(place_id)
        _number(radius_km, 0.001, 20040)
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
        items = self.list()
        for index, item in enumerate(items):
            if item.station_uuid == station_summary.station_uuid:
                items[index] = replace(station_summary, last_played=item.last_played)
                break
        else:
            items.append(station_summary)
        self._write(items)

    def move(self, station_uuid, index):
        station_identifier(station_uuid)
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
        items = self.list()
        if not any(item.station_uuid == station_uuid for item in items):
            raise ValueError('Favorite not found')
        self._write([replace(item, last_played=played_at) if item.station_uuid == station_uuid
                     else item for item in items])


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
