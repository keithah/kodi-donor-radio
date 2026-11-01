# SPDX-License-Identifier: GPL-2.0-or-later
"""Fixture tracking observations expose a bounded, declared monotonic clock."""
from contextlib import contextmanager
import ctypes
import json
from types import SimpleNamespace
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from tools import ad_fixture_server


@contextmanager
def running_fixture(assets):
    server = ad_fixture_server.make_server(assets, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = 'http://127.0.0.1:%d' % server.server_port
    def get(path):
        with urlopen(origin + path, timeout=3) as response:
            return response.read()
    try:
        yield get
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_tracking_timestamps_are_declared_and_reset_without_resetting_the_clock(monkeypatch, tmp_path):
    ticks = iter((100000001, 200000002, 300000003))
    clock = {'api': 'controlled_test_clock', 'domain': 'test'}
    monkeypatch.setattr(ad_fixture_server, 'make_tracking_clock',
                        lambda: (lambda: next(ticks), clock), raising=False)
    with running_fixture(tmp_path) as get:
        before = json.loads(get('/stats'))
        assert before.get('tracking_observations') == []
        clock = before['tracking_clock']
        assert clock == {'api': 'controlled_test_clock', 'domain': 'test'}
        get('/source/finite/index.m3u8')
        get('/track/placement-1/start?ignored=not-recorded')
        get('/track/placement-1/firstQuartile')
        with pytest.raises(HTTPError) as rejected:
            get('/track/placement-1/unknown-event')
        assert rejected.value.code == 404
        observed = json.loads(get('/stats'))
        assert observed['tracking_observations'] == [
            {'event': 'start', 'monotonic_ns': 100000001},
            {'event': 'firstQuartile', 'monotonic_ns': 200000002}]
        assert observed['beacons'] == {'placement-1/start': 1, 'placement-1/firstQuartile': 1}
        get('/reset')
        cleared = json.loads(get('/stats'))
        assert cleared['tracking_observations'] == []
        assert cleared['beacons'] == {} and cleared['requests'] == {}
        assert cleared['tracking_clock'] == clock
        get('/track/placement-1/complete')
        assert json.loads(get('/stats'))['tracking_observations'] == [
            {'event': 'complete', 'monotonic_ns': 300000003}]


def test_tracking_history_keeps_only_latest_256_while_counts_remain_complete(monkeypatch, tmp_path):
    ticks = iter(range(1000, 1300))
    clock = {'api': 'controlled_test_clock', 'domain': 'test'}
    monkeypatch.setattr(ad_fixture_server, 'make_tracking_clock',
                        lambda: (lambda: next(ticks), clock), raising=False)
    with running_fixture(tmp_path) as get:
        for _ in range(260):
            get('/track/placement-1/start')
        stats = json.loads(get('/stats'))
        records = stats.get('tracking_observations')
        assert records is not None
        assert len(records) == 256
        assert records[0] == {'event': 'start', 'monotonic_ns': 1004}
        assert records[-1] == {'event': 'start', 'monotonic_ns': 1259}
        assert stats['beacons'] == {'placement-1/start': 260}
        assert stats['requests']['/track/placement-1/start'] == 260


class NativeFunction:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def fake_mach_library(ticks, numer=125, denom=3, status=0):
    def timebase(pointer):
        values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
        values[0], values[1] = numer, denom
        return status
    return SimpleNamespace(
        mach_absolute_time=NativeFunction(lambda: ticks),
        mach_timebase_info=NativeFunction(timebase))


def test_darwin_uses_unshifted_mach_ticks_with_integer_nanosecond_conversion(monkeypatch):
    ticks = 2 ** 63 + 17
    native = fake_mach_library(ticks)
    monkeypatch.setattr(ad_fixture_server.sys, 'platform', 'darwin')
    monkeypatch.setattr(ad_fixture_server.ctypes, 'CDLL', lambda path: native)
    # Simulate old Python's process-relative monotonic value. It must not be used
    # for records advertised as Core Audio's cross-process Mach clock domain.
    monkeypatch.setattr(ad_fixture_server, 'monotonic_ns', lambda: 123)
    read_clock, metadata = ad_fixture_server.make_tracking_clock()
    assert read_clock() == ticks * 125 // 3
    assert metadata['api'] == 'mach_absolute_time'
    assert metadata['domain'] == 'mach_absolute_time'
    assert metadata['cross_process_comparable'] is True
    assert metadata['timebase_numer'] == 125
    assert metadata['timebase_denom'] == 3
    assert metadata['resolution_seconds'] == pytest.approx(125 / 3 / 1_000_000_000)
    assert metadata['python_monotonic']['implementation'] == time.get_clock_info('monotonic').implementation
    assert native.mach_absolute_time.restype is ctypes.c_uint64


@pytest.mark.parametrize(('status', 'numer', 'denom'), [(1, 125, 3), (0, 0, 3), (0, 125, 0)])
def test_darwin_invalid_timebase_fails_instead_of_mislabeling_python_clock(monkeypatch, status, numer, denom):
    monkeypatch.setattr(ad_fixture_server.sys, 'platform', 'darwin')
    native = fake_mach_library(100, numer=numer, denom=denom, status=status)
    monkeypatch.setattr(ad_fixture_server.ctypes, 'CDLL', lambda path: native)
    with pytest.raises(RuntimeError, match='Mach timebase'):
        ad_fixture_server.make_tracking_clock()


def test_other_platform_clock_is_not_advertised_as_mach_comparable(monkeypatch):
    monkeypatch.setattr(ad_fixture_server.sys, 'platform', 'linux')
    monkeypatch.setattr(ad_fixture_server, 'monotonic_ns', lambda: 1234)
    read_clock, metadata = ad_fixture_server.make_tracking_clock()
    assert read_clock() == 1234
    assert metadata['api'] == 'time.monotonic_ns'
    assert metadata['domain'] == 'python_monotonic'
    assert metadata['cross_process_comparable'] is False
