# SPDX-License-Identifier: GPL-2.0-or-later
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def server():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == '/legacy-icy':
                body = b'abcd\x02' + b"StreamTitle='Example';".ljust(32, b'\0') + b'efgh\0'
                self.connection.sendall(self.server.status_line +
                    b'\r\nContent-Type: audio/mpeg\r\nicy-metaint: 4\r\n\r\n' + body)
                self.close_connection = True
                return
            if self.path.startswith('/bad-status'):
                self.connection.sendall(b'garbage status token=secret\r\n\r\n')
                self.close_connection = True
                return
            if self.path == '/bad-chunk':
                self.connection.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\ninvalid-secret\r\n')
                self.close_connection = True
                return
            if self.path.startswith('/json/countries'):
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'[]')
                return
            if self.path == '/redirect-private':
                self.send_response(302)
                self.send_header('Location', 'http://192.168.1.2/secret')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/redirect-host':
                self.send_response(302)
                self.send_header('Location', 'http://other.example:%s/end' % self.server.server_port)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/deadline-redirect':
                time.sleep(.08)
                self.send_response(302)
                self.send_header('Location', '/deadline-trickle')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/oversized-ad-redirect':
                self.send_response(302)
                self.send_header('Location', '/' + 'x' * 9000)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/deadline-trickle':
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                for _ in range(100):
                    try:
                        self.wfile.write(b'x')
                        self.wfile.flush()
                    except OSError:
                        self.close_connection = True
                        break
                    time.sleep(.02)
                return
            self.send_response(200)
            if self.path != '/close-delimited':
                self.send_header('Content-Length', '100' if self.path == '/slow' else '5')
            if self.path in ('/empty-fixed', '/truncated-fixed', '/close-delimited'):
                self.close_connection = True
            self.end_headers()
            if self.path == '/slow':
                time.sleep(.6)
            elif self.path == '/empty-fixed':
                pass
            elif self.path == '/truncated-fixed':
                self.wfile.write(b'hi')
            else:
                self.wfile.write(b'hello')
        def log_message(self, *args):
            pass
    instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    yield instance, requests
    instance.shutdown()
    instance.server_close()


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/x',
    'http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/',
    'http://[::1]/', 'http://[fc00::1]/', 'http://[::ffff:127.0.0.1]/',
    'http://user:secret@example.com/'])
def test_unsafe_urls_are_rejected(url):
    from url_policy import UrlPolicy, UnsafeUrl
    with pytest.raises(UnsafeUrl):
        UrlPolicy().resolve(url)


@pytest.mark.parametrize('address', [
    '64:ff9b::a00:1', '64:ff9b::808:808', '64:ff9b:1::a00:1',
    '::ffff:8.8.8.8', '2002:c0a8:101::',
    # Teredo: public server/private client, then private server/public client.
    '2001:0:808:808:0:ffff:f5ff:fffe',
    '2001:0:a00:1:0:ffff:f7f7:f7f7',
])
def test_ipv4_embedding_cannot_bypass_public_destination_policy(address):
    from url_policy import UrlPolicy, UnsafeUrl
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, port, 0, 0))]
    policy = UrlPolicy(resolver=resolve, test_origins={'http://fixture.example:8765'})
    with pytest.raises(UnsafeUrl):
        policy.resolve('http://station.example/')
    # The explicit fixture exception still applies to exactly one origin.
    assert policy.resolve('http://fixture.example:8765/live').addresses[0][3][0] == address
    with pytest.raises(UnsafeUrl):
        policy.resolve('http://fixture.example:8766/live')


@pytest.mark.parametrize('address', ['2606:4700:4700::1111', '2002:808:808::'])
def test_public_native_and_6to4_ipv6_destinations_remain_usable(address):
    from url_policy import UrlPolicy
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, port, 0, 0))]
    assert UrlPolicy(resolver=resolve).resolve('https://station.example/').addresses[0][3][0] == address


@pytest.mark.parametrize('status_line', [b'ICY 200 OK', b'HTTP/1.0 200 OK', b'HTTP/1.1 200 OK'])
def test_radio_status_line_preserves_audio_and_metadata(server,status_line):
    from icy import IcyDecoder
    from station_resolver import resolve_station
    server[0].status_line = status_line
    client, origin = local_client(server[0])
    source = resolve_station(origin + '/legacy-icy', client, threading.Event())
    assert source.protocol == 'continuous'
    metadata = []
    decoder = IcyDecoder(int(source.response.headers['icy-metaint']), metadata.append)
    audio = decoder.feed(source.initial_bytes)
    with source.response as response:
        assert response.status == 200
        while True:
            block = response.read(3)
            if not block:
                break
            audio += decoder.feed(block)
    decoder.finish()
    assert audio == b'abcdefgh'
    assert metadata == [{'StreamTitle': 'Example'}]
    assert server[1][0][1]['Icy-MetaData'] == '1'


