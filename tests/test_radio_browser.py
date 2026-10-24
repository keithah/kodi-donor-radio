# SPDX-License-Identifier: GPL-2.0-or-later
import io
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest


class Reply:
    def __init__(self, body, mime='application/json', final_url='https://radio.example/source', status=200):
        self.body = io.BytesIO(body)
        self.headers = {'content-type': mime}
        self.final_url, self.status, self.closed = final_url, status, False
    def read(self, size):
        return self.body.read(size)
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class Transport:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def open(self, url, headers, cancel):
        self.calls.append((url, headers))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def browser(replies):
    from radio_browser import RadioBrowser
    transport = Transport(replies)
    return RadioBrowser(transport, mirror_lookup=lambda: ['https://one.example', 'https://two.example']), transport


def test_search_failover_unicode_pagination_and_malformed_isolation():
    station = {'stationuuid': 'abc', 'name': 'Café', 'url_resolved': 'https://radio.example/live',
               'url': 'http://radio.example/', 'codec': 'MP3', 'hls': 0}
    rb, transport = browser([OSError('offline'), Reply(json.dumps([None, {}, dict(station, hls=[]), station]).encode()), Reply(b'[]')])
    result = rb.search('Café 音', 'France', 'jazz', 0)
    assert [(s.uuid, s.name, s.url, s.codec, s.hls) for s in result] == [('abc', 'Café', 'https://radio.example/live', 'MP3', False)]
    assert rb.search('Café 音', 'France', 'jazz', 50) == []
    assert transport.calls[0][0].startswith('https://one.example/json/stations/search?')
    assert transport.calls[1][0].startswith('https://two.example/json/stations/search?')
    for call, offset in [(transport.calls[1], '0'), (transport.calls[2], '50')]:
        assert parse_qs(urlsplit(call[0]).query) == {'name': ['Café 音'], 'country': ['France'], 'countryExact': ['true'], 'tag': ['jazz'], 'tagExact': ['true'], 'offset': [offset], 'limit': ['50'], 'hidebroken': ['true']}
        assert call[1]['User-Agent'] == 'kodi-donor-radio/0.1.0'


def test_countries_and_tags_are_bounded_and_paginated():
    rb, transport = browser([Reply(b'[{"name":"France"}, null, {"name":8}]'), Reply(b'[{"name":"jazz"}]'), Reply(b'[]')])
    assert rb.countries() == ['France']
    assert rb.tags(0) == ['jazz']
    assert rb.tags(50) == []
    assert '/json/countries?' in transport.calls[0][0]
    assert parse_qs(urlsplit(transport.calls[2][0]).query) == {'offset': ['50'], 'limit': ['50']}


def test_tag_page_uses_raw_row_count_for_continuation():
    rows = [{'name': 'tag-%02d' % i} for i in range(49)] + [None]
    rb, _ = browser([Reply(json.dumps(rows).encode())])
    names, more = rb.tags_page(0, 50)
    assert len(names) == 49 and more is True


def test_click_one_attempt_per_selection_even_uncertain_delivery():
    rb, transport = browser([TimeoutError('delivery uncertain'), Reply(b'{"ok":true}')])
    rb.click('station-uuid')
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == 'https://one.example/json/url/station-uuid'
    rb.click('station-uuid')  # A later explicit selection is a distinct click.
    assert len(transport.calls) == 2


def test_empty_mirror_discovery_is_not_cached_and_can_recover():
    from http_client import HttpError
    from radio_browser import RadioBrowser

    lookups = iter(([], ['https://recovered.example']))
    calls = []
    def lookup():
        calls.append(True)
        return next(lookups)
    transport = Transport([Reply(b'[]')])
    rb = RadioBrowser(transport, mirror_lookup=lookup)

    with pytest.raises(HttpError, match='No Radio Browser mirrors available'):
        rb.countries()
    assert rb._mirrors is None

    assert rb.countries() == []
    assert len(calls) == 2
    assert transport.calls[0][0] == 'https://recovered.example/json/countries?limit=300'


