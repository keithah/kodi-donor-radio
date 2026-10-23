# SPDX-License-Identifier: GPL-2.0-or-later
import importlib
import json
import os
import stat

import pytest


def ui():
    return importlib.import_module('radio_ui')


def test_summary_round_trip_is_safe_and_legacy_station_compatible():
    from models import Station
    station = Station('abc', 'Radio', 'https://secret.example/live?token=secret', 'MP3', False)
    summary = ui().StationSummary.from_station(station)
    assert summary.station_uuid == 'abc'
    assert summary.country == ''
    assert not hasattr(summary, 'url')
    assert 'secret' not in json.dumps(summary.to_dict())
    assert ui().StationSummary.from_dict(summary.to_dict()) == summary


def test_favorites_preserve_order_update_played_and_round_trip(tmp_path):
    m = ui()
    store = m.FavoriteStore(tmp_path)
    store.add(m.StationSummary('a', 'First'))
    store.add(m.StationSummary('b', 'Second'))
    store.add(m.StationSummary('a', 'Renamed'))
    assert [s.station_uuid for s in store.list()] == ['a', 'b']
    store.move('b', 0)
    store.mark_played('a', '2026-10-23T12:00:00Z')
    loaded = m.FavoriteStore(tmp_path).list()
    assert [s.station_uuid for s in loaded] == ['b', 'a']
    assert loaded[1].last_played == '2026-10-23T12:00:00Z'
    store.remove('b')
    assert [s.station_uuid for s in store.list()] == ['a']
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'favorites.json').stat().st_mode) == 0o600


def test_places_have_single_default_and_stable_identity(tmp_path):
    store = ui().PlaceStore(tmp_path)
    first = store.save('Home', 1, 2, 25, make_default=True)
    second = store.save('Work', 3, 4, 50, make_default=True)
    assert [p.is_default for p in store.list()] == [False, True]
    store.set_default(first.place_id)
    store.set_radius(first.place_id, 100)
    assert store.list()[0].radius_km == 100
    assert sum(p.is_default for p in store.list()) == 1
    store.remove(second.place_id)
    assert store.list()[0].place_id == first.place_id


@pytest.mark.parametrize('filename,document', [
    ('favorites.json', {'schema_version': 2, 'items': []}),
    ('favorites.json', {'schema_version': 1, 'items': [{'station_uuid': 'a', 'name': 'A', 'url': 'secret'}]}),
    ('places.json', {'schema_version': 1, 'items': [] , 'extra': 1}),
    ('places.json', {'schema_version': 1, 'items': 'bad'}),
])
def test_stores_reject_unknown_schema_and_unsafe_fields(tmp_path, filename, document):
    (tmp_path / filename).write_text(json.dumps(document))
    store = ui().FavoriteStore(tmp_path) if filename == 'favorites.json' else ui().PlaceStore(tmp_path)
    with pytest.raises(ValueError):
        store.list()


def test_atomic_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    store = ui().FavoriteStore(tmp_path)
    store.add(ui().StationSummary('a', 'A'))
    previous = (tmp_path / 'favorites.json').read_bytes()
    def fail(source, destination):
        assert os.path.dirname(source) == str(tmp_path)
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.add(ui().StationSummary('b', 'B'))
    assert (tmp_path / 'favorites.json').read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ['favorites.json']


def test_store_limits_and_safe_string_bounds(tmp_path, monkeypatch):
    m = ui()
    monkeypatch.setattr(m, 'MAX_FAVORITES', 2)
    store = m.FavoriteStore(tmp_path)
    store.add(m.StationSummary('a', 'A'))
    store.add(m.StationSummary('b', 'B'))
    with pytest.raises(ValueError):
        store.add(m.StationSummary('c', 'C'))
    with pytest.raises(ValueError):
        m.StationSummary('a' * 129, 'A')
    with pytest.raises(ValueError):
        m.StationSummary('a', 'A' * 513)
    monkeypatch.setattr(m, 'MAX_PLACES', 1)
    places = m.PlaceStore(tmp_path)
    places.save('Home', 0, 0, 25)
    with pytest.raises(ValueError):
        places.save('Work', 0, 0, 25)


@pytest.mark.parametrize('lat,lon,radius', [(91, 0, 1), (0, -181, 1), (float('nan'), 0, 1), (True, 0, 1), (0, 0, 0), (0, 0, float('inf'))])
def test_invalid_places_never_write(tmp_path, lat, lon, radius):
    with pytest.raises(ValueError):
        ui().PlaceStore(tmp_path).save('Bad', lat, lon, radius)
    assert not (tmp_path / 'places.json').exists()


def test_radius_edges_missing_coordinates_and_antimeridian(tmp_path):
    from models import Station
    m = ui()
    place = m.PlaceStore(tmp_path).save('Date line', 0, 179.9, 25)
    stations = [Station('b', 'B', 'https://x', '', False, latitude=0, longitude=-179.9),
                Station('a', 'A', 'https://x', '', False, latitude=0, longitude=179.9),
                Station('c', 'C', 'https://x', '', False)]
    assert m.distance_km(0, 179.9, 0, -179.9) == pytest.approx(22.239, abs=.001)
    assert [s.uuid for s in m.stations_in_radius(stations, place)] == ['a', 'b']
    assert [s.uuid for s in m.stations_in_radius(stations, place, 1)] == ['a']
    distance = m.distance_km(0, 179.9, 0, -179.9)
    assert [s.uuid for s in m.stations_in_radius(stations, place, distance)] == ['a', 'b']
    with pytest.raises(ValueError):
        m.distance_km(91, 0, 0, 0)