@pytest.mark.parametrize('status_line', [b'ICY nope OK', b'ICY 099 Bad', b'ICY 1000 Bad',
    b'ICYY 200 OK', b'icy 200 OK', b'ICY 200 ' + b'x' * 65536])
def test_malformed_legacy_status_is_rejected(server,status_line):
    from http_client import HttpError
    server[0].status_line = status_line
    client, origin = local_client(server[0])
    with pytest.raises(HttpError):
        client.open(origin + '/legacy-icy', {}, threading.Event())


def test_private_dns_and_mixed_answers_rejected():
    from url_policy import UrlPolicy, UnsafeUrl
    def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 80))]
    with pytest.raises(UnsafeUrl):
        UrlPolicy(resolver=resolve).resolve('http://station.example/')


def local_client(server, **kwargs):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    origin = 'http://127.0.0.1:%s' % server.server_port
    return HttpClient(UrlPolicy(frozenset([origin])), **kwargs), origin


def test_response_context_and_bounded_reads(server):
    from http_client import read_bounded
    client, origin = local_client(server[0])
    with client.open(origin + '/', {}, threading.Event()) as response:
        assert response.status == 200
        assert response.final_url == origin + '/'
        assert read_bounded(response, 5) == b'hello'
    assert response.closed


def test_oversized_response_closes(server):
    from http_client import read_bounded, ResponseTooLarge
    client, origin = local_client(server[0])
    with client.open(origin + '/', {}, threading.Event()) as response:
        with pytest.raises(ResponseTooLarge):
            read_bounded(response, 4)
    assert response.closed


def test_private_redirect_is_revalidated(server):
    from url_policy import UnsafeUrl
    client, origin = local_client(server[0])
    with pytest.raises(UnsafeUrl):
        client.open(origin + '/redirect-private', {}, threading.Event())
    assert len(server[1]) == 1


def test_ad_origin_redirect_escape_rejected_before_follow(server):
    from http_client import HttpError
    client, base = local_client(server[0])
    with pytest.raises(HttpError, match='Advertising origin rejected'):
        client.open(base + '/redirect-host', {}, threading.Event(), allowed_origin=base)
    assert [request[0] for request in server[1]] == ['/redirect-host']


def test_ad_redirect_url_length_rejected(server):
    from http_client import HttpError
    client, base = local_client(server[0])
    with pytest.raises(HttpError):
        client.open(base + '/oversized-ad-redirect', {}, threading.Event(), allowed_origin=base)
    assert [request[0] for request in server[1]] == ['/oversized-ad-redirect']


