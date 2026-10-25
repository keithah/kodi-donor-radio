# SPDX-License-Identifier: GPL-2.0-or-later
"""Controlled VAST resolution through the real local HTTP transport."""
from contextlib import contextmanager
import json
import threading
from urllib.request import urlopen

import pytest


@contextmanager
def origin_server(tmp_path):
    from tools.ad_fixture_server import make_server
    from http_client import HttpClient
    from url_policy import UrlPolicy
    server = make_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_port
    try:
        yield base, HttpClient(UrlPolicy(test_origins={base}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def beacons(base):
    with urlopen(base + '/stats', timeout=5) as response:
        return json.load(response)['beacons']


def assert_ad_error(error, code, reason):
    assert (error.value.code, error.value.reason) == (code, reason)


def test_wrapper_tracking_inherited_once(tmp_path):
    from vast import VastResolver
    from ad_models import TrackingUrl
    with origin_server(tmp_path) as (base, client):
        ad = VastResolver(client, base).resolve(base + '/vast/wrapped', threading.Event())
        assert ad.id == 'placement-1'
        assert ad.media_url == base + '/ads/replacement.m3u8'
        assert ad.duration_seconds == 4
        assert [item.event for item in ad.tracking].count('start') == 1
        assert [item.event for item in ad.tracking].count('impression') == 1
        assert len(ad.error_urls) == 1
        assert TrackingUrl('firstQuartile', base + '/track/placement-1/firstQuartile?layer=wrapper') in ad.tracking
        assert beacons(base) == {}


def test_five_wrapper_hops_allowed_six_rejected(tmp_path):
    from vast import VastResolver
    from ad_models import AdError
    with origin_server(tmp_path) as (base, client):
        resolver = VastResolver(client, base)
        assert resolver.resolve(base + '/vast/wrapper-depth-2', threading.Event()).id == 'placement-1'
        with pytest.raises(AdError) as error:
            resolver.resolve(base + '/vast/wrapper-depth-1', threading.Event())
        assert_ad_error(error, 302, 'Too many VAST wrappers')
        with pytest.raises(AdError) as error:
            resolver.resolve(base + '/vast/wrapper-depth', threading.Event())
        assert_ad_error(error, 302, 'Too many VAST wrappers')
        assert beacons(base) == {}


def test_cycle_no_fill_bad_xml_and_external_entity(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        resolver = VastResolver(client, base)
        assert resolver.resolve(base + '/vast/no-fill', threading.Event()) is None
        for scenario, expected in (('wrapper-cycle', (302, 'VAST wrapper cycle')),
                                   ('invalid-xml', (100, 'Invalid VAST XML'))):
            with pytest.raises(AdError) as error:
                resolver.resolve(base + '/vast/' + scenario, threading.Event())
            assert_ad_error(error, *expected)
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
                            b'<!DOCTYPE VAST [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                            b'<VAST><Ad><InLine><AdTitle>&x;</AdTitle></InLine></Ad></VAST>'
                            if scenario == 'direct' else original(scenario, origin))
        with pytest.raises(AdError) as error:
            resolver.resolve(base + '/vast/direct', threading.Event())
        assert_ad_error(error, 100, 'Invalid VAST XML')
        assert beacons(base) == {}


@pytest.mark.parametrize('body', [
    ('<?xml version="1.0" encoding="UTF-16"?>'
     '<!DOCTYPE VAST [<!ENTITY x "expanded">]>'
     '<VAST version="4.2"><Ad>&x;</Ad></VAST>').encode('utf-16'),
    b'\xff\xfe<not-utf8-vast',
])
def test_vast_rejects_non_utf8_and_utf16_dtd_entities(body):
    from vast import VastResolver
    from ad_models import AdError
    resolver = VastResolver(None, 'http://127.0.0.1:8766')
    with pytest.raises(AdError) as error:
        resolver._parse(body)
    assert_ad_error(error, 100, 'Invalid VAST XML')


def test_ad_origin_wrapper_escape_rejected(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from http_client import HttpClient
    from url_policy import UrlPolicy
    escaped_requests = []
    class Escape(BaseHTTPRequestHandler):
        def do_GET(self):
            escaped_requests.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    escaped = ThreadingHTTPServer(('127.0.0.1', 0), Escape)
    thread = threading.Thread(target=escaped.serve_forever, daemon=True)
    thread.start()
    try:
        with origin_server(tmp_path) as (base, unused_client):
            other = 'http://127.0.0.1:%d' % escaped.server_port
            client = HttpClient(UrlPolicy(test_origins={base, other}))
            from tools import ad_fixture_server
            original = ad_fixture_server.vast_xml
            monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
                original('wrapped', origin).replace((origin + '/vast/direct').encode(),
                                                    (other + '/vast/direct').encode())
                if scenario == 'direct' else original(scenario, origin))
            with pytest.raises(AdError) as error:
                VastResolver(client, base).resolve(base + '/vast/direct', threading.Event())
            assert_ad_error(error, 100, 'Advertising origin rejected')
            assert escaped_requests == []
            assert beacons(base) == {}
    finally:
        escaped.shutdown()
        escaped.server_close()
        thread.join(timeout=2)


def test_wrapper_no_fill_control_blocks_empty_child(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
            original('wrapped', origin).replace(b'followAdditionalWrappers="true"',
                                                b'fallbackOnNoAd="false"')
            .replace(b'/vast/direct', b'/vast/no-fill')
            if scenario == 'wrapped' else original(scenario, origin))
        with pytest.raises(AdError) as error:
            VastResolver(client, base).resolve(base + '/vast/wrapped', threading.Event())
        assert_ad_error(error, 303, 'VAST wrapper returned no ad')
        assert beacons(base) == {}


def test_wrapper_no_fill_reports_inherited_error_even_when_fallback_allowed(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
            original('wrapped', origin).replace(b'/vast/direct', b'/vast/no-fill')
            if scenario == 'wrapped' else original(scenario, origin))
        with pytest.raises(AdError) as error:
            VastResolver(client, base).resolve(base + '/vast/wrapped', threading.Event())
        assert_ad_error(error, 303, 'VAST wrapper returned no ad')
        assert error.value.error_urls == (base + '/track/placement-1/error?[ERRORCODE]',)


def test_macros_are_encoded_and_unknown_endpoint_is_unusable(tmp_path, monkeypatch):
    from vast import VastResolver
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
            original('direct', origin).replace(b'[ERRORCODE]', b'[ERRORCODE]&amp;cb=[CACHEBUSTING]')
            .replace(b'/track/placement-1/start', b'/track/placement-1/start?unknown=[UNKNOWN]')
            if scenario == 'direct' else original(scenario, origin))
        ad = VastResolver(client, base).resolve(base + '/vast/direct', threading.Event())
        assert len(ad.error_urls) == 1
        assert all('[ERRORCODE]' in url and '[CACHEBUSTING]' in url for url in ad.error_urls)
        assert all(item.event != 'start' for item in ad.tracking)
        assert beacons(base) == {}


def test_macro_substitution_encodes_values():
    from vast import substitute_macros
    assert substitute_macros('http://fixture.test/e?code=[ERRORCODE]&cb=[CACHEBUSTING]',
                             error_code='3 01', cachebuster='a&b') == \
           'http://fixture.test/e?code=3%2001&cb=a%26b'
    assert substitute_macros('http://fixture.test/e?x=[UNKNOWN]', error_code=301) is None
    assert substitute_macros('http://fixture.test/e?x=[ERRORCODE]') is None


@pytest.mark.parametrize('replacement', [
    b'<Duration>NaN</Duration>',
    b'<Duration>00:01:01.000</Duration>',
    b'<Duration>00:00:00.000</Duration>',
])
def test_invalid_duration_rejected(tmp_path, monkeypatch, replacement):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
            original('direct', origin).replace(b'<Duration>00:00:04.000</Duration>', replacement)
            if scenario == 'direct' else original(scenario, origin))
        with pytest.raises(AdError) as error:
            VastResolver(client, base).resolve(base + '/vast/direct', threading.Event())
        assert_ad_error(error, 403, 'Invalid VAST duration')
        assert beacons(base) == {}