def test_dns_mirror_discovery_uses_forward_then_reverse(monkeypatch):
    import radio_browser
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, **kw: [(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('8.8.4.4', 443))] if host == 'all.api.radio-browser.info' else [])
    monkeypatch.setattr(socket, 'gethostbyaddr', lambda ip: ('de1.api.radio-browser.info' if ip == '8.8.8.8' else 'nl1.api.radio-browser.info', [], [ip]))
    assert set(radio_browser.discover_mirrors()) == {'https://de1.api.radio-browser.info', 'https://nl1.api.radio-browser.info'}


@pytest.mark.parametrize('body,mime', [(b'#EXTM3U\n/live\n', 'audio/x-mpegurl'), (b'[playlist]\nFile1=/live\n', 'audio/x-scpls'), (b'#EXTM3U\n/live\n', 'application/octet-stream'), (b'[playlist]\nFile1=/live\n', 'text/plain')])
def test_resolve_m3u_pls_mime_and_sniffing(body, mime):
    from station_resolver import resolve_station
    playlist = Reply(body, mime)
    stream = Reply(b'audio', 'audio/mpeg', 'https://radio.example/live')
    transport = Transport([playlist, stream])
    source = resolve_station('https://radio.example/source', transport, threading.Event())
    assert source.protocol == 'continuous'
    assert source.final_url == 'https://radio.example/live'
    assert source.response is stream and not stream.closed
    assert playlist.closed
    assert transport.calls[1][0] == 'https://radio.example/live'
    source.response.close()


def test_unknown_continuous_probe_transfers_prefix_without_reopen():
    from station_resolver import resolve_station
    stream = Reply(b'ID3audio payload', 'application/octet-stream')
    transport = Transport([stream])
    source = resolve_station('https://radio.example/live', transport, threading.Event())
    assert source.response is stream
    assert source.initial_bytes + source.response.read(100) == b'ID3audio payload'
    assert len(transport.calls) == 1 and not stream.closed
    source.response.close()


def test_hls_sniffing_stops_at_manifest_and_closes_probe():
    from station_resolver import resolve_station
    response = Reply(b'#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n', 'audio/x-mpegurl')
    source = resolve_station('https://radio.example/source', Transport([response]), threading.Event())
    assert source.protocol == 'hls'
    assert source.response is None and response.closed


def test_indirection_loops_close_responses():
    from station_resolver import resolve_station, ResolutionError
    response = Reply(b'#EXTM3U\n/source\n', 'audio/x-mpegurl')
    with pytest.raises(ResolutionError):
        resolve_station('https://radio.example/source', Transport([response]), threading.Event())
    assert response.closed


def test_indirection_depth_is_limited():
    from station_resolver import resolve_station, ResolutionError
    replies = [Reply(('#EXTM3U\n/%s\n' % (i + 1)).encode(), 'audio/x-mpegurl', 'https://radio.example/%s' % i) for i in range(4)]
    transport = Transport(replies)
    with pytest.raises(ResolutionError):
        resolve_station('https://radio.example/0', transport, threading.Event())
    assert len(transport.calls) == 4
    assert all(reply.closed for reply in replies)


def test_oversized_playlist_is_rejected_and_closed():
    from station_resolver import resolve_station
    from http_client import ResponseTooLarge
    response = Reply(b'#EXTM3U\n' + b'#' * (1024 * 1024), 'audio/x-mpegurl')
    with pytest.raises(ResponseTooLarge):
        resolve_station('https://radio.example/source', Transport([response]), threading.Event())
    assert response.closed


def test_fragmented_playlist_signature_still_resolves():
    from station_resolver import resolve_station
    class FragmentedReply(Reply):
        def read(self, size):
            return super().read(min(1, size))
    playlist = FragmentedReply(b'#EXTM3U\n/live\n', 'application/octet-stream')
    stream = Reply(b'audio', 'audio/mpeg', 'https://radio.example/live')
    source = resolve_station('https://radio.example/source', Transport([playlist, stream]), threading.Event())
    assert source.response is stream and playlist.closed
    source.response.close()


