# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure map projection and city-pin navigation helpers.

This module intentionally has no Kodi dependency. Station URLs and other
playback data are not retained in the map projection; a pin carries only its
stable ID, safe display fields, and station UUIDs.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata


MAX_VISIBLE_CITIES = 60
MAX_CANVAS_DIMENSION = 8192
_STATION_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_COUNTRY_CODE = re.compile(r'^[A-Za-z]{2}$')


@dataclass(frozen=True)
class MapView:
    """Validated geographic rectangle and pixel canvas.

    ``west > east`` describes a view crossing the antimeridian.  The special
    rectangle west=-180/east=180 spans the full longitude range.
    """

    north: float
    south: float
    west: float
    east: float
    width: int
    height: int

    def __post_init__(self):
        for value, low, high in ((self.north, -90, 90), (self.south, -90, 90),
                                 (self.west, -180, 180), (self.east, -180, 180)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid geographic map bound')
        if (self.north <= self.south or self.west == self.east or
                (self.west == 180 and self.east == -180)):
            raise ValueError('Map bounds must have positive area')
        for size in (self.width, self.height):
            if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_CANVAS_DIMENSION:
                raise ValueError('Invalid map canvas size')

    @property
    def full_longitude(self):
        return self.west == -180 and self.east == 180

    @property
    def longitude_span(self):
        if self.full_longitude:
            return 360.0
        span = (self.east - self.west) % 360.0
        return span


@dataclass(frozen=True)
class CityCluster:
    """Safe, stable display projection for stations at one city location."""

    city_id: str
    city: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    station_count: int
    station_uuids: tuple


def _coordinate(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and low <= value <= high)


def project(latitude, longitude, view):
    """Map a coordinate to rounded canvas pixels, or return ``None`` outside.

    The projection is equirectangular. Invalid coordinates are rejected rather
    than clamped, and antimeridian-crossing rectangles are supported.
    """
    if not isinstance(view, MapView):
        raise TypeError('view must be a MapView')
    if not _coordinate(latitude, -90, 90) or not _coordinate(longitude, -180, 180):
        return None
    if latitude < view.south or latitude > view.north:
        return None

    if view.full_longitude:
        x_ratio = (longitude + 180.0) / 360.0
    else:
        offset = (longitude - view.west) % 360.0
        span = view.longitude_span
        if offset > span:
            return None
        x_ratio = offset / span
    y_ratio = (view.north - latitude) / (view.north - view.south)
    x = min(view.width, max(0, int(round(x_ratio * view.width))))
    y = min(view.height, max(0, int(round(y_ratio * view.height))))
    return x, y


def _get(station, name, default=None):
    if isinstance(station, dict):
        return station.get(name, default)
    return getattr(station, name, default)


def _clean_label(value):
    if not isinstance(value, str):
        return ''
    normalized = ' '.join(''.join(ch for ch in value if unicodedata.category(ch)[0] != 'C').split())
    return normalized[:120]


def _city_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _make_city_id(key):
    payload = json.dumps(key, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()[:20]


def cluster_cities(stations):
    """Group valid stations into deterministic city/country map pins.

    Same-name cities are kept separate when their rounded coordinates differ.
    Invalid or missing coordinates and station IDs are omitted.
    """
    groups = {}
    for station in stations:
        station_uuid = _get(station, 'station_uuid', _get(station, 'uuid', ''))
        latitude = _get(station, 'latitude')
        longitude = _get(station, 'longitude')
        if (not isinstance(station_uuid, str) or not _STATION_ID.fullmatch(station_uuid) or
                not _coordinate(latitude, -90, 90) or not _coordinate(longitude, -180, 180)):
            continue
        # Keep the pure projection free of UI language; the Kodi adapter adds
        # the localized fallback label when it renders an unnamed area.
        city = (_clean_label(_get(station, 'city', '')) or
                _clean_label(_get(station, 'state', '')))
        country = _clean_label(_get(station, 'country', ''))
        raw_country_code = _get(station, 'country_code', '')
        country_code = (raw_country_code.upper() if isinstance(raw_country_code, str) and
                        _COUNTRY_CODE.fullmatch(raw_country_code) else '')
        # Three decimal places are fine enough to separate nearby same-name
        # cities while combining slightly different station coordinates.
        rounded_latitude = round(float(latitude), 3)
        rounded_longitude = round(float(longitude), 3)
        # Make +0.0 and -0.0 share the same deterministic cluster identity.
        rounded_latitude = 0.0 if rounded_latitude == 0 else rounded_latitude
        rounded_longitude = 0.0 if rounded_longitude == 0 else rounded_longitude
        key = (_city_key(city), country_code or _city_key(country),
               rounded_latitude, rounded_longitude)
        group = groups.setdefault(key, {'ids': set(), 'cities': set(), 'countries': set(),
                                        'codes': set()})
        group['ids'].add(station_uuid)
        group['cities'].add(city)
        if country:
            group['countries'].add(country)
        if country_code:
            group['codes'].add(country_code)

    clusters = []
    for key, group in groups.items():
        # Stable labels when source rows differ only in capitalization.
        city = min(group['cities'], key=lambda value: (value.casefold(), value))
        country = min(group['countries'], key=lambda value: (value.casefold(), value)) if group['countries'] else ''
        country_code = min(group['codes']) if group['codes'] else ''
        station_uuids = tuple(sorted(group['ids']))
        clusters.append(CityCluster(
            _make_city_id(key), city, country, country_code,
            key[2], key[3], len(station_uuids), station_uuids,
        ))
    return sorted(clusters, key=lambda city: city.city_id)


def visible_cities(cities, view, selected_city=None, limit=MAX_VISIBLE_CITIES):
    """Return visible pins, selected first then center-proximate and popular.

    The caller's limit is clamped to the hard 60-pin UI budget.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError('limit must be a non-negative integer')
    if limit == 0:
        return []
    limit = min(limit, MAX_VISIBLE_CITIES)
    center_x, center_y = view.width / 2.0, view.height / 2.0
    candidates = []
    for city in cities:
        point = project(city.latitude, city.longitude, view)
        if point is None:
            continue
        distance_sq = (point[0] - center_x) ** 2 + (point[1] - center_y) ** 2
        candidates.append((0 if city.city_id == selected_city else 1,
                           distance_sq, -city.station_count, city.city_id, city))
    candidates.sort(key=lambda item: item[:4])
    return [item[4] for item in candidates[:limit]]


def nearest_city_in_direction(cities, selected_id, direction, view):
    """Return the nearest visible city in a non-wrapping 45-degree cone."""
    if direction not in ('up', 'down', 'left', 'right'):
        raise ValueError('direction must be up, down, left, or right')
    by_id = {city.city_id: city for city in cities}
    selected = by_id.get(selected_id)
    if selected is None:
        return None
    origin = project(selected.latitude, selected.longitude, view)
    if origin is None:
        return None

    candidates = []
    for city in cities:
        if city.city_id == selected_id:
            continue
        point = project(city.latitude, city.longitude, view)
        if point is None:
            continue
        dx, dy = point[0] - origin[0], point[1] - origin[1]
        if direction == 'right':
            along, across = dx, abs(dy)
        elif direction == 'left':
            along, across = -dx, abs(dy)
        elif direction == 'down':
            along, across = dy, abs(dx)
        else:
            along, across = -dy, abs(dx)
        if along <= 0 or across > along:
            continue
        candidates.append((math.hypot(dx, dy), city.city_id, city))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]