def test_duplicate_json_keys_are_rejected(tmp_path):
    (tmp_path / 'favorites.json').write_text('{"schema_version":2,"schema_version":1,"items":[]}')
    with pytest.raises(ValueError):
        ui().FavoriteStore(tmp_path).list()


def test_equal_distance_stations_sort_by_name_then_identifier(tmp_path):
    from models import Station
    m = ui()
    place = m.PlaceStore(tmp_path).save('Home', 0, 0, 10)
    stations = [Station(i, name, 'https://example.com', '', False, latitude=0, longitude=0)
                for i, name in [('z', 'B'), ('b', 'A'), ('a', 'A')]]
    assert [s.uuid for s in m.stations_in_radius(stations, place)] == ['a', 'b', 'z']


@pytest.mark.parametrize('document', [
    {'schema_version': 1, 'items': [{'station_uuid': 'a', 'name': 'A'}, {'station_uuid': 'a', 'name': 'B'}]},
    {'schema_version': True, 'items': []},
    {'schema_version': 1, 'items': [{'station_uuid': 'a', 'name': 'A', 'latitude': 90}]},
])
def test_corrupt_favorites_are_not_silently_overwritten(tmp_path, document):
    path = tmp_path / 'favorites.json'
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ui().FavoriteStore(tmp_path).add(ui().StationSummary('b', 'B'))
    assert path.read_bytes() == before


def test_favorite_actions_validate_positions_timestamps_and_record_type(tmp_path):
    from models import Station
    m = ui()
    store = m.FavoriteStore(tmp_path)
    with pytest.raises(ValueError):
        store.add(Station('a', 'A', 'https://secret.example', '', False))
    store.add(m.StationSummary('a', 'A'))
    for index in (-1, 1, True):
        with pytest.raises(ValueError):
            store.move('a', index)
    with pytest.raises(ValueError):
        store.mark_played('a', 'yesterday')
    with pytest.raises(ValueError):
        store.mark_played('missing', '2026-10-23T12:00:00Z')
    store.mark_played('a', '2026-10-23T12:00:00Z')
    store.add(m.StationSummary('a', 'Updated'))
    assert store.list()[0].last_played == '2026-10-23T12:00:00Z'


def test_place_country_code_round_trip_and_old_schema_migration(tmp_path):
    store = ui().PlaceStore(tmp_path)
    place = store.save('Paris', 48.8, 2.3, 25, country_code='FR')
    assert store.list()[0].country_code == 'FR'
    path = tmp_path / 'places.json'
    data = json.loads(path.read_text())
    del data['items'][0]['country_code']
    path.write_text(json.dumps(data))
    assert store.list()[0].country_code == ''
    store.set_radius(place.place_id, 50)
    assert json.loads(path.read_text())['items'][0]['country_code'] == ''
    assert store.save('Legacy call', 0, 0, 1).country_code == ''


@pytest.mark.parametrize('code', ['F', 'FRA', '12', 'ßß', 'fr', None, True])
def test_place_country_code_validation_never_writes_invalid_values(tmp_path, code):
    with pytest.raises(ValueError):
        ui().PlaceStore(tmp_path).save('Bad', 0, 0, 25, country_code=code)
    assert not (tmp_path / 'places.json').exists()


def test_setup_batch_validates_all_places_before_single_atomic_write(tmp_path, monkeypatch):
    store = ui().PlaceStore(tmp_path)
    existing = store.save('Existing', 1, 2, 25, make_default=True)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save_many([dict(name='Paris', latitude=48.8, longitude=2.3, radius_km=25, country_code='FR'),
                         dict(name='Bad', latitude=None, longitude=2, radius_km=25)])
    assert store.path.read_bytes() == before
    calls = []
    real_replace = os.replace
    def replace(source, destination):
        calls.append(destination)
        real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', replace)
    store.save_many([dict(name='Paris', latitude=48.8, longitude=2.3, radius_km=25, country_code='FR'),
                     dict(name='Tokyo', latitude=35.7, longitude=139.7, radius_km=50, country_code='JP')])
    assert len(calls) == 1
    places = store.list()
    assert [p.name for p in places] == ['Existing', 'Paris', 'Tokyo']
    assert [p.is_default for p in places] == [True, False, False]
    assert places[0].place_id == existing.place_id


def test_setup_batch_default_limit_and_disk_failure_leave_no_partial_places(tmp_path, monkeypatch):
    m = ui()
    store = m.PlaceStore(tmp_path)
    values = [dict(name='Paris', latitude=48.8, longitude=2.3, radius_km=25, country_code='FR'),
              dict(name='Tokyo', latitude=35.7, longitude=139.7, radius_km=50, country_code='JP')]
    monkeypatch.setattr(m, 'MAX_PLACES', 1)
    with pytest.raises(ValueError): store.save_many(values)
    assert not store.path.exists()
    monkeypatch.setattr(m, 'MAX_PLACES', 50)
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError): store.save_many(values)
    assert not store.path.exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    store.save_many(values)
    assert [p.is_default for p in store.list()] == [True, False]


def test_removing_default_place_selects_first_remaining_in_stored_order(tmp_path):
    store = ui().PlaceStore(tmp_path)
    first = store.save('Zulu', 0, 0, 10)
    middle = store.save('Default', 0, 0, 10, make_default=True)
    last = store.save('Alpha', 0, 0, 10)
    store.remove(middle.place_id)
    assert [(p.place_id, p.is_default) for p in store.list()] == [(first.place_id, True), (last.place_id, False)]
    store.remove(first.place_id)
    assert store.list()[0].is_default
    store.remove(last.place_id)
    assert store.list() == []
