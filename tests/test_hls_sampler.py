import pytest
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from models import Station
from http_client import HttpClient
from url_policy import UrlPolicy
from tools.sample_hls_ad_markers import (discover_candidates, host_is_cdnstream1,
                                         JsonLineWriter, load_media_playlist, sample_station)


def test_host_filter_requires_cdnstream1_domain_boundary():
    assert host_is_cdnstream1('https://cdnstream1.com/live.m3u8') is True
    assert host_is_cdnstream1('http://edge.cdnstream1.com/live') is True
    assert host_is_cdnstream1('https://notcdnstream1.com/live') is False
    assert host_is_cdnstream1('https://cdnstream1.com.example.org/live') is False
    assert host_is_cdnstream1('://cdnstream1.com/live') is False
    assert host_is_cdnstream1('file://cdnstream1.com/live') is False


@pytest.mark.parametrize('url', [
    'https://cdnstream1.com:bad/path',
    'https://cdnstream1.com:65536/path',
    'https://cdnstream1.com:/path',
])
def test_host_filter_rejects_malformed_and_out_of_range_ports(url):
    assert host_is_cdnstream1(url) is False


def _station(uuid, url='https://cdnstream1.com/live', hls=True):
    return Station(uuid, uuid, url, 'AAC', hls)


class _PagedBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search(self, name, country, tag, offset, limit):
        self.calls.append((name, country, tag, offset, limit))
        return self.pages[offset]


def test_discovery_filters_hls_deduplicates_and_stops_at_bounds():
    first_page = [_station('one'), _station('one'), _station('non-hls', hls=False),
                  _station('lookalike', 'https://cdnstream1.com.example.org/live'),
                  _station('two')]
    first_page.extend(_station('other-%d' % index, hls=False)
                      for index in range(45))
    second_page = [_station('two'), _station('three')]
    browser = _PagedBrowser({0: first_page, 50: second_page})

    stations, pages_read = discover_candidates(browser, page_limit=2, candidate_limit=3)

    assert [station.uuid for station in stations] == ['one', 'two', 'three']
    assert pages_read == 2
    assert browser.calls == [('', '', '', 0, 50), ('', '', '', 50, 50)]

    capped = _PagedBrowser({0: first_page, 50: second_page})
    stations, pages_read = discover_candidates(capped, page_limit=1, candidate_limit=20)
    assert [station.uuid for station in stations] == ['one', 'two']
    assert pages_read == 1
    assert capped.calls == [('', '', '', 0, 50)]


@pytest.mark.parametrize('page_limit,candidate_limit', [(0, 1), (101, 1), (1, 0), (1, 21)])
def test_discovery_rejects_limits_outside_bounds(page_limit, candidate_limit):
    with pytest.raises(ValueError):
        discover_candidates(_PagedBrowser({}), page_limit, candidate_limit)


class _Clock:
    def __init__(self, cancel=None):
        self.value = 0.0
        self.cancel = cancel

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds
        if self.cancel is not None:
            self.cancel.set()


