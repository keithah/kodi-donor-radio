"""Geographic fixtures with independently checked PNG pixels."""
import json
from pathlib import Path
import struct
import zlib

import pytest

import radio_map
from radio_map import MapView, project


SOURCE = 'Natural Earth 1:110m land v4.1.0'
LAND_PATH = (Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio' /
             'resources/map/natural_earth_110m_land.json')


def rectangle(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def write_data(tmp_path, fixture_polygons, **changes):
    data = dict(schema_version=1, source=SOURCE, polygons=fixture_polygons)
    data.update(changes)
    path = tmp_path / 'land.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def decode_png(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    offset, compressed, kinds = 8, bytearray(), []
    while offset < len(data):
        length = struct.unpack_from('>I', data, offset)[0]
        kind, body = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from('>I', data, offset + 8 + length)[0]
        assert crc == zlib.crc32(kind + body) & 0xffffffff
        kinds.append(kind)
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            assert (depth, color, compression, filtering, interlace) == (8, 6, 0, 0, 0)
        elif kind == b'IDAT':
            compressed.extend(body)
        offset += 12 + length
    assert kinds[0] == b'IHDR' and kinds[-1] == b'IEND'
    assert offset == len(data)
    raw = zlib.decompress(compressed)
    assert len(raw) == height * (width * 4 + 1)
    rows = []
    for y in range(height):
        row = raw[y * (width * 4 + 1):(y + 1) * (width * 4 + 1)]
        assert row[0] == 0
        rows.append([tuple(row[x:x + 4]) for x in range(1, len(row), 4)])
    return width, height, rows


def render(polygons, view=None, width=40, height=20):
    view = view or MapView(90, -90, -180, 180, 400, 200)
    return decode_png(radio_map.render_land_png(polygons, view, width, height))[2]


def test_renderer_aligns_regional_land_with_pin_projection():
    view = MapView(60, 20, -130, -60, 70, 40)
    rows = render([[rectangle(-110, 30, -90, 50)]], view, 70, 40)
    ocean, land = rows[0][0], rows[20][30]
    assert ocean != land and ocean[3] == land[3] == 255
    # Bounds project to x=20..40, y=10..30; sample pixel centers.
    assert project(40, -100, view) == (30, 20)
    assert all(rows[y][x] == land for y in range(10, 30) for x in range(20, 40))
    assert rows[9][30] == rows[30][30] == rows[20][19] == rows[20][40] == ocean


def test_polygon_surrounding_view_clips_to_all_four_edges():
    view = MapView(10, -10, -10, 10, 40, 20)
    rows = render([[rectangle(-20, -20, 20, 20)]], view)
    ocean = render([], view)[0][0]
    assert all(pixel != ocean for row in rows for pixel in row)


def test_clipping_keeps_slanted_coastline_and_excludes_outside_polygon():
    view = MapView(10, -10, -10, 10, 40, 20)
    triangle = [[-20, -20], [20, -20], [-20, 20], [-20, -20]]
    rows = render([[triangle], [rectangle(40, 40, 50, 50)]], view)
    ocean = render([], view)[0][0]
    assert rows[15][5] != ocean
    assert rows[5][30] == ocean


@pytest.mark.parametrize('reverse', [False, True])
def test_holes_stay_ocean_and_separate_islands_remain_land(reverse):
    outer, hole = rectangle(-80, -60, 80, 60), rectangle(-40, -30, 40, 30)
    if reverse:
        outer.reverse()
    rows = render([[outer, hole], [rectangle(-10, -10, 10, 10)]], width=360, height=180)
    ocean = rows[0][0]
    assert rows[90][120] != ocean  # outer shell
    assert rows[90][150] == ocean  # hole
    assert rows[90][180] != ocean  # island inside hole


def test_antimeridian_view_places_both_sides_without_filling_center():
    view = MapView(20, -20, 170, -170, 80, 40)
    rows = render([[rectangle(172, -10, 176, 10)],
                   [rectangle(-176, -10, -172, 10)]], view, 80, 40)
    ocean = rows[0][0]
    assert rows[20][16] != ocean and rows[20][64] != ocean
    assert rows[20][40] == ocean


def test_ring_crossing_antimeridian_is_short_and_repeats_at_world_edges():
    polygon = [rectangle(175, -10, -175, 10)]
    rows = render([polygon], MapView(20, -20, 170, -170, 80, 40), 80, 40)
    ocean = rows[0][0]
    assert rows[20][40] != ocean and rows[20][0] == rows[20][79] == ocean
    world = render([polygon], width=360, height=180)
    assert world[90][0] != ocean and world[90][359] != ocean
    assert world[90][180] == ocean


def test_full_longitude_polar_polygon_does_not_collapse_at_seam():
    rows = render([[rectangle(-180, -90, 180, -70)]], width=360, height=180)
    ocean = rows[0][0]
    assert rows[179][0] != ocean and rows[179][359] != ocean
    assert rows[100][180] == ocean


def test_png_defaults_dimensions_and_size_are_bounded():
    data = radio_map.render_land_png([], MapView(90, -90, -180, 180, 1030, 515))
    width, height, rows = decode_png(data)
    assert (width, height) == (1680, 840)
    assert len(data) <= 2 * 1024 * 1024
    assert rows[0][0] == rows[-1][-1]


@pytest.mark.parametrize('width,height', [(0, 10), (10, -1), (True, 10), (10.5, 10),
                                          (8193, 1), (8192, 8192)])
def test_renderer_rejects_invalid_or_excessive_canvas_before_allocation(width, height):
    with pytest.raises(ValueError):
        radio_map.render_land_png([], MapView(90, -90, -180, 180, 10, 10), width, height)


def test_loader_returns_immutable_validated_coordinates(tmp_path):
    polygons = radio_map.load_land_polygons(write_data(tmp_path, [[rectangle(-20, -10, 20, 10)]]))
    assert polygons == ((((-20, -10), (20, -10), (20, 10), (-20, 10), (-20, -10)),),)


@pytest.mark.parametrize('change', [dict(schema_version=2), dict(schema_version=True),
                                   dict(source='wrong dataset'), dict(polygons={}),
                                   dict(extra='attributes are not coordinates')])
def test_loader_rejects_wrong_schema_and_metadata(tmp_path, change):
    with pytest.raises(ValueError):
        radio_map.load_land_polygons(write_data(tmp_path, [[rectangle(0, 0, 10, 10)]], **change))


@pytest.mark.parametrize('ring', [[], [[0, 0]] * 3, [[0, 0], [1, 0], [1, 1], [0, 1]],
                                 [[0, 0], [181, 0], [1, 1], [0, 0]],
                                 [[0, 0], [1, 91], [1, 1], [0, 0]],
                                 [[0, 0], [True, 1], [1, 1], [0, 0]],
                                 [[0, 0], [float('nan'), 1], [1, 1], [0, 0]],
                                 [[0, 0], [float('inf'), 1], [1, 1], [0, 0]],
                                 [[0, 0], ['1', 1], [1, 1], [0, 0]],
                                 [[0, 0], [1, 1, 2], [1, 1], [0, 0]]])
def test_loader_rejects_malformed_rings_and_coordinates(tmp_path, ring):
    with pytest.raises(ValueError):
        radio_map.load_land_polygons(write_data(tmp_path, [[ring]]))


def test_loader_rejects_empty_polygon_and_excessive_point_count(tmp_path):
    with pytest.raises(ValueError):
        radio_map.load_land_polygons(write_data(tmp_path, [[]]))
    path = tmp_path / 'too-many.json'
    path.write_text('{"schema_version":1,"source":"' + SOURCE +
                    '","polygons":[[[' + ','.join(['[0,0]'] * 100001) + ']]]}')
    with pytest.raises(ValueError):
        radio_map.load_land_polygons(path)


def test_loader_bounds_file_read_and_rejects_invalid_json(tmp_path):
    path = tmp_path / 'invalid.json'
    for data in (b' ' * (2 * 1024 * 1024 + 1), b'not JSON', b'\xff', b'[]'):
        path.write_bytes(data)
        with pytest.raises(ValueError):
            radio_map.load_land_polygons(path)


def test_renderer_validates_untrusted_polygon_inputs_too():
    with pytest.raises(ValueError):
        render([[rectangle(-181, 0, 0, 10)]])
    with pytest.raises(TypeError):
        radio_map.render_land_png([], None)


def test_renderer_rejects_pathological_raster_work():
    # All 2,600 edges span the full height, exceeding five million edge rows
    # while remaining comfortably within the point and canvas limits.
    ring = [[-0.5, -1], [0.5, 1]] * 1300 + [[-0.5, -1]]
    with pytest.raises(ValueError, match='work exceeds'):
        render([[ring]], MapView(1, -1, -1, 1, 40, 2048), 40, 2048)


def test_bundled_data_is_bounded_and_recognizes_continents():
    assert LAND_PATH.stat().st_size <= 2 * 1024 * 1024
    polygons = radio_map.load_land_polygons(LAND_PATH)
    assert 100 < len(polygons) < 1000
    assert sum(len(ring) for polygon in polygons for ring in polygon) <= 100000
    rows = render(polygons, width=720, height=360)
    ocean = rows[180][0]
    # Interior North America, South America, Africa, Eurasia and Australia.
    for x, y in [(160, 80), (240, 200), (400, 180), (560, 100), (630, 230)]:
        assert rows[y][x] != ocean
    assert rows[200][160] == ocean  # central Pacific