def test_socket_uses_validated_address_without_dns_rebinding(server):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    resolutions, destinations = [], []
    def changing_dns(host, port, **kwargs):
        address = '8.8.8.8' if not resolutions else '10.0.0.1'
        resolutions.append(address)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]
    class RoutedSocket(socket.socket):
        def connect(self, address):
            destinations.append(address)
            return super().connect(('127.0.0.1', server[0].server_port))
    client = HttpClient(UrlPolicy(resolver=changing_dns), socket_factory=RoutedSocket)
    with client.open('http://radio.example:80/', {}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert resolutions == ['8.8.8.8']
    assert destinations == [('8.8.8.8', 80)]


@pytest.mark.parametrize('url,expected_host,expected_sni', [
    ('http://radio.example/', 'radio.example', []),
    ('http://radio.example:80/', 'radio.example', []),
    ('https://radio.example/', 'radio.example', ['radio.example']),
    ('https://radio.example:443/', 'radio.example', ['radio.example']),
    ('https://radio.example:8443/', 'radio.example:8443', ['radio.example']),
    ('https://radio.example:80/', 'radio.example:80', ['radio.example']),
    ('http://radio.example:443/', 'radio.example:443', []),
    ('https://[2606:4700:4700::1111]/', '[2606:4700:4700::1111]', ['2606:4700:4700::1111']),
    ('https://[2606:4700:4700::1111]:8443/', '[2606:4700:4700::1111]:8443', ['2606:4700:4700::1111']),
])
def test_request_preserves_original_host_and_tls_name(server,url,expected_host,expected_sni):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    hostnames = []
    class Context:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname):
            hostnames.append(server_hostname)
            return sock  # Offline fixture checks SNI boundary; real default context verifies certificates.
    def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
    class RoutedSocket(socket.socket):
        def connect(self, address):
            return super().connect(('127.0.0.1', server[0].server_port))
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=RoutedSocket, ssl_context=Context())
    with client.open(url, {'Host': 'untrusted.example'}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert hostnames == expected_sni
    assert server[1][-1][1]['Host'] == expected_host
    assert HttpClient().ssl_context.check_hostname
    assert HttpClient().ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_cross_origin_redirect_strips_credentials(server):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
    class RoutedSocket(socket.socket):
        def connect(self, address):
            return super().connect(('127.0.0.1', server[0].server_port))
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=RoutedSocket)
    with client.open('http://radio.example:%s/redirect-host' % server[0].server_port,
                     {'Authorization': 'Bearer secret', 'Cookie': 'secret', 'X-Test': 'kept'}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    headers = {k.lower(): v for k, v in server[1][-1][1].items()}
    assert 'authorization' not in headers and 'cookie' not in headers
    assert headers['x-test'] == 'kept'
    assert headers['host'].startswith('other.example:')


def test_read_timeout(server):
    client, origin = local_client(server[0], read_timeout=.05)
    with client.open(origin + '/slow', {}, threading.Event()) as response:
        with pytest.raises(TimeoutError):
            response.read(10)


def test_total_deadline_survives_redirects_and_trickle(server):
    from http_client import read_bounded
    client, base = local_client(server[0], read_timeout=1)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with client.open(base + '/deadline-redirect', {}, threading.Event(),
                         deadline=started + .25) as response:
            read_bounded(response, 256)
    assert time.monotonic() - started < .7
    assert [request[0] for request in server[1]] == ['/deadline-redirect', '/deadline-trickle']


def test_cancellation_interrupts_read_and_closes(server):
    from http_client import Cancelled
    client, origin = local_client(server[0])
    cancel = threading.Event()
    response = client.open(origin + '/slow', {}, cancel)
    timer = threading.Timer(.05, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        response.read(10)
    timer.join()
    assert time.monotonic() - started < .4
    assert response.closed


def test_already_cancelled_never_connects():
    from http_client import HttpClient, Cancelled
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        HttpClient().open('http://8.8.8.8/', {}, cancel)


def test_dns_timeout_bounds_open_and_cancellation():
    from http_client import HttpClient, Cancelled
    from url_policy import UrlPolicy
    entered = threading.Event()
    release = threading.Event()
    def stuck_dns(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))]
    try:
        client = HttpClient(UrlPolicy(resolver=stuck_dns), connect_timeout=.05)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.open('http://radio.example/', {}, threading.Event())
        assert time.monotonic() - started < .4
        assert entered.is_set()
        cancel = threading.Event()
        timer = threading.Timer(.05, cancel.set)
        timer.start()
        with pytest.raises(Cancelled):
            HttpClient(UrlPolicy(resolver=stuck_dns)).open('http://radio.example/', {}, cancel)
        timer.join()
    finally:
        release.set()


def test_pre_request_ipv6_failure_falls_back_to_validated_ipv4(server):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    destinations = []
    def dns(host, port, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:4860:4860::8888', port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
    class RoutedSocket(socket.socket):
        def connect(self, address):
            destinations.append(address)
            if self.family == socket.AF_INET6:
                raise OSError('IPv6 unavailable')
            return super().connect(('127.0.0.1', server[0].server_port))
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=RoutedSocket)
    with client.open('http://radio.example/', {}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert destinations == [('2001:4860:4860::8888', 80, 0, 0), ('8.8.8.8', 80)]
    assert len(server[1]) == 1


def test_failure_during_request_transmission_never_tries_next_address(server):
    from http_client import HttpClient
    from url_policy import UrlPolicy
    destinations = []
    def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.4.4', port))]
    class UncertainSocket(socket.socket):
        def connect(self, address):
            destinations.append(address)
            return super().connect(('127.0.0.1', server[0].server_port))
        def sendall(self, data, *args):
            super().sendall(data, *args)
            raise OSError('Request may have arrived')
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=UncertainSocket)
    with pytest.raises(OSError, match='Request may have arrived'):
        client.open('http://radio.example/', {}, threading.Event())
    assert destinations == [('8.8.8.8', 80)]


def test_address_fallback_shares_one_connect_deadline():
    from http_client import HttpClient
    from url_policy import UrlPolicy
    timeouts = []
    def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.4.4', port))]
    class FailingSocket(socket.socket):
        def connect(self, address):
            timeouts.append(self.gettimeout())
            time.sleep(.04)
            raise OSError('Connection refused')
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=FailingSocket, connect_timeout=.1)
    with pytest.raises(OSError):
        client.open('http://radio.example/', {}, threading.Event())
    assert len(timeouts) == 2
    assert 0 < timeouts[1] < timeouts[0] - .02


