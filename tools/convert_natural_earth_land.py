#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert a local official Natural Earth land ZIP to bounded coordinates.

Usage: python3 tools/convert_natural_earth_land.py /local/ne_110m_land.zip output.json
Only the Polygon shapefile and version marker are read. No network calls or
third-party packages are used, and ZIP members are never extracted to disk.
"""
import argparse
import json
import math
from pathlib import Path
import struct
import sys
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       'plugin.audio.kodi_donor_radio/resources/lib'))
from radio_map import LAND_SOURCE, MAX_LAND_BYTES, MAX_LAND_POINTS, _validated_land_polygons


def _read_member(archive, name, limit):
    matches = [info for info in archive.infolist() if info.filename == name]
    if len(matches) != 1 or matches[0].file_size > limit:
        raise ValueError('Missing, duplicate or oversized ZIP member: ' + name)
    with archive.open(matches[0]) as source:
        payload = source.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('ZIP member exceeds size limit')
    return payload


def _parse_shapefile(payload):
    if (len(payload) < 100 or struct.unpack_from('>i', payload)[0] != 9994 or
            struct.unpack_from('>i', payload, 24)[0] * 2 != len(payload) or
            struct.unpack_from('<2i', payload, 28) != (1000, 5)):
        raise ValueError('Expected complete Polygon shapefile')
    polygons, offset, count = [], 100, 0
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ValueError('Truncated shapefile record header')
        size = struct.unpack_from('>i', payload, offset + 4)[0] * 2
        offset += 8
        end = offset + size
        if size < 44 or end > len(payload) or struct.unpack_from('<i', payload, offset)[0] != 5:
            raise ValueError('Invalid Polygon record')
        parts, points = struct.unpack_from('<2i', payload, offset + 36)
        count += points
        if (parts < 1 or points < parts * 4 or count > MAX_LAND_POINTS or
                size != 44 + parts * 4 + points * 16):
            raise ValueError('Invalid or excessive Polygon record counts')
        starts = list(struct.unpack_from('<%di' % parts, payload, offset + 44)) + [points]
        if starts[0] != 0 or any(b - a < 4 for a, b in zip(starts, starts[1:])):
            raise ValueError('Invalid Polygon part offsets')
        coordinates = []
        for i in range(points):
            point = struct.unpack_from('<2d', payload, offset + 44 + parts * 4 + i * 16)
            # Official data has machine-epsilon overshoot at +/-180 and -90.
            # Six decimals retains sub-meter precision and normalizes it.
            if not all(math.isfinite(value) for value in point):
                raise ValueError('Nonfinite Polygon coordinate')
            coordinates.append([round(value, 6) for value in point])
        polygons.append([coordinates[a:b] for a, b in zip(starts, starts[1:])])
        offset = end
    if not polygons:
        raise ValueError('Shapefile has no land polygons')
    _validated_land_polygons(polygons)
    return polygons


def convert_land_zip(source_path, output_path):
    """Read a local v4.1.0 ZIP, preserving ring parts but dropping attributes."""
    # Limit the archive as well as its individual decompressed members.
    if Path(source_path).stat().st_size > MAX_LAND_BYTES:
        raise ValueError('Land ZIP exceeds size limit')
    with zipfile.ZipFile(source_path) as archive:
        version = _read_member(archive, 'ne_110m_land.VERSION.txt', 64).decode('ascii').strip()
        if version != '4.1.0':
            raise ValueError('Expected Natural Earth land version 4.1.0')
        polygons = _parse_shapefile(_read_member(archive, 'ne_110m_land.shp', MAX_LAND_BYTES))
    data = {'schema_version': 1, 'source': LAND_SOURCE, 'polygons': polygons}
    payload = (json.dumps(data, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(payload) > MAX_LAND_BYTES:
        raise ValueError('Converted coordinates exceed size limit')
    Path(output_path).write_bytes(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_zip', type=Path, help='Local official ne_110m_land.zip')
    parser.add_argument('output_json', type=Path)
    args = parser.parse_args()
    try:
        convert_land_zip(args.source_zip, args.output_json)
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeError) as exc:
        parser.exit(1, 'Conversion failed: %s\n' % exc)


if __name__ == '__main__':
    main()
