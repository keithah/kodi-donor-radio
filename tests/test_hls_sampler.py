import pytest
import io
import json
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from models import Station
from http_client import HttpClient
from url_policy import UrlPolicy
from tools.sample_hls_ad_markers import (discover_candidates, host_is_cdnstream1,
                                         JsonLineWriter, load_media_playlist, sample_station)
import tools.sample_hls_ad_markers as sampler


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

    def search_page(self, name, country, tag, offset, limit):
        page = self.search(name, country, tag, offset, limit)
        return page, len(page) >= limit


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


def test_discovery_uses_raw_row_continuation_after_malformed_row():
    class Browser(_PagedBrowser):
        def search_page(self, name, country, tag, offset, limit):
            page = self.search(name, country, tag, offset, limit)
            return page, offset == 0

    first = [_station('not-hls-%d' % index, hls=False) for index in range(49)]
    browser = Browser({0: first, 50: [_station('later-candidate')]})

    stations, pages_read = discover_candidates(browser, page_limit=2, candidate_limit=1)

    assert [station.uuid for station in stations] == ['later-candidate']
    assert pages_read == 2
    assert [call[3] for call in browser.calls] == [0, 50]


def test_json_line_writer_flushes_each_complete_record():
    class BufferedDestination:
        def __init__(self):
            self.pending = ''
            self.visible = ''
            self.flush_count = 0

        def write(self, value):
            self.pending += value

        def flush(self):
            self.visible += self.pending
            self.pending = ''
            self.flush_count += 1

    destination = BufferedDestination()
    writer = JsonLineWriter(destination)
    writer.emit_record({'kind': 'cue_observed', 'station_uuid': 'one'})
    assert destination.pending == ''
    assert destination.flush_count == 1
    assert json.loads(destination.visible)['kind'] == 'cue_observed'


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
            response = value.pop(0) if isinstance(value, list) else value
            status, body = response[:2]
            content_type = response[2] if len(response) > 2 else 'application/vnd.apple.mpegurl'
            data = body.encode()
            self.send_response(status)
            if content_type is not None:
                self.send_header('Content-Type', content_type)
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


def test_sampler_cancels_blocked_playlist_response():
    blocked = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    requests = []
    first_body = (b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-CUE-OUT:8\n'
                  b'#EXTINF:4,\nsegment.aac\n')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                requests.append(self.path)
                request_number = len(requests)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(first_body) if request_number == 1 else 1024))
            self.end_headers()
            if request_number == 1:
                self.wfile.write(first_body)
            else:
                self.wfile.flush()
                blocked.set()
                release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = 'http://127.0.0.1:%d' % server.server_port
    client = HttpClient(UrlPolicy(test_origins={origin}))
    station = Station('blocked', 'Blocked Radio', origin + '/live.m3u8', 'AAC', True)
    cancel = threading.Event()
    clock = _Clock()
    output = io.StringIO()
    result = []
    worker = threading.Thread(target=lambda: result.append(sample_station(
        station, client, JsonLineWriter(output), 30, cancel, clock.now, clock.wait)))
    try:
        worker.start()
        assert blocked.wait(3), 'second playlist response did not block'
        assert 'break_start' in output.getvalue()
        cancel.set()
        worker.join(3)
        assert not worker.is_alive(), 'blocked playlist read did not cancel'
        assert len(requests) == 2
        assert result[0]['outcome'] == 'cancelled'
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        assert any(record['kind'] == 'break_unresolved' for record in records)
    finally:
        cancel.set()
        release.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
        server_thread.join()


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
    assert requests == ['/0.m3u8', '/1.m3u8', '/2.m3u8']


def test_sampler_recovers_after_transient_poll_failures(playlists):
    _, routes, requests, _ = playlists
    manifest = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,\nsegment.aac\n')
    routes['/live.m3u8?token=QUERYSECRET'] = [
        manifest, (503, ''), (503, ''), (503, ''), manifest,
    ]

    result, _ = _run_sample(playlists, duration=9)

    assert result['outcome'] == 'no_marker_observed'
    assert result['poll_count'] == 2
    assert result['elapsed_seconds'] >= 9
    assert len(requests) == 5


