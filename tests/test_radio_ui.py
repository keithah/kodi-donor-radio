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
    station = Station('abc', 'Radio', 'https://secret.example/live?token=secret', 'MP3', False,
                      state='Ontario', click_count=23)
    summary = ui().StationSummary.from_station(station)
    assert summary.station_uuid == 'abc'
    assert summary.country == ''
    assert summary.state == 'Ontario'
    assert summary.click_count == 23
    assert not hasattr(summary, 'url')
    assert 'secret' not in json.dumps(summary.to_dict())
    assert ui().StationSummary.from_dict(summary.to_dict()) == summary
    assert ui().StationSummary.from_dict({'station_uuid': 'legacy', 'name': 'Legacy'}).state == ''


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


def test_store_uses_path_chmod_for_portable_private_file_mode(tmp_path, monkeypatch):
    m = ui()
    calls = []
    real_chmod = m.os.chmod
    def record_chmod(path, mode):
        calls.append((os.fspath(path), mode))
        return real_chmod(path, mode)
    monkeypatch.setattr(m.os, 'chmod', record_chmod)
    monkeypatch.setattr(m.os, 'fchmod', lambda *_args: pytest.fail('fchmod is not portable'), raising=False)

    m.FavoriteStore(tmp_path).add(m.StationSummary('portable', 'Portable'))

    assert any(os.path.basename(path).startswith('.favorites.json') and mode == 0o600
               for path, mode in calls)


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
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.favorites.json.lock', 'favorites.json']


@pytest.mark.parametrize('kind', ['favorite', 'place'])
def test_profile_mutations_wait_for_other_kodi_invocation_lock(tmp_path, kind):
    import threading
    from playback_arbiter import profile_file_lock
    module = ui()
    if kind == 'favorite':
        store, lock_name = module.FavoriteStore(tmp_path), '.favorites.json.lock'
        store.add(module.StationSummary('before', 'Before'))
        mutate = lambda: store.add(module.StationSummary('after', 'After'))
    else:
        store, lock_name = module.PlaceStore(tmp_path), '.places.json.lock'
        store.save('Before', 0, 0, 10)
        mutate = lambda: store.save('After', 1, 1, 10)
    entered = threading.Event()
    errors = []
    def update():
        entered.set()
        try:
            mutate()
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=update)
    with profile_file_lock(tmp_path / lock_name):
        worker.start()
        assert entered.wait(1)
        worker.join(.1)
        assert worker.is_alive(), 'a competing profile mutation must wait for the lock'
    worker.join(1)
    assert not worker.is_alive() and not errors
    assert len(store.list()) == 2


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


def test_huge_json_integer_is_rejected_as_invalid_numeric_value():
    with pytest.raises(ValueError, match='Invalid numeric value'):
        ui().coordinates(10 ** 1000, 0)


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
    assert [path.name for path in tmp_path.iterdir()] == ['.places.json.lock']
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


def test_recent_search_round_trip_schema_order_and_scope_deduplication(tmp_path):
    store = ui().RecentSearchStore(tmp_path)
    store.record('jazz', 'name', '2026-10-26T12:00:00Z')
    store.record('jazz', 'tag', '2026-10-26T13:00:00Z')
    store.record(' Jazz ', 'name', '2026-10-26T14:00:00Z')
    rows = ui().RecentSearchStore(tmp_path).list()
    assert [(r.query, r.scope, r.timestamp) for r in rows] == [
        (' Jazz ', 'name', '2026-10-26T14:00:00Z'), ('jazz', 'tag', '2026-10-26T13:00:00Z')]
    assert json.loads(store.path.read_text()) == {'schema_version': 1, 'items': [
        {'query': ' Jazz ', 'scope': 'name', 'timestamp': '2026-10-26T14:00:00Z'},
        {'query': 'jazz', 'scope': 'tag', 'timestamp': '2026-10-26T13:00:00Z'}]}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_recent_search_evicts_oldest_at_ten_and_sorts_actual_instants(tmp_path):
    store = ui().RecentSearchStore(tmp_path)
    for minute in range(11):
        store.record(str(minute), 'name', '2026-10-26T12:%02d:00Z' % minute)
    assert [r.query for r in store.list()] == ['10', '9', '8', '7', '6', '5', '4', '3', '2', '1']
    store.record('old', 'country', '2026-10-26T14:00:00+03:00')
    assert [r.query for r in store.list()] == ['10', '9', '8', '7', '6', '5', '4', '3', '2', '1']
    store.record('new', 'language', '2026-10-26T07:11:00-05:00')
    assert store.list()[0].query == 'new'


