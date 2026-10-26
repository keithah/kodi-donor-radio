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
import struct
import unicodedata
import zlib


MAX_VISIBLE_CITIES = 60
MAX_CANVAS_DIMENSION = 8192
_STATION_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_COUNTRY_CODE = re.compile(r'^[A-Za-z]{2}$')

LAND_SOURCE = 'Natural Earth 1:110m land v4.1.0'
MAX_LAND_BYTES = 2 * 1024 * 1024
MAX_LAND_POINTS = 100000
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_RENDER_PIXELS = 4 * 1024 * 1024
MAX_RASTER_EDGE_STEPS = 5000000
_OCEAN_RGBA = b'\x10\x23\x35\xff'
_LAND_RGBA = b'\x39\x63\x68\xff'


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


def _validated_land_polygons(polygons):
    """Normalize bounded coordinate sequences, including direct renderer input."""
    if not isinstance(polygons, (list, tuple)) or len(polygons) > MAX_LAND_POINTS // 4:
        raise ValueError('Invalid land polygons')
    result, count = [], 0
    for polygon in polygons:
        if (not isinstance(polygon, (list, tuple)) or not polygon or
                len(polygon) > MAX_LAND_POINTS // 4):
            raise ValueError('Invalid land polygon rings')
        rings = []
        for ring in polygon:
            if not isinstance(ring, (list, tuple)) or len(ring) < 4:
                raise ValueError('Land rings must be closed with at least four points')
            count += len(ring)
            if count > MAX_LAND_POINTS:
                raise ValueError('Too many land coordinates')
            points = []
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError('Invalid land coordinate pair')
                for value, low, high in ((point[0], -180, 180), (point[1], -90, 90)):
                    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                            not low <= value <= high or not math.isfinite(value)):
                        raise ValueError('Invalid land coordinate')
                points.append(tuple(point))
            if points[0] != points[-1]:
                raise ValueError('Land rings must be closed')
            rings.append(tuple(points))
        result.append(tuple(rings))
    return tuple(result)


def load_land_polygons(path):
    """Read versioned local land geometry, at most 2 MiB and 100,000 points.

    Return immutable polygons of closed rings of (longitude, latitude) pairs.
    Invalid data raises ValueError; local file errors retain their OSError type.
    No Kodi, network, imaging or GIS dependency is needed.
    """
    with open(path, 'rb') as source:
        payload = source.read(MAX_LAND_BYTES + 1)
    if len(payload) > MAX_LAND_BYTES:
        raise ValueError('Land coordinate file exceeds size limit')
    try:
        data = json.loads(payload.decode('utf-8'))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid land coordinate JSON') from exc
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'source', 'polygons'} or
            type(data['schema_version']) is not int or data['schema_version'] != 1 or
            data['source'] != LAND_SOURCE):
        raise ValueError('Unsupported land coordinate schema or source')
    return _validated_land_polygons(data['polygons'])


def _unwrap_land_ring(ring):
    """Use short seam-crossing edges, preserving explicit full-world edges.

    Natural Earth's Antarctica closes along -180/+180 at the pole; that
    explicit 360-degree edge must not be collapsed into a zero-width edge.
    """
    result = [ring[0]]
    shift = 0
    for previous, point in zip(ring, ring[1:]):
        delta = point[0] - previous[0]
        if 180 < delta < 360:
            shift -= 360
        elif -360 < delta < -180:
            shift += 360
        result.append((point[0] + shift, point[1]))
    longitudes = [point[0] for point in result]
    if max(longitudes) - min(longitudes) > 360 or result[0] != result[-1]:
        raise ValueError('Land ring winds around the globe')
    return result


def _clip_land_ring(points, west, east, south, north):
    """Sutherland-Hodgman clipping against the geographic view rectangle."""
    for axis, boundary, keep_above in ((0, west, True), (0, east, False),
                                        (1, south, True), (1, north, False)):
        if not points:
            break
        clipped = []
        previous = points[-1]
        was_inside = previous[axis] >= boundary if keep_above else previous[axis] <= boundary
        for point in points:
            inside = point[axis] >= boundary if keep_above else point[axis] <= boundary
            if inside != was_inside:
                ratio = (boundary - previous[axis]) / (point[axis] - previous[axis])
                intersection = [previous[i] + ratio * (point[i] - previous[i]) for i in (0, 1)]
                intersection[axis] = boundary
                clipped.append(tuple(intersection))
            if inside:
                clipped.append(point)
            previous, was_inside = point, inside
        points = clipped
    return points


