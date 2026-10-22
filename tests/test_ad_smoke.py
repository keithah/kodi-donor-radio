# SPDX-License-Identifier: GPL-2.0-or-later
"""Acceptance checks must reject a plausible but incorrect playback transcript."""
import pytest

from tools.smoke_ad_relay import check_trace
from tools import smoke_ad_kodi


def valid():
    return {
        'sections': ['preroll', 'programme_a', 'programme_a', 'replacement',
                     'replacement', 'programme_b', 'programme_b'],
        'expected_sections': ['preroll', 'programme_a', 'programme_a', 'replacement',
                              'replacement', 'programme_b', 'programme_b'],
        'samples': [96256] * 7,
        'expected_samples': [96256] * 7,
        'events': [],
        'stopped_at': None,
    }


def test_valid_unmapped_trace_has_no_success_beacons():
    assert check_trace(valid()) == []


def test_original_break_leakage_fails():
    trace = valid(); trace['sections'][3] = 'original_break'
    assert any('section' in item for item in check_trace(trace))


def test_missing_programme_fails():
    trace = valid(); trace['sections'].pop()
    assert any('segment count' in item for item in check_trace(trace))


def test_duplicate_event_fails():
    trace = valid(); trace['events'] = [
        {'event': 'impression', 'placement_id': 'a', 'at': 1.0},
        {'event': 'impression', 'placement_id': 'a', 'at': 1.1},
    ]
    assert any('duplicate' in item for item in check_trace(trace))


def test_completion_after_stop_fails():
    trace = valid(); trace['stopped_at'] = 2.0
    trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 2.5}]
    assert any('after stop' in item for item in check_trace(trace))


def test_unmapped_success_beacon_fails():
    trace = valid(); trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 1.5}]
    assert any('playback evidence' in item for item in check_trace(trace))


def test_claimed_mapping_cannot_make_relay_only_success_pass():
    trace = valid(); trace['mapping_proven'] = True
    trace['events'] = [{'event': 'complete', 'placement_id': 'a', 'at': 1.5}]
    assert any('playback evidence' in item for item in check_trace(trace))


def test_kodi_smoke_rejects_unrelated_item_even_with_advancing_clock():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': False, 'time_seconds': 1.0},
        {'playing': True, 'item_matches_expected': False, 'time_seconds': 3.0}]}
    assert any('item' in item for item in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_rejects_frozen_clock_on_expected_item():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 1.0}]}
    assert any('advance' in item for item in smoke_ad_kodi.validate_observation(result))


def test_kodi_smoke_accepts_expected_item_with_advancing_clock():
    result = {'source': 'normal', 'timeline': [
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.0},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.4},
        {'playing': True, 'item_matches_expected': True, 'time_seconds': 0.8}]}
    assert smoke_ad_kodi.validate_observation(result) == []


def test_kodi_rpc_ignores_async_notification_before_matching_response(monkeypatch):
    class Socket:
        def __init__(self):
            self.parts = [b'{"jsonrpc":"2.0","method":"Player.OnPause","params":{}}\n'
                          b'{"jsonrpc":"2.0","id":1,"result":{"Player.Caching":true}}\n']
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, *_): pass
        def sendall(self, *_): pass
        def recv(self, *_): return self.parts.pop(0) if self.parts else b''
    monkeypatch.setattr(smoke_ad_kodi.socket, 'create_connection', lambda *_args, **_kwargs: Socket())
    assert smoke_ad_kodi.rpc('XBMC.GetInfoBooleans') == {'Player.Caching': True}