def test_history_counts_station_plays_orders_and_evicts_oldest_at_500(tmp_path):
    store = ui().HistoryStore(tmp_path)
    for number in range(501):
        store.record_play('station-%d' % number, '2026-10-26T12:%02d:%02dZ' % divmod(number, 60))
    rows = ui().HistoryStore(tmp_path).list()
    assert len(rows) == 500
    assert rows[0].station_uuid == 'station-500'
    assert rows[-1].station_uuid == 'station-1'
    store.record_play('station-1', '2026-10-26T13:00:00Z')
    first = store.list()[0]
    assert (first.station_uuid, first.play_count, first.last_played) == ('station-1', 2, '2026-10-26T13:00:00Z')
    assert json.loads(store.path.read_text())['items'][0] == {
        'station_uuid': 'station-1', 'play_count': 2, 'last_played': '2026-10-26T13:00:00Z'}
    assert json.loads(store.path.read_text())['schema_version'] == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_history_out_of_order_write_keeps_latest_utc_instant(tmp_path):
    store = ui().HistoryStore(tmp_path)
    store.record_play('station-1', '2026-10-26T13:00:00Z')

    store.record_play('station-1', '2026-10-26T14:00:00+03:00')

    entry = store.list()[0]
    assert entry.play_count == 2
    assert entry.last_played == '2026-10-26T13:00:00Z'

    store.record_play('station-1', '2026-10-26T17:00:00+03:00')
    entry = store.list()[0]
    assert entry.play_count == 3
    assert entry.last_played == '2026-10-26T17:00:00+03:00'


@pytest.mark.parametrize('query,scope,timestamp', [
    ('', 'name', '2026-10-26T12:00:00Z'), ('   ', 'name', '2026-10-26T12:00:00Z'),
    ('x'*257, 'name', '2026-10-26T12:00:00Z'), ('x\x7f', 'name', '2026-10-26T12:00:00Z'),
    ('x\n', 'name', '2026-10-26T12:00:00Z'), (None, 'name', '2026-10-26T12:00:00Z'),
    ('x', 'all', '2026-10-26T12:00:00Z'), ('x', 'name', ''),
    ('x', 'name', '2026-10-26T12:00:00'), ('x', 'name', 'yesterday')])
def test_recent_search_invalid_input_never_writes(tmp_path, query, scope, timestamp):
    store = ui().RecentSearchStore(tmp_path)
    with pytest.raises(ValueError):
        store.record(query, scope, timestamp)
    assert not store.path.exists()


@pytest.mark.parametrize('identifier,timestamp', [
    ('https://secret.example/live', '2026-10-26T12:00:00Z'), ('x'*129, '2026-10-26T12:00:00Z'),
    ('x', ''), ('x', '2026-10-26T12:00:00'), ('x', 'yesterday')])
def test_history_invalid_input_never_writes(tmp_path, identifier, timestamp):
    store = ui().HistoryStore(tmp_path)
    with pytest.raises(ValueError):
        store.record_play(identifier, timestamp)
    assert not store.path.exists()


@pytest.mark.parametrize('store_name,filename,valid', [
    ('RecentSearchStore', 'recent_searches.json', {'query': 'jazz', 'scope': 'name', 'timestamp': '2026-10-26T12:00:00Z'}),
    ('HistoryStore', 'history.json', {'station_uuid': 'a', 'play_count': 1, 'last_played': '2026-10-26T12:00:00Z'})])
@pytest.mark.parametrize('corruption', ['duplicate', 'unknown', 'version', 'oversized', 'record'])
def test_history_stores_reject_corrupt_files_without_rewriting(tmp_path, store_name, filename, valid, corruption):
    rows = [dict(valid)]
    document = {'schema_version': 1, 'items': rows}
    if corruption == 'duplicate': rows.append(dict(valid))
    elif corruption == 'unknown': rows[0]['url'] = 'secret'
    elif corruption == 'version': document['schema_version'] = True
    elif corruption == 'record': rows[0][('play_count' if store_name == 'HistoryStore' else 'query')] = True
    path = tmp_path / filename
    path.write_text(' ' * (2*1024*1024+1) if corruption == 'oversized' else json.dumps(document))
    before = path.read_bytes()
    store = getattr(ui(), store_name)(tmp_path)
    with pytest.raises(ValueError):
        store.list()
    with pytest.raises(ValueError):
        if store_name == 'HistoryStore': store.record_play('b', '2026-10-26T13:00:00Z')
        else: store.record('rock', 'name', '2026-10-26T13:00:00Z')
    assert path.read_bytes() == before


