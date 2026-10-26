import json
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

import pytest

import radio_map


ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / 'tools/convert_natural_earth_land.py'


def shapefile(rings, shape_type=5):
    points = [point for ring in rings for point in ring]
    parts, offset = [], 0
    for ring in rings:
        parts.append(offset)
        offset += len(ring)
    bbox = (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))
    record = (struct.pack('<i4d2i', shape_type, *bbox, len(parts), len(points)) +
              struct.pack('<%di' % len(parts), *parts) +
              b''.join(struct.pack('<2d', *p) for p in points))
    length = 100 + 8 + len(record)
    header = (struct.pack('>7i', 9994, 0, 0, 0, 0, 0, length // 2) +
              struct.pack('<2i8d', 1000, shape_type, *bbox, 0, 0, 0, 0))
    return header + struct.pack('>2i', 1, len(record) // 2) + record


def archive(tmp_path, contents, version='4.1.0'):
    path = tmp_path / 'land.zip'
    with zipfile.ZipFile(path, 'w') as output:
        output.writestr('ne_110m_land.shp', contents)
        output.writestr('ne_110m_land.VERSION.txt', version)
        output.writestr('ne_110m_land.dbf', b'private unused attributes')
    return path


def convert(path, target):
    return subprocess.run([sys.executable, str(CONVERTER), str(path), str(target)],
                          capture_output=True, text=True, timeout=10)


def test_local_zip_converter_preserves_parts_and_only_emits_coordinate_schema(tmp_path):
    shell = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    hole = [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]]
    target = tmp_path / 'output.json'
    result = convert(archive(tmp_path, shapefile([shell, hole])), target)
    assert result.returncode == 0, result.stderr
    data = json.loads(target.read_text())
    assert data == {'schema_version': 1, 'source': 'Natural Earth 1:110m land v4.1.0',
                    'polygons': [[shell, hole]]}
    assert len(radio_map.load_land_polygons(target)) == 1
    original = target.read_bytes()
    assert convert(tmp_path / 'land.zip', target).returncode == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize('kind', ['wrong-shape', 'truncated', 'wrong-version', 'bad-coordinate',
                                  'bad-parts', 'excessive-member'])
def test_converter_rejects_invalid_local_archives_without_output(tmp_path, kind):
    ring = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    if kind == 'bad-coordinate':
        ring[1][0] = 181
    data = shapefile([ring], shape_type=3 if kind == 'wrong-shape' else 5)
    if kind == 'truncated':
        data = data[:-2]
    if kind == 'bad-parts':
        data = data[:152] + struct.pack('<i', 2) + data[156:]
    if kind == 'excessive-member':
        data = b' ' * (2 * 1024 * 1024 + 1)
    path = archive(tmp_path, data, version='5.0.0' if kind == 'wrong-version' else '4.1.0')
    target = tmp_path / 'output.json'
    result = convert(path, target)
    assert result.returncode != 0
    assert 'Conversion failed:' in result.stderr
    assert not target.exists()


def test_converter_accepts_only_local_zip_paths(tmp_path):
    target = tmp_path / 'output.json'
    result = convert('https://example.invalid/land.zip', target)
    assert result.returncode != 0
    assert 'Conversion failed:' in result.stderr
    assert not target.exists()