def test_sampler_resets_reachability_streak_after_successful_poll(playlists):
    _, routes, requests, _ = playlists
    manifest = (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nsegment.aac\n')
    routes['/live.m3u8?token=QUERYSECRET'] = [
        manifest, (503, ''), (503, ''), (503, ''), manifest,
        (503, ''), (503, ''), (503, ''), manifest,
    ]

    result, _ = _run_sample(playlists, duration=17)

    assert result['outcome'] == 'no_marker_observed'
    assert result['poll_count'] == 3
    assert result['elapsed_seconds'] >= 17
    assert len(requests) == 9


def test_sampler_preserves_marker_after_exhausted_poll_failures(playlists):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = [
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-CUE-OUT:8\n#EXTINF:4,\nsegment.aac\n'),
        (503, ''), (503, ''), (503, ''), (503, ''),
    ]

    result, records = _run_sample(playlists, duration=30)

    assert result['outcome'] == 'marker_observed'
    assert result['elapsed_seconds'] == 8
    assert len(requests) == 5
    assert any(record['kind'] == 'break_start' for record in records)
    assert any(record['kind'] == 'break_unresolved' for record in records)


def test_sampler_cancel_during_failed_poll_keeps_unresolved_break(playlists):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = [
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-CUE-OUT:8\n#EXTINF:4,\nsegment.aac\n'),
        (503, ''),
    ]
    cancel = threading.Event()
    clock = _Clock()
    waits = []

    def wait(seconds):
        clock.wait(seconds)
        waits.append(seconds)
        if clock.value >= 2.25:
            cancel.set()

    output = io.StringIO()
    writer = JsonLineWriter(output)
    base, _, _, client = playlists
    station = Station('station-1', 'Radio', base + '/live.m3u8?token=QUERYSECRET', 'AAC', True)
    result = sample_station(station, client, writer, 30, cancel, clock.now, wait)
    records = [json.loads(line) for line in output.getvalue().splitlines()]

    assert result['outcome'] == 'cancelled'
    assert len(requests) == 2
    assert any(record['kind'] == 'break_unresolved' for record in records)


@pytest.mark.parametrize('second_response,outcome', [
    ((200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nsegment.aac\n', 'audio/aac'),
     'unsupported'),
    ((200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:BAD,\nsegment.aac\n'),
     'parser_failure'),
])
def test_sampler_does_not_retry_terminal_poll_failure(playlists, second_response, outcome):
    _, routes, requests, _ = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = [
        (200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nsegment.aac\n'),
        second_response,
    ]

    result, _ = _run_sample(playlists, duration=30)

    assert result['outcome'] == outcome
    assert len(requests) == 2


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
            self.headers = response.headers
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


@pytest.mark.parametrize('content_type', ['audio/aac', 'application/octet-stream', None])
def test_sampler_rejects_nonplaylist_mime_before_body_read(playlists, content_type):
    base, routes, requests, client = playlists
    routes['/live.m3u8?token=QUERYSECRET'] = (
        200, '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nmedia.aac\n', content_type)
    reads = []

    class TrackedResponse:
        def __init__(self, response):
            self.response = response
            self.status = response.status
            self.headers = response.headers
            self.final_url = response.final_url

        def read(self, size):
            reads.append(size)
            return self.response.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.response.close()

    class TrackedClient:
        def open(self, *args, **kwargs):
            return TrackedResponse(client.open(*args, **kwargs))

    station = Station('station-1', 'Radio', base + '/live.m3u8?token=QUERYSECRET', 'AAC', True)
    clock = _Clock()
    result = sample_station(station, TrackedClient(), JsonLineWriter(io.StringIO()),
                            3, threading.Event(), clock.now, clock.wait)

    assert result['outcome'] == 'unsupported'
    assert reads == []
    assert requests == ['/live.m3u8?token=QUERYSECRET']


def _fake_summary(station, outcome='no_marker_observed'):
    return {'station_uuid': station.uuid, 'station_name': station.name,
            'outcome': outcome, 'elapsed_seconds': 1200.0, 'poll_count': 10,
            'event_count': 0, 'truncated': False}


def test_cli_rejects_duration_below_twenty_minutes():
    with pytest.raises(SystemExit) as error:
        sampler.main(['--duration-seconds', '1199'])
    assert error.value.code == 2


def test_cli_applies_page_candidate_and_worker_bounds(monkeypatch, capsys):
    for arguments in (['--pages', '0'], ['--pages', '101'],
                      ['--limit', '0'], ['--limit', '21'],
                      ['--workers', '0'], ['--workers', '5'],
                      ['--duration-seconds', '7201']):
        with pytest.raises(SystemExit) as error:
            sampler.main(arguments)
        assert error.value.code == 2

    selected = [_station('one')]
    discovery_calls, sample_calls = [], []

    def discover(browser, pages, limit):
        discovery_calls.append((pages, limit))
        return selected, pages

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        sample_calls.append((station.uuid, duration, cancel))
        return _fake_summary(station)

    monkeypatch.setattr(sampler, 'discover_candidates', discover)
    monkeypatch.setattr(sampler, 'sample_station', sample)
    assert sampler.main([]) == 0
    assert sampler.main(['--pages', '100', '--limit', '20', '--workers', '4',
                         '--duration-seconds', '7200']) == 0
    assert discovery_calls == [(20, 8), (100, 20)]
    assert [(uuid, duration) for uuid, duration, _ in sample_calls] == [('one', 1200), ('one', 7200)]
    assert sample_calls[0][2] is not sample_calls[1][2]
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record['kind'] for record in records] == ['station_summary', 'station_summary']


def test_cli_caps_concurrent_station_workers(monkeypatch, capsys):
    stations = [_station(str(index)) for index in range(6)]
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: (stations, 1))
    state = {'active': 0, 'maximum': 0}
    lock = threading.Lock()
    cancellations = []

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        with lock:
            state['active'] += 1
            state['maximum'] = max(state['maximum'], state['active'])
            cancellations.append(cancel)
        time.sleep(0.02)
        with lock:
            state['active'] -= 1
        return _fake_summary(station)

    monkeypatch.setattr(sampler, 'sample_station', sample)
    assert sampler.main(['--workers', '2', '--limit', '6']) == 0
    assert state['maximum'] == 2
    assert len({id(cancel) for cancel in cancellations}) == 6
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 6
    assert all(record['kind'] == 'station_summary' for record in records)


def test_marker_free_sample_is_not_reported_as_ad_free(monkeypatch, capsys):
    station = _station('marker-free')
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: ([station], 1))
    monkeypatch.setattr(sampler, 'sample_station',
                        lambda station, client, writer, duration, cancel, monotonic, wait: _fake_summary(station))
    assert sampler.main([]) == 0
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record['kind'] == 'station_summary'
    assert record['outcome'] == 'no_marker_observed'
    assert 'ad_free' not in output


@pytest.mark.parametrize('failure', ['policy_rejection', 'reachability_failure', 'parser_failure'])
def test_cli_station_failure_returns_one_without_cancelling_peers(monkeypatch, capsys, failure):
    stations = [_station('failed'), _station('healthy')]
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: (stations, 1))
    cancellation_states = []

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        cancellation_states.append(cancel.is_set())
        return _fake_summary(station, failure if station.uuid == 'failed' else 'no_marker_observed')

    monkeypatch.setattr(sampler, 'sample_station', sample)
    assert sampler.main(['--workers', '2']) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {record['station_uuid']: record['outcome'] for record in records} == {
        'failed': failure, 'healthy': 'no_marker_observed'}
    assert cancellation_states == [False, False]