def test_history_orders_timezone_instants_and_station_id_ties(tmp_path):
    store = ui().HistoryStore(tmp_path)
    store.record_play('b', '2026-10-26T07:00:00-05:00')
    store.record_play('c', '2026-10-26T14:00:00+03:00')
    store.record_play('a', '2026-10-26T12:00:00Z')
    assert [r.station_uuid for r in store.list()] == ['a', 'b', 'c']


@pytest.mark.parametrize('count', [0, -1, True, 1.5, '1'])
def test_history_rejects_invalid_persisted_play_counts(tmp_path, count):
    path = tmp_path / 'history.json'
    path.write_text(json.dumps({'schema_version': 1, 'items': [
        {'station_uuid': 'a', 'play_count': count, 'last_played': '2026-10-26T12:00:00Z'}]}))
    with pytest.raises(ValueError):
        ui().HistoryStore(tmp_path).list()


def test_unrepresentable_utc_timestamp_is_invalid_profile_data(tmp_path):
    with pytest.raises(ValueError):
        ui().HistoryStore(tmp_path).record_play('a', '0001-01-01T00:00:00+01:00')
    assert not (tmp_path / 'history.json').exists()


@pytest.mark.parametrize('kind', ['recent', 'history'])
def test_new_profile_stores_serialize_mutations_and_preserve_file_on_atomic_failure(tmp_path, monkeypatch, kind):
    import threading
    from playback_arbiter import profile_file_lock
    if kind == 'recent':
        store = ui().RecentSearchStore(tmp_path)
        mutate = lambda: store.record('jazz', 'name', '2026-10-26T12:00:00Z')
    else:
        store = ui().HistoryStore(tmp_path)
        mutate = lambda: store.record_play('a', '2026-10-26T12:00:00Z')
    mutate()
    before = store.path.read_bytes()
    entered = threading.Event()
    errors = []
    def update():
        entered.set()
        try: mutate()
        except Exception as error: errors.append(error)
    worker = threading.Thread(target=update)
    with profile_file_lock(tmp_path / ('.' + store.filename + '.lock')):
        worker.start()
        assert entered.wait(1)
        worker.join(.1)
        assert worker.is_alive()
    worker.join(2)
    assert not worker.is_alive() and not errors
    if kind == 'history': assert store.list()[0].play_count == 2
    before = store.path.read_bytes()
    def fail(*args): raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError): mutate()
    assert store.path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.' + store.filename + '.lock', store.filename]


@pytest.mark.parametrize('kind,limit', [('recent', 10), ('history', 500)])
def test_new_profile_stores_reject_oversized_item_lists(tmp_path, kind, limit):
    if kind == 'recent':
        store = ui().RecentSearchStore(tmp_path)
        rows = [{'query': str(i), 'scope': 'name', 'timestamp': '2026-10-26T12:00:00Z'} for i in range(limit+1)]
    else:
        store = ui().HistoryStore(tmp_path)
        rows = [{'station_uuid': str(i), 'play_count': 1, 'last_played': '2026-10-26T12:00:00Z'} for i in range(limit+1)]
    store.path.write_text(json.dumps({'schema_version': 1, 'items': rows}))
    with pytest.raises(ValueError): store.list()


def test_directory_cache_round_trip_exact_expiry_and_immutable_stale_rows(tmp_path):
    from dataclasses import FrozenInstanceError
    now = [1000]
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: now[0])
    assert store.get('countries', 'all', 60) is None
    rows = [['France', 'FR']]
    store.put('countries', 'all', rows)
    rows[0][0] = 'Changed'
    page = store.get('countries', 'all', 60)
    assert (page.kind, page.rows, page.continuation, page.stored_at, page.fresh) == (
        'countries', (('France', 'FR'),), None, 1000, True)
    with pytest.raises(FrozenInstanceError): page.fresh = False
    now[0] = 1059
    assert store.get('countries', 'all', 60).fresh
    now[0] = 1060
    assert store.get('countries', 'all', 60) is None
    stale = store.get('countries', 'all', 60, allow_stale=True)
    assert stale.rows == (('France', 'FR'),) and not stale.fresh
    store.put('tags', 'all', ['jazz'], continuation=False)
    assert ui().DirectoryCacheStore(tmp_path, clock=lambda: 1060).get('tags', 'all', 60).rows == ('jazz',)


