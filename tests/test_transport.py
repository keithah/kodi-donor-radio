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
            self.send_response(200)
            self.send_header('Content-Length', '100' if self.path == '/slow' else '5')
            self.end_headers()
            if self.path == '/slow':
                time.sleep(.6)
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


def test_tls_checks_original_hostname(server):
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
    with client.open('https://radio.example/', {}, threading.Event()) as response:
        assert response.read(5) == b'hello'
    assert hostnames == ['radio.example']
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