def test_cli_reports_empty_candidate_discovery(monkeypatch, capsys):
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: ([], 20))

    assert sampler.main([]) == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [{
        'kind': 'discovery_summary', 'outcome': 'no_candidates',
        'pages_read': 20, 'selected_count': 0,
    }]


def test_cli_interrupt_cancels_and_summarizes_all_workers(monkeypatch, capsys):
    stations = [_station(str(index)) for index in range(3)]
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: (stations, 1))
    cancellations = []
    lock = threading.Lock()

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        with lock:
            cancellations.append(cancel)
        cancel.wait(2)
        return _fake_summary(station, 'cancelled' if cancel.is_set() else 'no_marker_observed')

    def interrupt(futures):
        raise KeyboardInterrupt
        yield

    monkeypatch.setattr(sampler, 'sample_station', sample)
    monkeypatch.setattr(sampler, 'as_completed', interrupt)
    assert sampler.main(['--workers', '2', '--limit', '3']) == 130
    assert len(cancellations) == 3
    assert all(cancel.is_set() for cancel in cancellations)
    assert len({id(cancel) for cancel in cancellations}) == 3
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 3
    assert all(record['outcome'] == 'cancelled' for record in records)


def test_cli_directory_requests_have_total_deadline(monkeypatch):
    calls = []

    class Client:
        def open(self, url, headers, cancel, deadline=None):
            calls.append(deadline)

    class Browser:
        def __init__(self, client, cancel):
            self.client = client

        def search_page(self, name, country, tag, offset, limit):
            self.client.open('https://directory.example/stations', {}, threading.Event())
            return [], False

    monkeypatch.setattr(sampler, 'HttpClient', Client)
    monkeypatch.setattr(sampler, 'RadioBrowser', Browser)
    assert sampler.main([]) == 0
    assert len(calls) == 1
    assert calls[0] is not None
    assert 0 < calls[0] - time.monotonic() <= 15