def test_directory_cache_safe_station_rows_hashed_keys_and_page_isolation(tmp_path):
    from models import Station
    m = ui()
    store = m.DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    station = Station('safe', 'Radio', 'https://secret.example/live?token=secret', 'MP3', False)
    summary = m.StationSummary.from_station(station)
    key = 'private-place-id:radius=25:offset=0'
    store.put('place', key, [summary], continuation=200)
    store.put('place', 'private-place-id:radius=50:offset=0', [])
    store.put('map', key, [])
    assert store.get('place', key, 60).rows == (summary,)
    assert store.get('place', key, 60).continuation == 200
    assert store.get('place', 'private-place-id:radius=50:offset=0', 60).rows == ()
    assert store.get('map', key, 60).rows == ()
    assert store.get('place', 'private-place-id:radius=25:offset=200', 60) is None
    raw = store.path.read_text()
    assert 'secret' not in raw and key not in raw and 'private-place-id' not in raw
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    with pytest.raises(ValueError): store.put('place', 'raw', [station])


def test_directory_cache_never_persists_favicon_urls(tmp_path):
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    summary = ui().StationSummary('station', 'Station',
                                  favicon='https://radio.example/live?token=secret')

    store.put('place', 'place:radius=25', [summary])

    raw = store.path.read_text()
    assert 'radio.example' not in raw
    assert 'token=secret' not in raw
    assert store.get('place', 'place:radius=25', 60).rows[0].favicon == ''


@pytest.mark.parametrize('kind,rows,continuation,stored_at', [
    ('search', [], None, 1000), ('countries', [('France', 'fr')], None, 1000),
    ('countries', [('France', 'FRA')], None, 1000), ('countries', [('France', 'FR', 'x')], None, 1000),
    ('countries', [('', 'FR')], None, 1000), ('tags', ['x'*257], None, 1000),
    ('tags', ['bad\n'], None, 1000), ('tags', [''], None, 1000),
    ('tags', [True], None, 1000), ('map', [], 0, 1000), ('place', [], True, 1000),
    ('tags', [], None, 1000), ('tags', [], 0, 1000), ('tags', [], 1, 1000),
    ('countries', [], False, 1000),
    ('place', [], -1, 1000), ('place', [], 1.5, 1000), ('place', [], None, 1001),
    ('place', [], None, float('nan')), ('place', [], None, True), ('place', [], None, -1),
])
def test_directory_cache_invalid_values_never_write(tmp_path, kind, rows, continuation, stored_at):
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    with pytest.raises(ValueError):
        store.put(kind, 'key', rows, continuation=continuation, stored_at=stored_at)
    assert not store.path.exists()


@pytest.mark.parametrize('corruption', ['duplicate-key', 'duplicate-entry', 'version', 'extra',
                                       'unsafe-row', 'future', 'kind', 'hash', 'too-many', 'oversized', 'json'])
def test_directory_cache_rejects_corruption_without_rewriting(tmp_path, corruption):
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    store.put('place', 'key', [ui().StationSummary('a', 'A')])
    document = json.loads(store.path.read_text())
    if corruption == 'duplicate-entry': document['items'].append(document['items'][0])
    elif corruption == 'version': document['schema_version'] = True
    elif corruption == 'extra': document['unexpected'] = 1
    elif corruption == 'unsafe-row': document['items'][0]['rows'][0]['url'] = 'secret'
    elif corruption == 'future': document['items'][0]['stored_at'] = 1001
    elif corruption == 'kind': document['items'][0]['kind'] = 'search'
    elif corruption == 'hash': document['items'][0]['key_hash'] = 'raw-key'
    elif corruption == 'too-many': document['items'] *= 101
    raw = json.dumps(document)
    if corruption == 'duplicate-key': raw = raw.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
    elif corruption == 'oversized': raw = ' ' * (2*1024*1024+1)
    elif corruption == 'json': raw = '{'
    store.path.write_text(raw)
    before = store.path.read_bytes()
    with pytest.raises(ValueError): store.get('place', 'key', 60, allow_stale=True)
    with pytest.raises(ValueError): store.put('tags', 'new', ['jazz'])
    assert store.path.read_bytes() == before


