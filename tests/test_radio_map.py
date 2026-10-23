from types import SimpleNamespace

import pytest

from radio_map import (
    MapView,
    cluster_cities,
    nearest_city_in_direction,
    project,
    visible_cities,
)


def station(uuid, city, lat, lon, country='US', country_name='United States'):
    return SimpleNamespace(station_uuid=uuid, city=city, country_code=country,
                           country=country_name, latitude=lat, longitude=lon)


def test_world_projection_maps_geographic_edges_to_canvas_edges():
    view = MapView(north=90, south=-90, west=-180, east=180, width=1000, height=500)
    assert project(90, -180, view) == (0, 0)
    assert project(-90, 180, view) == (1000, 500)
    assert project(0, 0, view) == (500, 250)


def test_regional_projection_is_bounded_and_rejects_points_outside_view():
    view = MapView(north=60, south=20, west=-130, east=-60, width=700, height=400)
    assert project(60, -130, view) == (0, 0)
    assert project(20, -60, view) == (700, 400)
    assert project(40, -95, view) == (350, 200)
    assert project(61, -95, view) is None
    assert project(40, -131, view) is None


@pytest.mark.parametrize('args', [
    dict(north=91, south=-90, west=-180, east=180, width=10, height=10),
    dict(north=0, south=0, west=-180, east=180, width=10, height=10),
    dict(north=90, south=-90, west=-180, east=-180, width=10, height=10),
    dict(north=90, south=-90, west=180, east=-180, width=10, height=10),
    dict(north=90, south=-90, west=-180, east=180, width=0, height=10),
])
def test_map_view_rejects_invalid_geographic_or_canvas_bounds(args):
    with pytest.raises(ValueError):
        MapView(**args)


def test_project_rejects_invalid_coordinates_without_clamping_them():
    view = MapView(90, -90, -180, 180, 100, 50)
    assert project(91, 0, view) is None
    assert project(0, 181, view) is None
    assert project(float('nan'), 0, view) is None


def test_projection_supports_a_view_crossing_the_antimeridian():
    view = MapView(30, -30, 170, -170, 200, 100)
    assert project(0, 170, view) == (0, 50)
    assert project(0, 180, view) == (100, 50)
    assert project(0, -170, view) == (200, 50)
    assert project(0, 0, view) is None


def test_clustering_groups_normalized_city_country_and_rounded_coordinates():
    clusters = cluster_cities([
        station('b', '  San   Francisco ', 37.77491, -122.41939),
        station('a', 'san francisco', 37.77489, -122.41941),
        station('c', 'San Francisco', 37.784, -122.419),
        station('d', 'San Francisco', 37.7749, -122.4194, country='CA'),
    ])
    assert len(clusters) == 3
    sf = next(c for c in clusters if c.country_code == 'US' and c.latitude == pytest.approx(37.775))
    assert sf.city == 'San Francisco'
    assert sf.station_uuids == ('a', 'b')
    assert sf.station_count == 2
    assert clusters == cluster_cities(list(reversed([
        station('b', '  San   Francisco ', 37.77491, -122.41939),
        station('a', 'san francisco', 37.77489, -122.41941),
        station('c', 'San Francisco', 37.784, -122.419),
        station('d', 'San Francisco', 37.7749, -122.4194, country='CA'),
    ])))


def test_clustering_drops_invalid_coordinates_and_missing_station_ids():
    clusters = cluster_cities([
        station('bad-lat', 'City', 91, 0),
        station('bad-lon', 'City', 0, float('inf')),
        station('bad-nan', 'City', float('nan'), 0),
        station('bad-text', 'City', '10', 0),
        station('', 'City', 0, 0),
        station('valid', 'City', 0, 0),
    ])
    assert len(clusters) == 1
    assert clusters[0].station_uuids == ('valid',)


def test_visible_cities_caps_at_60_and_prioritizes_selected_then_center_then_count():
    view = MapView(90, -90, -180, 180, 1000, 500)
    stations = [station('center-small', 'Center small', 0, 0)]
    stations += [station('near-large-%d' % n, 'Near large', 2, 2) for n in range(5)]
    stations += [station('selected', 'Far selected', 0, 170)]
    stations += [station('far', 'Far', 0, 160)]
    stations += [station('pin-%d' % n, 'Pin %d' % n, 0, -170 + n * .1)
                 for n in range(70)]
    cities = cluster_cities(stations)
    selected = next(c.city_id for c in cities if c.city == 'Far selected')

    visible = visible_cities(cities, view, selected_city=selected, limit=200)
    assert len(visible) == 60
    assert visible[0].city_id == selected
    assert visible[1].city == 'Center small'
    assert visible[2].city == 'Near large'
    assert [c.city_id for c in visible] == [c.city_id for c in visible_cities(cities, view, selected_city=selected)]


def test_visible_cities_excludes_out_of_view_pins_and_handles_zero_limit():
    view = MapView(45, -45, -30, 30, 300, 200)
    cities = cluster_cities([station('inside', 'Inside', 0, 0), station('outside', 'Outside', 0, 40)])
    assert [c.city for c in visible_cities(cities, view)] == ['Inside']
    assert visible_cities(cities, view, limit=0) == []


def test_nearest_directional_city_uses_distance_then_stable_id_and_never_wraps():
    view = MapView(90, -90, -180, 180, 360, 180)
    cities = cluster_cities([
        station('origin', 'Origin', 0, 0),
        station('east-far', 'East far', 0, 40),
        station('east-near-z', 'East near z', 0, 10),
        station('east-near-a', 'East near a', 0, 10),
        station('north', 'North', 20, 0),
    ])
    origin = next(c.city_id for c in cities if c.city == 'Origin')
    near = sorted((c for c in cities if c.city.startswith('East near')), key=lambda c: c.city_id)[0]
    assert nearest_city_in_direction(cities, origin, 'right', view) == near
    assert nearest_city_in_direction(cities, origin, 'left', view) is None
    assert nearest_city_in_direction(cities, origin, 'down', view) is None
    assert nearest_city_in_direction(cities, origin, 'up', view).city == 'North'
    assert nearest_city_in_direction(cities, 'missing', 'right', view) is None


def test_directional_navigation_uses_45_degree_cone_and_accepts_boundaries():
    view = MapView(90, -90, -180, 180, 360, 180)
    cities = cluster_cities([
        station('origin', 'Origin', 0, 0),
        station('inside', 'Inside', 10, 10),
        station('outside', 'Outside', 20, 5),
    ])
    origin = next(c.city_id for c in cities if c.city == 'Origin')
    assert nearest_city_in_direction(cities, origin, 'right', view).city == 'Inside'


def test_directional_navigation_rejects_unknown_directions_and_does_not_wrap_view_edges():
    view = MapView(90, -90, -180, 180, 360, 180)
    cities = cluster_cities([station('edge', 'Edge', 0, 179), station('far-west', 'West', 0, -179)])
    edge = next(c.city_id for c in cities if c.city == 'Edge')
    assert nearest_city_in_direction(cities, edge, 'right', view) is None
    with pytest.raises(ValueError):
        nearest_city_in_direction(cities, edge, 'diagonal', view)
