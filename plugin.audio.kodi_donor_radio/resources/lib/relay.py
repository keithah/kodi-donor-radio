# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded loopback relay: observe exactly the bytes delivered to this session."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import re
import secrets
import socket
from socketserver import ThreadingMixIn
import threading
import time
from urllib.parse import urlsplit

from control import ServiceDescriptor
from hls import parse_playlist, rewrite_playlist
from http_client import BODY_BYTES, CHUNK_BYTES, USER_AGENT, read_bounded
from icy import IcyDecoder
from models import Event, Station
from sessions import ResourceLimit, SessionManager


_HEADERS = frozenset({'content-type', 'content-range', 'accept-ranges', 'cache-control',
                      'etag', 'last-modified', 'expires'})
_MEDIA = re.compile(r'^/media/([a-f0-9]{48})/([a-f0-9]{48})$')
_SESSION = re.compile(r'^/sessions/([a-f0-9]{48})$')


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, relay):
        self.relay = relay
        self.slots = threading.BoundedSemaphore(relay.worker_limit)
        self.lock = threading.Lock()
        self.connections, self.workers = set(), set()
        super().__init__(('127.0.0.1', 0), _Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(.1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self.lock:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            with self.lock:
                self.connections.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        worker = threading.current_thread()
        with self.lock:
            self.workers.add(worker)
        try:
            request.settimeout(self.relay.client_timeout)
            super().process_request_thread(request, address)
        finally:
            with self.lock:
                self.connections.discard(request)
                self.workers.discard(worker)
            self.slots.release()

    def service_actions(self):
        self.relay.manager.expire()

    def handle_error(self, request, address):
        pass  # Exception text can contain upstream URLs or credentials.


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'RadioRelay'
    sys_version = ''

    def log_message(self, *args):
        pass

    def _reply(self, status, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/json')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def _valid_target(self):
        # Reject absolute-form proxy targets, query URLs, and browser-origin control.
        return self.path.startswith('/') and not self.path.startswith('//') and '?' not in self.path and '#' not in self.path

    def _authorized(self):
        value = self.headers.get('Authorization', '')
        expected = 'Bearer ' + self.server.relay.descriptor.control_token
        return (not self.headers.get('Origin') and not self.headers.get('Transfer-Encoding')
                and hmac.compare_digest(value.encode(), expected.encode()))

    def do_HEAD(self):
        self._reply(405)

    def do_POST(self):
        if not self._valid_target() or self.path != '/sessions':
            return self._reply(404)
        if not self._authorized():
            return self._reply(401)
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384:
                return self._reply(400)
            body = self.rfile.read(int(lengths[0]))
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {'uuid', 'name', 'url', 'codec', 'hls'}:
                return self._reply(400)
            if (not all(isinstance(data[k], str) and len(data[k]) <= 4096 for k in ('uuid', 'name', 'url', 'codec'))
                    or not isinstance(data['hls'], bool)):
                return self._reply(400)
            session = self.server.relay.manager.create(Station(**data))
        except (ValueError, UnicodeError):
            return self._reply(400)
        except Exception:
            return self._reply(502)
        self._reply(201, {'id': session.id, 'playback_url': session.playback_url})

    def do_DELETE(self):
        match = _SESSION.fullmatch(self.path) if self._valid_target() else None
        if match is None:
            return self._reply(404)
        if not self._authorized():
            return self._reply(401)
        self.server.relay.manager.stop(match.group(1))
        self._reply(204)

    def do_GET(self):
        self._started = False
        if not self._valid_target():
            return self._reply(404)
        if self.path == '/health':
            return self._reply(200, {'status': 'ok', 'instance_id': self.server.relay.descriptor.instance_id})
        match = _MEDIA.fullmatch(self.path)
        if match is None:
            return self._reply(404)
        manager = self.server.relay.manager
        session = manager.get(match.group(1))
        if session is None:
            return self._reply(404)
        try:
            with session.acquire(match.group(2)) as resource:
                if resource.kind == 'continuous':
                    if self.headers.get('Range'):
                        return self._reply(416)
                    if not session.claim_continuous():
                        return self._reply(409)
                    try:
                        self._continuous(session)
                    finally:
                        manager.stop(session.id)
                elif resource.kind in ('hls', 'playlist'):
                    # Fetch order and observation order agree within one rendition.
                    with resource.playlist_lock:
                        if session.cancel.is_set():
                            return
                        self._resource(session, resource)
                else:
                    self._resource(session, resource)
        except KeyError:
            if not self._started:
                self._reply(404)
        except Exception:
            if not self._started:
                self._reply(502)
            self.close_connection = True

    def _check_encoding(self, response):
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Unsupported content encoding')
        # HttpClient removes standard chunk framing; all other codings fail closed.
        if response.headers.get('transfer-encoding', '').lower() not in ('', 'chunked'):
            raise ValueError('Unsupported transfer encoding')

    def _headers(self, status, headers, length=None):
        nominated = {part.strip().lower() for part in headers.get('connection', '').split(',')}
        forwarded = {name: value for name, value in headers.items() if name in _HEADERS and name not in nominated}
        if any('\r' in value or '\n' in value for value in forwarded.values()):
            raise ValueError('Invalid upstream header')
        if 'content-length' in nominated or headers.get('transfer-encoding'):
            length = None
        self.send_response(status)
        for name, value in forwarded.items():
            self.send_header(name.title(), value)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Connection', 'close')
        self.end_headers()
        self._started = True
        self.close_connection = True

    def _continuous(self, session):
        response = session.source.response
        self._check_encoding(response)
        decoder = None
        metaint = response.headers.get('icy-metaint')
        if metaint is not None:
            if not re.fullmatch('[0-9]{1,10}', metaint):
                raise ValueError('Invalid ICY interval')
            def metadata(value):
                session.sink.emit(Event('metadata_observed', session.id, session.station.uuid,
                                        'icy', 'icy', value, datetime.now(timezone.utc).isoformat(), time.monotonic()))
            decoder = IcyDecoder(int(metaint), metadata)
        self._headers(response.status, response.headers)
        block = session.source.initial_bytes
        try:
            while not session.cancel.is_set():
                if block:
                    audio = decoder.feed(block) if decoder is not None else block
                    if audio:
                        self.wfile.write(audio)
                block = response.read(CHUNK_BYTES)
                if not block:
                    break
        finally:
            if decoder is not None:
                decoder.finish()

    def _resource(self, session, resource):
        headers = {'User-Agent': USER_AGENT}
        value = self.headers.get('Range')
        if value:
            if resource.kind in ('hls', 'playlist') or not re.fullmatch(r'bytes=(?:[0-9]+-[0-9]*|-[0-9]+)', value):
                return self._reply(416)
            headers['Range'] = value
        response = self.server.relay.manager.client.open(resource.url, headers, session.cancel)
        try:
            self._check_encoding(response)
            if response.status != 200 and response.status not in (206, 416):
                self._headers(response.status if 400 <= response.status <= 599 else 502, response.headers, 0)
                return
            if response.status == 416:
                content_range = response.headers.get('content-range', '')
                if not re.fullmatch(r'bytes \*/[0-9]+', content_range):
                    raise ValueError('Invalid range response')
                self._headers(416, response.headers, 0)
                return
            is_playlist = resource.kind in ('hls', 'playlist')
            if is_playlist:
                if response.status != 200:
                    raise ValueError('Partial playlist unsupported')
                body = read_bounded(response, BODY_BYTES)
                playlist = parse_playlist(body.decode('utf-8-sig'), response.final_url)
                routes = session.register_many([ref.url for ref in playlist.uri_references],
                                               [ref.kind for ref in playlist.uri_references])
                registered = dict(zip([ref.url for ref in playlist.uri_references], routes))
                rewritten = rewrite_playlist(playlist, registered.__getitem__).encode('utf-8')
                if playlist.segments:
                    # A backwards window that still overlaps is not a restart.
                    if (resource.first_sequence is not None
                            and playlist.segments[-1].sequence < resource.first_sequence):
                        resource.epoch += 1
                    resource.first_sequence = playlist.segments[0].sequence
                session.tracker.observe(playlist, resource.id, resource.epoch)
                self._headers(200, {'content-type': 'application/vnd.apple.mpegurl'}, len(rewritten))
                self.wfile.write(rewritten)
            else:
                length = response.headers.get('content-length')
                if length is not None and not re.fullmatch('[0-9]+', length):
                    raise ValueError('Invalid upstream length')
                if response.status == 206:
                    match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)', response.headers.get('content-range', ''))
                    if match is None or not value:
                        raise ValueError('Invalid range response')
                    start, end, total = map(int, match.groups())
                    if not 0 <= start <= end < total or (length is not None and int(length) != end - start + 1):
                        raise ValueError('Invalid range response')
                # Read first so an immediate upstream failure cannot become a 200.
                block = response.read(CHUNK_BYTES)
                self._headers(response.status, response.headers, int(length) if length is not None else None)
                while block and not session.cancel.is_set():
                    self.wfile.write(block)
                    block = response.read(CHUNK_BYTES)
        finally:
            response.close()


class RelayServer:
    def __init__(self, manager=None, worker_limit=8, client_timeout=10):
        if not 1 <= worker_limit <= 8:
            raise ValueError('Worker limit must be between one and eight')
        self.manager, self.worker_limit = manager or SessionManager(), worker_limit
        self.client_timeout = client_timeout
        self._server, self._thread = None, None
        self.descriptor = None

    def start(self):
        if self._server is not None:
            return self.descriptor
        self._server = _Server(self)
        self.descriptor = ServiceDescriptor(self._server.server_port, secrets.token_hex(24), secrets.token_hex(32))
        self.manager.base_url = 'http://127.0.0.1:%s' % self.descriptor.port
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': .1}, daemon=True)
        self._thread.start()
        return self.descriptor

    def close(self):
        self.manager.close()
        if self._server is None:
            return
        deadline = time.monotonic() + 11
        self._server.shutdown()
        with self._server.lock:
            connections = list(self._server.connections)
            workers = list(self._server.workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._server.server_close()
        self._thread.join(max(0, deadline - time.monotonic()))
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        self._server = None