def test_cli_interrupt_during_submission_cancels_started_workers(monkeypatch, capsys):
    stations = [_station('one'), _station('two')]
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: (stations, 1))
    original_executor = sampler.ThreadPoolExecutor
    observed = []

    class InterruptingExecutor(original_executor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.submissions = 0

        def submit(self, *args, **kwargs):
            self.submissions += 1
            if self.submissions == 2:
                raise KeyboardInterrupt
            return super().submit(*args, **kwargs)

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        observed.append(cancel)
        cancel.wait(0.5)
        return _fake_summary(station, 'cancelled' if cancel.is_set() else 'no_marker_observed')

    monkeypatch.setattr(sampler, 'ThreadPoolExecutor', InterruptingExecutor)
    monkeypatch.setattr(sampler, 'sample_station', sample)
    assert sampler.main(['--workers', '2']) == 130
    assert len(observed) == 1 and observed[0].is_set()
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {record['station_uuid'] for record in records} == {'one', 'two'}
    assert all(record['outcome'] == 'cancelled' for record in records)


def test_cli_interrupt_during_result_collection_cancels_workers(monkeypatch, capsys):
    stations = [_station('one'), _station('two')]
    monkeypatch.setattr(sampler, 'discover_candidates', lambda browser, pages, limit: (stations, 1))
    started = threading.Barrier(3)
    cancellations = []
    original_result = Future.result
    interrupted = []

    def sample(station, client, writer, duration, cancel, monotonic, wait):
        cancellations.append(cancel)
        started.wait(timeout=2)
        cancel.wait(0.5)
        return _fake_summary(station, 'cancelled' if cancel.is_set() else 'no_marker_observed')

    def ready_futures(futures):
        started.wait(timeout=2)
        yield next(iter(futures))

    def interrupt_once(future, *args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return original_result(future, *args, **kwargs)

    monkeypatch.setattr(sampler, 'sample_station', sample)
    monkeypatch.setattr(sampler, 'as_completed', ready_futures)
    monkeypatch.setattr(Future, 'result', interrupt_once)
    assert sampler.main(['--workers', '2']) == 130
    assert len(cancellations) == 2
    assert all(cancel.is_set() for cancel in cancellations)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {record['station_uuid'] for record in records} == {'one', 'two'}
    assert all(record['outcome'] == 'cancelled' for record in records)
