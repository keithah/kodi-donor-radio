# SPDX-License-Identifier: GPL-2.0-or-later
import json
import os

import pytest

from icy_marker_store import IcyMarkerStore


def test_versioned_private_persistence_and_exact_station_matching(tmp_path):
    store = IcyMarkerStore(tmp_path)
    store.learn('one', '  commercial  ', 'end')
    assert store.lookup('one', 'COMMERCIAL') == 'end'
    assert store.lookup('two', 'COMMERCIAL') is None
    assert store.lookup('one', 'COMMERCIAL BREAK') is None
    restored = IcyMarkerStore(tmp_path)
    assert restored.lookup('one', 'commercial') == 'end'
    path = tmp_path / 'icy-markers.json'
    assert json.loads(path.read_text()) == {
        'version': 1, 'stations': {'one': {'COMMERCIAL': 'end'}}}
    assert path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_atomic_failure_preserves_disk_and_memory(tmp_path, monkeypatch):
    store = IcyMarkerStore(tmp_path)
    store.learn('one', '-', 'start')
    previous = (tmp_path / 'icy-markers.json').read_bytes()
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.learn('one', '-', 'end')
    assert store.lookup('one', '-') == 'start'
    assert (tmp_path / 'icy-markers.json').read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ['icy-markers.json']


@pytest.mark.parametrize('marker', ['', ' ', '\n-', '-\r', '\x00', '\x7f', '\u0085',
    'https://radio.example/ad', '//radio.example/ad', 'token=secret', 'PASSWORD:secret',
    'Bearer secret', 'Basic dXNlcjpwYXNz', 'access_token=secret', 'client_secret=secret',
    'user=listener', 'login:listener', 'mailto:listener@example.com', 'x' * 257, 'é' * 129])
def test_rejects_unsafe_or_oversized_values(tmp_path, marker):
    store = IcyMarkerStore(tmp_path)
    with pytest.raises(ValueError):
        store.learn('one', marker, 'start')
    assert not (tmp_path / 'icy-markers.json').exists()


@pytest.mark.parametrize('action', ['candidate', 'START', '', None, 1])
def test_rejects_invalid_actions(tmp_path, action):
    with pytest.raises(ValueError):
        IcyMarkerStore(tmp_path).learn('one', '-', action)


def test_marker_utf8_boundary_and_eight_marker_limit(tmp_path):
    store = IcyMarkerStore(tmp_path)
    store.learn('one', 'é' * 128, 'start')
    for index in range(7):
        store.learn('one', 'MARKER%d' % index, 'start')
    with pytest.raises(ValueError):
        store.learn('one', 'NINTH', 'end')
    store.learn('one', 'MARKER0', 'end')
    assert IcyMarkerStore(tmp_path).lookup('one', 'MARKER0') == 'end'


def test_256_station_limit_can_still_update_existing_station(tmp_path):
    store = IcyMarkerStore(tmp_path)
    for index in range(256):
        store.learn('station-%d' % index, '-', 'start')
    with pytest.raises(ValueError):
        store.learn('station-256', '-', 'end')
    store.learn('station-0', '-', 'end')
    assert store.lookup('station-0', '-') == 'end'


@pytest.mark.parametrize('data', [b'not json', b'{"version":2,"stations":{}}',
    b'{"version":1,"stations":{"one":{"token=secret":"start"}}}',
    b'{"version":1,"stations":{"one":{"-":"invalid"}}}', b'x' * (2 * 1024 * 1024 + 1)])
def test_invalid_or_oversized_profile_does_not_load_rules(tmp_path, data):
    (tmp_path / 'icy-markers.json').write_bytes(data)
    assert IcyMarkerStore(tmp_path).lookup('one', '-') is None


@pytest.mark.parametrize('data', [b'not json', b'{"version":2,"stations":{}}'])
def test_learn_refuses_to_overwrite_unreadable_profile(tmp_path, data):
    path = tmp_path / 'icy-markers.json'
    path.write_bytes(data)
    store = IcyMarkerStore(tmp_path)
    with pytest.raises(ValueError, match='unreadable'):
        store.learn('new-station', '-', 'start')
    assert path.read_bytes() == data
    assert store.lookup('new-station', '-') is None