@pytest.mark.parametrize('stall', ['injected', 'forward', 'reverse'])
def test_mirror_discovery_has_one_bounded_deadline(monkeypatch, stall):
    import socket
    import time
    from radio_browser import RadioBrowser
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        release.wait(2)
        return []
    lookup = blocked if stall == 'injected' else None
    if stall == 'forward':
        monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    elif stall == 'reverse':
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(socket, 'gethostbyaddr', blocked)
    transport = Transport([])
    rb = RadioBrowser(transport, mirror_lookup=lookup, connect_timeout=.05)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            rb.countries()
        assert entered.is_set()
        assert time.monotonic() - started < .3
        assert transport.calls == []
    finally:
        release.set()


@pytest.mark.parametrize('stall', ['injected', 'forward', 'reverse'])
def test_mirror_discovery_can_be_cancelled(monkeypatch, stall):
    import socket
    import time
    from http_client import Cancelled
    from radio_browser import RadioBrowser
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        release.wait(2)
        return []
    lookup = blocked if stall == 'injected' else None
    if stall == 'forward':
        monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    elif stall == 'reverse':
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(socket, 'gethostbyaddr', blocked)
    rb = RadioBrowser(Transport([]), mirror_lookup=lookup, cancel=cancel)
    timer = threading.Timer(.05, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            rb.countries()
        assert entered.is_set()
        assert time.monotonic() - started < .3
    finally:
        release.set()
        timer.join()


@pytest.mark.parametrize('country,tag,exact', [
    ('', '', {}), ('Congo', '', {'countryExact':['true']}),
    ('', 'rock', {'tagExact':['true']}),
    ('Congo', 'rock', {'countryExact':['true'], 'tagExact':['true']}),
])
def test_selected_categories_are_exact_but_name_search_remains_partial(country,tag,exact):
    rb, transport = browser([Reply(b'[]')])
    assert rb.search('Radio', country, tag, 50, limit=25) == []
    query = parse_qs(urlsplit(transport.calls[0][0]).query, keep_blank_values=True)
    assert query == dict({'name':['Radio'], 'country':[country], 'tag':[tag],
                          'offset':['50'], 'limit':['25'], 'hidebroken':['true']}, **exact)


def test_station_optional_metadata_is_parsed_and_bounded():
    row = {'stationuuid': 'a', 'name': 'Radio', 'url': 'https://radio.example/live',
           'country': 'France', 'countrycode': 'fr', 'state': 'State', 'city': 'City',
           'language': 'French', 'tags': 'jazz,news', 'favicon': 'https://radio.example/icon.png',
           'bitrate': 128, 'geo_lat': 48.5, 'geo_long': 2.5, 'clickcount': 12, 'votes': 3}
    rb, _ = browser([Reply(json.dumps([row, dict(row, country='X'*1000, geo_lat=91,
                             bitrate=-1, votes=True, favicon='file:///secret')]).encode())])
    good, bad = rb.search('', '', '', 0)
    assert (good.country, good.country_code, good.city, good.language, good.tags) == ('France', 'FR', 'City', 'French', 'jazz,news')
    assert (good.latitude, good.longitude, good.bitrate, good.click_count, good.votes) == (48.5, 2.5, 128, 12, 3)
    assert len(bad.country) <= 256
    assert bad.latitude is None and bad.longitude is None
    assert bad.bitrate == 0 and bad.votes == 0 and bad.favicon == ''


def test_by_uuid_validated_lookup():
    row = {'stationuuid': 'abc-123', 'name': 'Radio', 'url': 'https://radio.example/live', 'country': 'France'}
    rb, transport = browser([Reply(json.dumps([row]).encode())])
    assert rb.by_uuid('abc-123').country == 'France'
    assert transport.calls[0][0] == 'https://one.example/json/stations/byuuid?uuids=abc-123'


@pytest.mark.parametrize('body', [b'[]', b'{}', b'[null]', b'[{"stationuuid":"other","name":"Radio","url":"https://radio.example"}]'])
def test_by_uuid_rejects_missing_or_malformed_station(body):
    rb, _ = browser([Reply(body)])
    with pytest.raises(ValueError):
        rb.by_uuid('abc-123')


@pytest.mark.parametrize('uuid', ['', 'a'*129, '../bad', 'bad,other', None])
def test_by_uuid_rejects_invalid_identifiers_without_network(uuid):
    rb, transport = browser([])
    with pytest.raises(ValueError):
        rb.by_uuid(uuid)
    assert transport.calls == []


def test_invalid_country_code_and_empty_normalized_name_are_not_persisted():
    row = {'stationuuid': 'a', 'name': 'Radio', 'url': 'https://radio.example/live', 'countrycode': 'ßß'}
    rb, _ = browser([Reply(json.dumps([row, dict(row, name='\n')]).encode())])
    results = rb.search('', '', '', 0)
    assert len(results) == 1
    assert results[0].country_code == ''


def test_geographic_query_uses_only_documented_filters_and_tracks_raw_page_length():
    row = {'stationuuid':'a','name':'Radio','url':'https://radio.example/live','geo_lat':0,'geo_long':0}
    rb, transport = browser([Reply(json.dumps([None]*49+[row]).encode())])
    stations, more = rb.geographic('FR', 50, 50)
    assert [s.uuid for s in stations] == ['a'] and more is True
    assert parse_qs(urlsplit(transport.calls[0][0]).query) == {
        'countrycode':['FR'], 'has_geo_info':['true'], 'hidebroken':['true'],
        'order':['clickcount'], 'reverse':['true'], 'offset':['50'], 'limit':['50']}


def test_global_geo_fallback_omits_country_and_short_page_is_exhausted():
    rb, transport = browser([Reply(b'[]')])
    assert rb.geographic('', 0, 50) == ([], False)
    query = parse_qs(urlsplit(transport.calls[0][0]).query)
    assert 'countrycode' not in query and query['has_geo_info'] == ['true']
    assert not any(k in query for k in ('latitude','longitude','radius','distance'))


def test_country_listing_preserves_names_and_codes_for_routes():
    rb, _ = browser([Reply(b'[{"name":"France","iso_3166_1":"FR"},{"name":"Invalid","iso_3166_1":"123"}]')])
    assert rb.country_entries() == [('France','FR')]


def test_country_search_is_code_scoped_and_popularity_ordered():
    rb, transport = browser([Reply(b'[]')])
    assert rb.search_country('FR', 50) == []
    assert parse_qs(urlsplit(transport.calls[0][0]).query) == {'countrycode':['FR'],
        'offset':['50'],'limit':['50'],'hidebroken':['true'],'order':['clickcount'],'reverse':['true']}


@pytest.mark.parametrize('code', ['FRA','fr','../FR',None])
def test_invalid_country_queries_never_reach_network(code):
    rb, transport = browser([])
    with pytest.raises(ValueError): rb.geographic(code, 0, 50)
    assert transport.calls == []


@pytest.mark.parametrize('country_search', [False, True])
def test_station_search_pages_keep_raw_continuation_after_malformed_rows(country_search):
    row = {'stationuuid': 'station-1', 'name': 'Radio', 'url': 'https://radio.example/live'}
    rb, transport = browser([Reply(json.dumps([None] + [row] * 49).encode()), Reply(b'[]')])
    if country_search:
        stations, has_more = rb.search_country_page('FR', 0)
        next_stations, next_has_more = rb.search_country_page('FR', 50)
    else:
        stations, has_more = rb.search_page('', '', 'jazz', 0)
        next_stations, next_has_more = rb.search_page('', '', 'jazz', 50)
    assert len(stations) == 49 and has_more is True
    assert next_stations == [] and next_has_more is False
    assert parse_qs(urlsplit(transport.calls[-1][0]).query)['offset'] == ['50']
