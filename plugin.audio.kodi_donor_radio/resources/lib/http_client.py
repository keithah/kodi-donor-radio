# SPDX-License-Identifier: GPL-2.0-or-later
"""Small HTTP client with pinned destinations and explicit streaming ownership."""
import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from url_policy import UrlPolicy, origin


CHUNK_BYTES = 64 * 1024
BODY_BYTES = 1024 * 1024
USER_AGENT = 'kodi-donor-radio/0.1.0'
# getaddrinfo cannot be interrupted; cap abandoned system resolver calls.
_DNS_SLOTS = threading.BoundedSemaphore(8)


class Cancelled(OSError):
    pass


class ResponseTooLarge(ValueError):
    pass


class HttpError(OSError):
    pass


class _Lifetime:
    def __init__(self, connection, sock, cancel):
        self.connection, self.sock, self.cancel = connection, sock, cancel
        self.stopped = threading.Event()
        self.closed = False
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()

    def _watch(self):
        while not self.stopped.wait(.02):
            if self.cancel.is_set():
                self.close()
                return

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.connection.close()


class Response:
    """Caller owns this response and must close it, including on session stop."""
    def __init__(self, raw, lifetime, final_url):
        self._raw, self._lifetime = raw, lifetime
        self.status = raw.status
        self.headers = {key.lower(): value for key, value in raw.getheaders()}
        self.final_url = final_url

    @property
    def closed(self):
        return self._lifetime.closed

    def read(self, size):
        if size < 0:
            raise ValueError('A bounded read size is required')
        try:
            if self._lifetime.cancel.is_set():
                raise Cancelled('Request cancelled')
            # read1 returns available audio without waiting for an entire chunk.
            result = self._raw.read1(min(size, CHUNK_BYTES))
            if self._lifetime.cancel.is_set():
                raise Cancelled('Request cancelled')
            return result
        except Exception as error:
            self.close()
            if self._lifetime.cancel.is_set():
                raise Cancelled('Request cancelled') from error
            if isinstance(error, socket.timeout):
                raise TimeoutError('HTTP read timed out') from error
            raise

    def close(self):
        self._lifetime.close()
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def read_bounded(response, limit=BODY_BYTES, initial=b''):
    """Finite-body consumers opt into an aggregate cap; media stays streaming."""
    data = bytearray(initial)
    while len(data) <= limit:
        block = response.read(min(CHUNK_BYTES, limit + 1 - len(data)))
        if not block:
            return bytes(data)
        data.extend(block)
    raise ResponseTooLarge('Response exceeds size limit')


class HttpClient:
    def __init__(self, policy=None, connect_timeout=5, read_timeout=10,
                 max_redirects=5, socket_factory=None, ssl_context=None):
        self.policy = policy or UrlPolicy()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.max_redirects = max_redirects
        self.socket_factory = socket_factory or socket.socket
        self.ssl_context = ssl_context or ssl.create_default_context()
        if not self.ssl_context.check_hostname or self.ssl_context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError('TLS certificate and hostname verification are required')

    def open(self, url, headers, cancel):
        outgoing = {k: v for k, v in headers.items() if k.lower() not in ('host', 'connection', 'accept-encoding')}
        outgoing.setdefault('User-Agent', USER_AGENT)
        outgoing['Accept-Encoding'] = 'identity'
        outgoing['Connection'] = 'close'
        for redirect in range(self.max_redirects + 1):
            if cancel.is_set():
                raise Cancelled('Request cancelled')
            destination = self._resolve(url, cancel)
            if cancel.is_set():
                raise Cancelled('Request cancelled')
            response = self._request(url, destination, outgoing, cancel)
            if response.status not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get('location')
            response.close()
            if not location or redirect >= self.max_redirects:
                raise HttpError('Invalid or excessive redirects')
            next_url = urljoin(url, location)
            if origin(next_url) != origin(url):
                outgoing = {k: v for k, v in outgoing.items() if k.lower() not in
                            ('authorization', 'proxy-authorization', 'cookie', 'cookie2', 'referer')}
            url = next_url
        raise HttpError('Excessive redirects')

    def _resolve(self, url, cancel):
        deadline = time.monotonic() + self.connect_timeout
        while not _DNS_SLOTS.acquire(blocking=False):
            if cancel.wait(.02):
                raise Cancelled('Request cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('DNS resolution timed out')
        done, result = threading.Event(), []
        def lookup():
            try:
                result.append((True, self.policy.resolve(url)))
            except Exception as error:
                result.append((False, error))
            finally:
                _DNS_SLOTS.release()
                done.set()
        threading.Thread(target=lookup, daemon=True).start()
        while not done.wait(min(.02, max(0, deadline - time.monotonic()))):
            if cancel.is_set():
                raise Cancelled('Request cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('DNS resolution timed out')
        if cancel.is_set():
            raise Cancelled('Request cancelled')
        success, value = result[0]
        if not success:
            raise value
        return value

    def _request(self, url, destination, headers, cancel):
        last_error = None
        for family, socktype, proto, sockaddr in destination.addresses:
            if cancel.is_set():
                raise Cancelled('Request cancelled')
            conn = http.client.HTTPConnection(destination.hostname, destination.port, timeout=self.connect_timeout)
            sock = self.socket_factory(family, socktype, proto)
            lifetime = _Lifetime(conn, sock, cancel)
            try:
                sock.settimeout(self.connect_timeout)
                sock.connect(sockaddr)  # Numeric address only; never resolve hostname again.
                if destination.scheme == 'https':
                    sock = self.ssl_context.wrap_socket(sock, server_hostname=destination.hostname)
                    lifetime.sock = sock
                sock.settimeout(self.read_timeout)
                conn.sock = sock
                parts = urlsplit(url)
                path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
                conn.request('GET', path, headers=headers)
                raw = conn.getresponse()
                return Response(raw, lifetime, url)
            except Exception as error:
                lifetime.close()
                if cancel.is_set():
                    raise Cancelled('Request cancelled') from error
                # Do not repeat a GET after bytes may have been sent: click counters
                # and some station URLs have side effects despite the HTTP method.
                last_error = TimeoutError('HTTP request timed out') if isinstance(error, socket.timeout) else error
                break
        raise last_error or HttpError('No usable destination')