def _land_edges(polygon, view, width, height):
    """Bucket clipped pixel edges by their first intersected scanline."""
    west, east = view.west, view.west + view.longitude_span
    starts, work = {}, 0
    for ring in polygon:
        unwrapped = _unwrap_land_ring(ring)
        low, high = min(p[0] for p in unwrapped), max(p[0] for p in unwrapped)
        first, last = math.ceil((west - high) / 360), math.floor((east - low) / 360)
        for copy in range(first, last + 1):
            clipped = _clip_land_ring([(x + copy * 360, y) for x, y in unwrapped],
                                      west, east, view.south, view.north)
            if len(clipped) < 3:
                continue
            # These ratios are the same as project(); retain subpixel geometry
            # until raster filling rather than rounding each coastline vertex.
            points = [((x - west) / view.longitude_span * width,
                       (view.north - y) / (view.north - view.south) * height)
                      for x, y in clipped]
            for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
                if y0 == y1:
                    continue
                if y0 > y1:
                    x0, y0, x1, y1 = x1, y1, x0, y0
                start = max(0, math.ceil(y0 - 0.5))
                end = min(height, math.ceil(y1 - 0.5))
                if start < end:
                    work += end - start
                    if work > MAX_RASTER_EDGE_STEPS:
                        raise ValueError('Land raster work exceeds limit')
                    starts.setdefault(start, []).append((end, x0, y0, (x1 - x0) / (y1 - y0)))
    return starts, work


def _png_chunk(kind, payload):
    return (struct.pack('>I', len(payload)) + kind + payload +
            struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))


def render_land_png(polygons, view, width=1680, height=840):
    """Render a bounded, opaque RGBA PNG using the pin projection's bounds.

    Each polygon uses even-odd filling so holes are independent of ring
    orientation; separate polygons paint a union, preserving islands in holes.
    Raster coverage samples pixel centers. Output is at most 2 MiB, canvas
    area at most 4 Mi pixels, and edge/scanline work at most five million.
    """
    if not isinstance(view, MapView):
        raise TypeError('view must be a MapView')
    for size in (width, height):
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_CANVAS_DIMENSION:
            raise ValueError('Invalid land render canvas size')
    if width * height > MAX_RENDER_PIXELS:
        raise ValueError('Land render canvas exceeds pixel limit')
    polygons = _validated_land_polygons(polygons)
    pixels = bytearray(_OCEAN_RGBA * (width * height))
    work = 0
    for polygon in polygons:
        starts, polygon_work = _land_edges(polygon, view, width, height)
        work += polygon_work
        if work > MAX_RASTER_EDGE_STEPS:
            raise ValueError('Land raster work exceeds limit')
        if not starts:
            continue
        active, start_rows, index = [], sorted(starts), 0
        y = start_rows[0]
        while active or index < len(start_rows):
            active = [edge for edge in active if edge[0] > y]
            if index < len(start_rows) and start_rows[index] == y:
                active.extend(starts[y])
                index += 1
            if not active:
                if index < len(start_rows):
                    y = start_rows[index]
                continue
            crossings = sorted(x0 + (y + 0.5 - y0) * slope for _, x0, y0, slope in active)
            for left, right in zip(crossings[::2], crossings[1::2]):
                first = max(0, min(width, math.ceil(left - 0.5)))
                last = max(first, min(width, math.ceil(right - 0.5)))
                offset = (y * width + first) * 4
                pixels[offset:offset + (last - first) * 4] = _LAND_RGBA * (last - first)
            y += 1

    compressor, compressed = zlib.compressobj(), bytearray()
    for y in range(height):
        compressed.extend(compressor.compress(b'\0' + pixels[y * width * 4:(y + 1) * width * 4]))
        if len(compressed) > MAX_PNG_BYTES - 57:
            raise ValueError('Land PNG exceeds size limit')
    compressed.extend(compressor.flush())
    if len(compressed) > MAX_PNG_BYTES - 57:
        raise ValueError('Land PNG exceeds size limit')
    return (b'\x89PNG\r\n\x1a\n' +
            _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) +
            _png_chunk(b'IDAT', compressed) + _png_chunk(b'IEND', b''))


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