def test_directory_cache_bounds_evict_oldest_and_preserve_file_on_oversized_write(tmp_path):
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    for number in range(101):
        store.put('tags', str(number), ['tag'], continuation=False, stored_at=number)
    assert store.get('tags', '0', 2000) is None
    assert store.get('tags', '1', 2000).rows == ('tag',)
    assert len(json.loads(store.path.read_text())['items']) == 100
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.put('place', 'too-many', [ui().StationSummary(str(i), 'A') for i in range(201)])
    with pytest.raises(ValueError): store.put('tags', 'too-many', ['jazz']*51)
    with pytest.raises(ValueError): store.put('countries', 'too-many', [('France', 'FR')]*301)
    assert store.path.read_bytes() == before
    store.put('place', 'full-page', [ui().StationSummary(str(i), 'A') for i in range(200)])
    assert len(store.get('place', 'full-page', 60).rows) == 200


def test_directory_cache_mutation_waits_for_lock_and_atomic_failure_preserves_data(tmp_path, monkeypatch):
    import threading
    from playback_arbiter import profile_file_lock
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    store.put('tags', 'before', ['jazz'], continuation=False)
    started = threading.Event()
    errors = []
    def update():
        started.set()
        try: store.put('tags', 'after', ['rock'], continuation=False)
        except Exception as error: errors.append(error)
    worker = threading.Thread(target=update)
    with profile_file_lock(tmp_path / '.directory_cache.json.lock'):
        worker.start()
        assert started.wait(1)
        worker.join(.1)
        assert worker.is_alive()
    worker.join(2)
    assert not worker.is_alive() and not errors
    assert store.get('tags', 'before', 60).rows == ('jazz',)
    assert store.get('tags', 'after', 60).rows == ('rock',)
    before = store.path.read_bytes()
    def fail(*args): raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError): store.put('tags', 'failed', ['pop'], continuation=False)
    assert store.path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.directory_cache.json.lock', 'directory_cache.json']


def test_directory_cache_file_byte_bound_preserves_last_valid_file(tmp_path):
    store = ui().DirectoryCacheStore(tmp_path, clock=lambda: 1000)
    summary = ui().StationSummary('s'*128, '𝄞'*512, city='𝄞'*256, state='𝄞'*256,
                                  country='𝄞'*256, codec='𝄞'*64, tags='𝄞'*1024)
    store.put('map', 'first', [summary]*200)
    before = store.path.read_bytes()
    assert len(before) <= 2*1024*1024
    with pytest.raises(ValueError): store.put('map', 'second', [summary]*200)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('has_more', [True, False])
def test_directory_cache_tags_preserve_raw_page_continuation(tmp_path, has_more):
    from radio_ui import DirectoryCacheStore
    store = DirectoryCacheStore(tmp_path)
    store.put('tags', '0', ['jazz'], continuation=has_more)
    assert store.get('tags', '0', 60).continuation is has_more


def test_favorite_refresh_updates_metadata_preserving_order_played_and_missing(tmp_path):
    from dataclasses import replace
    m = ui()
    store = m.FavoriteStore(tmp_path)
    first = m.StationSummary('a', 'First', last_played='2026-10-23T12:00:00Z')
    missing = m.StationSummary('b', 'Missing', city='Old city')
    store.add(first)
    store.add(missing)
    updated = replace(first, name='Updated', city='New city', country='Canada',
                      country_code='CA', codec='AAC', tags='jazz', bitrate=128,
                      latitude=1, longitude=2, favicon='https://radio.example/logo.png',
                      last_played='2026-10-24T12:00:00Z')
    store.refresh([m.StationSummary('unknown', 'Never add'), updated])
    assert store.list() == [replace(updated, favicon='', last_played=first.last_played), missing]
    assert '"url"' not in store.path.read_text()
    assert 'radio.example/logo.png' not in store.path.read_text()


def test_favorite_refresh_invalid_later_summary_leaves_file_unchanged(tmp_path):
    from models import Station
    m = ui()
    store = m.FavoriteStore(tmp_path)
    store.add(m.StationSummary('a', 'First'))
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.refresh([m.StationSummary('a', 'Updated'),
                       Station('b', 'Unsafe', 'https://secret.example/live', '', False)])
    assert store.path.read_bytes() == before


def test_favorite_refresh_atomic_write_failure_preserves_file(tmp_path, monkeypatch):
    m = ui()
    store = m.FavoriteStore(tmp_path)
    store.add(m.StationSummary('a', 'First'))
    before = store.path.read_bytes()
    def fail(*args): raise OSError('disk unavailable')
    monkeypatch.setattr(m.os, 'replace', fail)
    with pytest.raises(OSError):
        store.refresh([m.StationSummary('a', 'Updated')])
    assert store.path.read_bytes() == before