@pytest.mark.parametrize('path,prefix', [('/empty-fixed', b''), ('/truncated-fixed', b'hi')])
def test_fixed_length_premature_eof_raises_and_closes(server, path, prefix):
    from http_client import HttpError
    client, origin = local_client(server[0])
    response = client.open(origin + path, {}, threading.Event())
    received = bytearray()
    with pytest.raises(HttpError, match='Incomplete HTTP response'):
        while True:
            block = response.read(64)
            if not block:
                break
            received.extend(block)
    assert bytes(received) == prefix and response.closed


def test_zero_size_read_and_close_delimited_eof_remain_valid(server):
    from http_client import read_bounded
    client, origin = local_client(server[0])
    with client.open(origin + '/', {}, threading.Event()) as response:
        assert response.read(0) == b''
        assert read_bounded(response) == b'hello'
    with client.open(origin + '/close-delimited', {}, threading.Event()) as response:
        assert read_bounded(response) == b'hello'


def test_malformed_status_is_safe_http_error_without_address_retry(server):
    from http_client import HttpClient, HttpError
    from url_policy import UrlPolicy
    destinations = []
    def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ('8.8.8.8', '8.8.4.4')]
    class RoutedSocket(socket.socket):
        def connect(self, address):
            destinations.append(address)
            return super().connect(('127.0.0.1', server[0].server_port))
    client = HttpClient(UrlPolicy(resolver=dns), socket_factory=RoutedSocket)
    with pytest.raises(HttpError) as error:
        client.open('http://radio.example/bad-status', {}, threading.Event())
    assert 'secret' not in str(error.value)
    assert destinations == [('8.8.8.8', 80)]
    assert len(server[1]) == 1


def test_malformed_response_allows_discovery_failover_but_click_is_single_attempt(server):
    from radio_browser import RadioBrowser
    client, base = local_client(server[0])
    rb = RadioBrowser(client, mirror_lookup=lambda: [base + '/bad-status', base])
    assert rb.countries() == []
    assert len(server[1]) == 2
    rb = RadioBrowser(client, mirror_lookup=lambda: [base + '/bad-status', base])
    rb.click('station')
    assert len(server[1]) == 3


def test_malformed_chunk_body_is_safe_http_error_and_closes(server):
    from http_client import HttpError, read_bounded
    client, base = local_client(server[0])
    response = client.open(base + '/bad-chunk', {}, threading.Event())
    with pytest.raises(HttpError) as error:
        read_bounded(response)
    assert response.closed and 'secret' not in str(error.value)


@pytest.mark.parametrize('protocol_error', ['HTTPException', 'LineTooLong', 'BadStatusLine'])
def test_other_read_protocol_errors_are_normalized_and_closed(protocol_error):
    import http.client
    from http_client import HttpError, Response
    class Raw:
        status, length = 200, None
        def getheaders(self):
            return []
        def read1(self, size):
            raise getattr(http.client, protocol_error)('secret')
        def close(self):
            pass
    class Lifetime:
        cancel = threading.Event()
        closed = False
        def close(self):
            self.closed = True
    response = Response(Raw(), Lifetime(), 'https://radio.example/')
    with pytest.raises(HttpError) as error:
        response.read(10)
    assert response.closed and 'secret' not in str(error.value)


@pytest.mark.parametrize('target,expected', [
    ('/café/音.mp3?title=été&city=東京', '/caf%C3%A9/%E9%9F%B3.mp3?title=%C3%A9t%C3%A9&city=%E6%9D%B1%E4%BA%AC'),
    ('/a%2fb/音?sig=A%2Bb%2fC%3D&x=:/?@!$&\'()*+,;=[]~',
     '/a%2fb/%E9%9F%B3?sig=A%2Bb%2fC%3D&x=:/?@!$&\'()*+,;=[]~'),
    ('/a%2fb?sig=A%2Bb%2fC%3D&x=:/?@!$&\'()*+,;=[]~',
     '/a%2fb?sig=A%2Bb%2fC%3D&x=:/?@!$&\'()*+,;=[]~'),
])
def test_unicode_request_target_encodes_only_non_ascii(server,target,expected):
    client, origin = local_client(server[0])
    with client.open(origin + target, {}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert server[1][0][0] == expected


def test_relative_hls_unicode_segment_uses_encoded_wire_target(server):
    from hls import parse_playlist
    client, origin = local_client(server[0])
    parsed = parse_playlist('#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\n../音.aac?sig=a%2Fb&title=été\n',
                            origin + '/live/index.m3u8')
    with client.open(parsed.segments[0].uri, {}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert server[1][0][0] == '/%E9%9F%B3.aac?sig=a%2Fb&title=%C3%A9t%C3%A9'