@pytest.fixture
def playlists():
    routes, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            value = routes.get(self.path, (404, ''))
            status, body = value.pop(0) if isinstance(value, list) else value
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_port
    try:
        yield base, routes, requests, HttpClient(UrlPolicy(test_origins={base}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _run_sample(playlists, duration=3, cancel=None, clock=None, name='Radio'):
    base, _, _, client = playlists
    cancel = cancel or threading.Event()
    clock = clock or _Clock()
    output = io.StringIO()
    writer = JsonLineWriter(output)
    station = Station('station-1', name, base + '/live.m3u8?token=QUERYSECRET', 'AAC', True)
    result = sample_station(station, client, writer, duration, cancel, clock.now, clock.wait)
    return result, [json.loads(line) for line in output.getvalue().splitlines()]


def test_sampler_emits_hls_cue_events_without_fetching_segments(playlists):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nmedia.m3u8?token=MEDIASECRET\n')
    routes['/media.m3u8?token=MEDIASECRET'] = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-CUE-OUT:8\n#EXTINF:4,\nsegment1.aac?token=SEGMENTSECRET\n#EXT-X-CUE-IN\n#EXTINF:4,\nsegment2.aac\n')
    result, records = _run_sample(playlists)
    assert result['outcome'] == 'marker_observed'
    assert {'cue_observed', 'break_start', 'break_end'} <= {r['kind'] for r in records}
    assert all('segment' not in path for path in requests)
    assert requests.count('/media.m3u8?token=MEDIASECRET') >= 2


def test_sampler_emits_adswizz_hls_events_across_overlapping_windows(playlists):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nmedia.m3u8\n')
    routes['/media.m3u8'] = [
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,adswizzContext=OPAQUESECRET\nhttps://delivery-cdn-cf.adswizz.com/ad_SECRET.aac\n'),
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,adswizzContext=OPAQUESECRET\nhttps://delivery-cdn-cf.adswizz.com/ad_SECRET.aac\n#EXTINF:4,\nregular.aac\n'),
    ]
    result, records = _run_sample(playlists)
    assert result['outcome'] == 'marker_observed'
    assert [r['kind'] for r in records].count('break_start') == 1
    assert [r['kind'] for r in records].count('break_end') == 1
    assert all('aac' not in path for path in requests)


def test_sampler_redacts_urls_context_and_cue_payloads(playlists):
    _, routes, _, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-CUE-OUT:8\n#EXTINF:4,adswizzContext=OPAQUESECRET\nhttps://delivery-cdn-cf.adswizz.com/ad_SECRET.aac\n#EXT-X-DATERANGE:ID="IDSECRET",SCTE35-OUT=0xFC00\n')
    result, records = _run_sample(playlists, name='Station\nhttps://secret.example/?token=NAMESECRET')
    encoded = json.dumps((result, records))
    for secret in ('QUERYSECRET', 'OPAQUESECRET', 'IDSECRET', 'FC00', 'NAMESECRET', 'ad_SECRET.aac', '/live.m3u8'):
        assert secret not in encoded
    assert all(set(record) <= {'station_uuid', 'station_name', 'kind', 'source', 'observed_at', 'media_sequence', 'discontinuity', 'media_position', 'duration', 'classification', 'reason'} for record in records)
    assert all(len(record['station_name']) <= 128 for record in records)


def test_sampler_reports_unresolved_break_on_cancel(playlists):
    _, routes, _, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-CUE-OUT:8\n#EXTINF:4,\nsegment.aac\n')
    cancel = threading.Event()
    result, records = _run_sample(playlists, duration=30, cancel=cancel, clock=_Clock(cancel))
    assert result['outcome'] == 'cancelled'
    assert any(r['kind'] == 'break_unresolved' for r in records)


def test_sampler_epoch_resets_do_not_join_ad_breaks(playlists):
    _, routes, _, _ = playlists
    first = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:100\n#EXT-X-CUE-OUT:8\n#EXTINF:4,\nsegment.aac\n')
    routes['/live.m3u8?token=QUERYSECRET'] = [
        first,
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-CUE-IN\n#EXTINF:4,\nsegment2.aac\n'),
    ]
    _, records = _run_sample(playlists)
    assert any(r['kind'] == 'break_unresolved' and r['reason'] == 'epoch_changed' for r in records)
    assert not any(r['kind'] == 'break_end' for r in records)


def test_master_playlist_depth_is_bounded(playlists):
    base, routes, requests, client = playlists
    for depth in range(4):
        routes['/%d.m3u8' % depth] = (200, '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\n%d.m3u8\n' % (depth + 1))
    with pytest.raises(ValueError):
        load_media_playlist(base + '/0.m3u8', client, threading.Event())
    assert '/4.m3u8' not in requests


def test_sampler_does_not_follow_m3u_to_media(playlists):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\nmedia.aac?token=SECRET\n')
    routes['/media.aac?token=SECRET'] = (200, 'CONTINUOUS_MEDIA')

    result, records = _run_sample(playlists)

    assert result['outcome'] == 'unsupported'
    assert records == []
    assert requests == ['/live.m3u8?token=QUERYSECRET']


def test_observation_duration_starts_after_media_playlist_load(playlists):
    base, routes, requests, client = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nsegment.aac\n')
    clock = _Clock()

    class DelayedClient:
        def __init__(self):
            self.calls = 0

        def open(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                clock.value += 10
            return client.open(*args, **kwargs)

    output = io.StringIO()
    station = Station('station-1', 'Radio', base + '/live.m3u8?token=QUERYSECRET', 'AAC', True)
    result = sample_station(station, DelayedClient(), JsonLineWriter(output),
                            3, threading.Event(), clock.now, clock.wait)

    assert result['poll_count'] >= 2
    assert clock.value >= 13
    assert requests.count('/live.m3u8?token=QUERYSECRET') >= 2


def test_sampler_rejects_continuous_body_after_signature_probe(playlists):
    base, routes, requests, client = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (200, 'A' * 4096)
    observed = []

    class CountingResponse:
        def __init__(self, response):
            self.response = response
            self.status = response.status
            self.final_url = response.final_url

        def read(self, size):
            data = self.response.read(size)
            observed.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.response.close()

    class CountingClient:
        def open(self, *args, **kwargs):
            return CountingResponse(client.open(*args, **kwargs))

    station = Station('station-1', 'Radio', base + '/live.m3u8?token=QUERYSECRET', 'AAC', True)
    result = sample_station(station, CountingClient(), JsonLineWriter(io.StringIO()),
                            3, threading.Event(), _Clock().now, lambda seconds: None)

    assert result['outcome'] == 'unsupported'
    assert sum(observed) <= 10
    assert requests == ['/live.m3u8?token=QUERYSECRET']