def test_oversized_xml_depth_nodes_and_media_url_rejected(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        resolver = VastResolver(client, base)
        variants = (
            (b'<VAST version="4.3">' + b'<x>' * 33 + b'</x>' * 33 + b'</VAST>',
             (100, 'VAST XML exceeds limits')),
            (b'<VAST version="4.3">' + b'<x/>' * 4097 + b'</VAST>',
             (100, 'VAST XML exceeds limits')),
            (original('direct', base).replace(b'/ads/replacement.m3u8', b'/ads/' + b'x' * 8192),
             (100, 'Invalid advertising URL')),
            (original('direct', base).replace((base + '/ads/replacement.m3u8').encode(),
                                              b'/ads/' + b'x' * (8192 - 5)),
             (100, 'Invalid advertising URL')),
            (b'<VAST version="4.3">' + b' ' * (256 * 1024) + b'</VAST>',
             (301, 'VAST transport failed')),
        )
        for data, expected in variants:
            monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin, data=data: data)
            with pytest.raises(AdError) as error:
                resolver.resolve(base + '/vast/direct', threading.Event())
            assert_ad_error(error, *expected)
        assert beacons(base) == {}


def test_wrapper_required_verification_is_rejected(tmp_path, monkeypatch):
    from vast import VastResolver
    from ad_models import AdError
    from tools import ad_fixture_server
    original = ad_fixture_server.vast_xml
    with origin_server(tmp_path) as (base, client):
        monkeypatch.setattr(ad_fixture_server, 'vast_xml', lambda scenario, origin:
            original('wrapped', origin).replace(b'</Wrapper>',
                b'<AdVerifications><Verification vendor="required"/></AdVerifications></Wrapper>')
            if scenario == 'wrapped' else original(scenario, origin))
        with pytest.raises(AdError) as error:
            VastResolver(client, base).resolve(base + '/vast/wrapped', threading.Event())
        assert_ad_error(error, 403, 'Unsupported required VAST feature')
        assert error.value.error_urls == (base + '/track/placement-1/error?[ERRORCODE]',)
        assert beacons(base) == {}
