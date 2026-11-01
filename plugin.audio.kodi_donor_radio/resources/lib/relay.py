# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded loopback relay: observe exactly the bytes delivered to this session."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import re
import secrets
import select
import socket
from socketserver import TCPServer, ThreadingMixIn
import threading
import time
from urllib.parse import urlsplit

from control import ServiceDescriptor
from hls import parse_playlist, rewrite_playlist
from http_client import BODY_BYTES, CHUNK_BYTES, USER_AGENT, IncompleteResponse, read_bounded
from icy import IcyDecoder
from models import Event, Station
from sessions import ResourceLimit, SessionManager
from ad_session import CompositionFailure, SourceUnavailable
from ad_media import extract_adts


_HEADERS = frozenset({'content-type', 'content-range', 'accept-ranges', 'cache-control',
                      'etag', 'last-modified', 'expires'})
_MEDIA = re.compile(r'^/media/([a-f0-9]{48})/([a-f0-9]{48})(?:\.(?:m3u8|ts|aac|mp3|mp4|m4s|m4a|ac3|ec3))?$')
_SESSION = re.compile(r'^/sessions/([a-f0-9]{48})$')
_FIXTURE_LAUNCH = re.compile(r'^/fixture-launches/([a-f0-9]{48})$')
_AD_RELOAD_INTERVAL = .5
_MAX_COMPOSITION_RELOAD_FAILURES = 3


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

    def server_bind(self):
        # HTTPServer resolves getfqdn() during bind. Our numeric loopback-only
        # listener needs no DNS, which can otherwise stall service startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

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
        return ('Origin' not in self.headers and 'Transfer-Encoding' not in self.headers
                and hmac.compare_digest(value.encode(), expected.encode()))

    def do_HEAD(self):
        if not self._valid_target():
            return self._reply(404)
        match = _MEDIA.fullmatch(self.path)
        if match is None:
            return self._reply(405 if not self.path.startswith('/media/') else 404)
        session = self.server.relay.manager.get(match.group(1))
        if session is None:
            return self._reply(404)
        try:
            resource = session.lookup(match.group(2))
            composed_audio = self.path == session.continuous_route
            if (resource is None or session.cancel.is_set()
                    or (not composed_audio and self.path != session.route(resource.id))):
                return self._reply(404)
            # Metadata lookup must not acquire/claim the stream, fetch, or observe cues.
            if composed_audio:
                headers = {'content-type': 'audio/aac'}
            elif resource.kind == 'continuous':
                headers = session.source.response.headers
            elif resource.kind in ('hls', 'playlist', 'audio_master'):
                headers = {'content-type': 'application/vnd.apple.mpegurl'}
            elif resource.kind == 'cached':
                ad = session.ad
                if ad is not None:
                    data = ad.read(resource.cache_id)
                    self._headers(200, {'content-type': 'video/mp2t'}, len(data))
                    return
                return self._reply(405)  # Segment/map metadata is not retained.
            else:
                return self._reply(405)  # Segment/map metadata is not retained.
            # HEAD ignores Range; no transformed body length is known here.
            self._headers(200, headers)
        except KeyError:
            self._reply(404)
        except ValueError:
            self._reply(502)

    def do_POST(self):
        if not self._valid_target() or self.path not in ('/sessions', '/preview-sessions', '/icy-marker/learn', '/icy-marker/label', '/fixture-launches', '/fixture-clock/qualify'):
            return self._reply(404)
        if not self._authorized():
            return self._reply(401)
        if self.path in ('/icy-marker/learn', '/icy-marker/label'):
            return self._learn_icy_marker()
        if self.path in ('/fixture-launches', '/fixture-clock/qualify'):
            return self._fixture_launch_control()
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384:
                return self._reply(400)
            body = self.rfile.read(int(lengths[0]))
            data = json.loads(body)
            fields = {'uuid', 'name', 'url', 'codec', 'hls'}
            if not isinstance(data, dict) or set(data) not in (fields, fields | {'fixture_launch_nonce'}):
                return self._reply(400)
            if (not all(isinstance(data[k], str) and len(data[k]) <= 4096 for k in ('uuid', 'name', 'url', 'codec'))
                    or not isinstance(data['hls'], bool)):
                return self._reply(400)
            nonce = data.pop('fixture_launch_nonce', None)
            if nonce is not None and (self.path != '/sessions' or not isinstance(nonce, str)
                                      or re.fullmatch(r'[a-f0-9]{48}', nonce) is None):
                return self._reply(400)
            station = Station(**data)
            if self.path == '/preview-sessions':
                session = self.server.relay.manager.create_preview(station)
            else:
                session = self.server.relay.manager.create(station, fixture_launch_nonce=nonce)
        except (ValueError, UnicodeError):
            return self._reply(400)
        except Exception:
            return self._reply(502)
        # Resolve from the fetched response, never Radio Browser codec/HLS hints.
        mime = ('application/vnd.apple.mpegurl' if session.source.protocol == 'hls'
                else session.source.response.headers.get('content-type', '').split(';', 1)[0].strip().lower())
        if len(mime) > 128 or not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', mime):
            mime = 'application/octet-stream'
        result = {'id': session.id, 'playback_url': session.playback_url, 'mime_type': mime}
        if session.adaptive_playback_url is not None:
            result['adaptive_playback_url'] = session.adaptive_playback_url
        if session.continuous_playback_url is not None:
            result['continuous_playback_url'] = session.continuous_playback_url
        self._reply(201, result)

    def _fixture_launch_control(self):
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 512:
                return self._reply(400)
            body = self.rfile.read(int(lengths[0]))
            if len(body) != int(lengths[0]):
                return self._reply(400)
            data = json.loads(body)
            fields = {'fixture_id'} if self.path == '/fixture-launches' else {'session_id', 'nonce'}
            if not isinstance(data, dict) or set(data) != fields or any(
                    not isinstance(data[key], str) for key in fields):
                return self._reply(400)
            manager = self.server.relay.manager
            if self.path == '/fixture-launches':
                nonce = manager.reserve_fixture_launch(data['fixture_id'])
                return self._reply(201, {'nonce': nonce})
            if any(re.fullmatch(r'[a-f0-9]{48}', data[key]) is None for key in fields):
                return self._reply(400)
            qualified = manager.qualify_fixture_clock(data['session_id'], data['nonce'])
        except (ValueError, UnicodeError, RecursionError):
            return self._reply(400)
        return self._reply(204 if qualified else 409)

    def _learn_icy_marker(self):
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 4096:
                return self._reply(400)
            body = self.rfile.read(int(lengths[0]))
            if len(body) != int(lengths[0]):
                return self._reply(400)
            data = json.loads(body)
            labeling = self.path == '/icy-marker/label'
            expected = {'session_id', 'marker', 'action'} | ({'remember'} if labeling else set())
            if (not isinstance(data, dict) or set(data) != expected
                    or not all(isinstance(data[key], str) for key in ('session_id', 'marker', 'action'))
                    or (labeling and type(data['remember']) is not bool)
                    or not re.fullmatch(r'[a-f0-9]{48}', data['session_id'])):
                return self._reply(400)
            if labeling:
                learned = self.server.relay.manager.label_icy_marker(**data)
            else:
                learned = self.server.relay.manager.learn_icy_marker(**data)
        except (ValueError, UnicodeError, RecursionError):
            return self._reply(400)
        except OSError:
            return self._reply(502)
        self._reply(204 if learned else 409)

    def do_DELETE(self):
        launch = _FIXTURE_LAUNCH.fullmatch(self.path) if self._valid_target() else None
        if launch is not None:
            if not self._authorized():
                return self._reply(401)
            self.server.relay.manager.cancel_fixture_launch(launch.group(1))
            return self._reply(204)
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
        if self.path == '/icy-marker/candidate':
            if not self._authorized():
                return self._reply(401)
            return self._reply(200, self.server.relay.manager.current_icy_candidate())
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
                composed_audio = self.path == session.continuous_route
                if not composed_audio and self.path != session.route(resource.id):
                    return self._reply(404)
                if composed_audio:
                    ranges = self.headers.get_all('Range', [])
                    if len(ranges) > 1 or (ranges and ranges[0] != 'bytes=0-'):
                        return self._reply(416)
                    if not session.claim_continuous():
                        return self._reply(409)
                    try:
                        self._composed_audio(session, resource)
                        if not self._started:
                            self._reply(503 if session.cancel.is_set() else 502)
                    finally:
                        session.finish_continuous_audio()
                        manager.stop(session.id)
                elif resource.kind == 'continuous':
                    if self.headers.get('Range') not in (None, 'bytes=0-'):
                        return self._reply(416)
                    if not session.claim_continuous():
                        return self._reply(409)
                    try:
                        self._continuous(session)
                    finally:
                        manager.stop(session.id)
                elif resource.kind in ('hls', 'playlist', 'audio_master'):
                    # Fetch order and observation order agree within one rendition.
                    audio_master = resource.kind == 'audio_master'
                    if audio_master:
                        resource = session.lookup(session.playback_url)
                    with resource.playlist_lock:
                        if session.cancel.is_set():
                            return self._reply(404)
                        ad = session.ad
                        if ad is not None and ad.terminal:
                            return self._reply(503)
                        self._resource(session, resource, audio_master=audio_master)
                elif resource.kind == 'cached':
                    self._cached(session, resource)
                else:
                    self._resource(session, resource)
        except KeyError:
            if not self._started:
                self._reply(404)
        except IncompleteResponse:
            # Fixed schema and reason only: origin exceptions can contain secrets.
            session.sink.emit(Event('stream_error', session.id, session.station.uuid,
                                    session.source.protocol, 'http', {},
                                    datetime.now(timezone.utc).isoformat(), time.monotonic(),
                                    {'reason': 'incomplete_upstream_body'}))
            if not self._started:
                self._reply(502)
            self.close_connection = True
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
                session.observe_icy_metadata(value, datetime.now(timezone.utc).isoformat(),
                                             time.monotonic())
            # A zero interval denotes an unframed audio body (no metadata).
            if int(metaint) > 0:
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

    def _cached(self, session, resource):
        ad = session.ad
        if ad is None:
            return self._reply(404)
        data = ad.read(resource.cache_id)
        size = len(data)
        value = self.headers.get('Range')
        status, start, end = 200, 0, size - 1
        if value is not None:
            match = re.fullmatch(r'bytes=([0-9]+)-([0-9]*)', value)
            suffix = re.fullmatch(r'bytes=-([0-9]+)', value)
            if match:
                start = int(match.group(1))
                end = min(size - 1, int(match.group(2))) if match.group(2) else size - 1
            elif suffix and int(suffix.group(1)) > 0:
                start = max(0, size - int(suffix.group(1)))
            else:
                start = size
            if start > end or start >= size:
                self._headers(416, {'content-range': 'bytes */%d' % size}, 0)
                return
            status = 206
        headers = {'content-type': 'video/mp2t', 'accept-ranges': 'bytes'}
        if status == 206:
            headers['content-range'] = 'bytes %d-%d/%d' % (start, end, size)
        self._headers(status, headers, end - start + 1)
        self.wfile.write(data[start:end + 1])

    def _observe_playlist(self, session, resource, response):
        body = read_bounded(response, BODY_BYTES)
        playlist = parse_playlist(body.decode('utf-8-sig'), response.final_url)
        if playlist.segments:
            # A backwards window that still overlaps is not a restart.
            if (resource.first_sequence is not None
                    and playlist.segments[-1].sequence < resource.first_sequence):
                resource.epoch += 1
            resource.first_sequence = playlist.segments[0].sequence
        session.tracker.observe(playlist, resource.id, resource.epoch)
        return playlist

    def _reload_composition(self, session, resource, ad):
        # The caller owns the same lock as native/Adaptive manifest requests.
        with self.server.relay.manager.client.open(
                resource.url, {'User-Agent': USER_AGENT}, session.cancel,
                allowed_origin=session.source.final_url) as response:
            self._check_encoding(response)
            if response.status != 200:
                raise SourceUnavailable('source_unavailable')
            playlist = self._observe_playlist(session, resource, response)
            ad.render(playlist, session.cached_route)
            session.retire_cached()

    def _composed_audio(self, session, resource):
        """Send one immutable AAC segment at a time, starting at sample zero.

        Only bounded output metadata survives each iteration. The next payload
        is copied while the root playlist lock prevents retirement, then sent
        without that lock. Socket writes establish delivery bounds, never a
        playback clock or an advertising success event.
        """
        ad = session.ad
        if ad is None:
            raise CompositionFailure('composition_unavailable')
        sequence = sample = 0
        known = {}
        reloaded_at = None
        reload_failures = 0
        while not session.cancel.is_set():
            audio = None
            with resource.playlist_lock:
                if session.cancel.is_set():
                    return
                if session.ad is not ad or ad.terminal:
                    raise CompositionFailure('composition_unavailable')
                presentation = ad.presentation()
                segments = presentation.segments
                if len(segments) > session.resource_limit:
                    raise CompositionFailure('continuous_output_limit')
                current = {}
                previous = None
                for item in segments:
                    if (item.samples <= 0 or item.samples % 1024
                            or item.sequence in current
                            or (previous is not None and
                                (item.sequence != previous.sequence + 1
                                 or item.start_sample != previous.start_sample + previous.samples))):
                        raise CompositionFailure('continuous_output_gap')
                    if item.sequence in known and known[item.sequence] != item:
                        raise CompositionFailure('continuous_output_changed')
                    current[item.sequence] = item
                    previous = item
                known = current
                item = current.get(sequence)
                if item is not None:
                    if item.start_sample != sample:
                        raise CompositionFailure('continuous_output_gap')
                    info, audio = extract_adts(bytes(ad.read(item.resource_id)))
                    if (info.samples != item.samples or info.sample_rate != 48000
                            or info.channels != 2 or info.codec != 'aac'):
                        raise CompositionFailure('continuous_output_changed')
                elif segments and segments[0].sequence > sequence:
                    raise CompositionFailure('continuous_output_retired')
                elif presentation.ended:
                    if not segments or segments[-1].sequence + 1 != sequence:
                        raise CompositionFailure('continuous_output_gap')
                    return
                elif not segments and sequence:
                    raise CompositionFailure('continuous_output_retired')
                elif reloaded_at is None or time.monotonic() - reloaded_at >= _AD_RELOAD_INTERVAL:
                    try:
                        self._reload_composition(session, resource, ad)
                    except (SourceUnavailable, OSError):
                        if session.cancel.is_set():
                            return
                        reload_failures += 1
                        reloaded_at = time.monotonic()
                        if reload_failures >= _MAX_COMPOSITION_RELOAD_FAILURES:
                            raise
                    else:
                        reload_failures = 0
                        reloaded_at = time.monotonic()
                    continue
            if audio is not None:
                if session.cancel.is_set():
                    return
                if not self._started:
                    self._headers(200, {'content-type': 'audio/aac'})
                self.wfile.write(audio)
                del audio
                if not session.record_continuous_delivery(item):
                    return
                sequence += 1
                sample += item.samples
                continue
            # Cancellation interrupts idle waits. A closed consumer must not
            # leave a worker polling a stalled source indefinitely.
            if select.select([self.connection], [], [], 0)[0]:
                if not self.connection.recv(1, socket.MSG_PEEK):
                    return
            remaining = _AD_RELOAD_INTERVAL - (time.monotonic() - reloaded_at)
            session.cancel.wait(max(0, min(.1, remaining)))

    def _resource(self, session, resource, audio_master=False):
        if audio_master and session.ad is None:
            return self._reply(502)
        headers = {'User-Agent': USER_AGENT}
        value = self.headers.get('Range')
        if value:
            if resource.kind in ('hls', 'playlist'):
                # Rewritten manifests have no upstream byte correspondence.
                # Media clients commonly probe the whole body with this range.
                if value != 'bytes=0-':
                    return self._reply(416)
            else:
                if not re.fullmatch(r'bytes=(?:[0-9]+-[0-9]*|-[0-9]+)', value):
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
                playlist = self._observe_playlist(session, resource, response)
                ad = session.ad
                if ad is not None and resource.kind == 'hls' and not playlist.is_master:
                    try:
                        rendered = ad.render(playlist, session.cached_route)
                    except SourceUnavailable:
                        if ad.committed:
                            # A transient fetch must not destroy an already
                            # published ad timeline; Kodi can reload it.
                            return self._reply(503)
                        ad.close()
                        if session.ad is ad:
                            session.ad = None
                    except Exception:
                        if ad.committed:
                            ad.terminate()
                            return self._reply(503)
                        ad.close()
                        if session.ad is ad:
                            session.ad = None
                    else:
                        session.retire_cached()
                        if audio_master:
                            # render() has verified every source and creative as
                            # clear AAC-LC, 48 kHz stereo MPEG-TS. Never apply this
                            # codec declaration to the native fallback below.
                            segments = ad.presentation().segments
                            bandwidth = max((len(ad.read(segment.resource_id)) * 8 * 48000
                                             + segment.samples - 1) // segment.samples
                                            for segment in segments)
                            rendered = ('#EXTM3U\n#EXT-X-VERSION:3\n'
                                        '#EXT-X-STREAM-INF:BANDWIDTH=%d,CODECS="mp4a.40.2"\n'
                                        '%s\n' % (bandwidth, session.playback_url)).encode('utf-8')
                        self._headers(200, {'content-type': 'application/vnd.apple.mpegurl'}, len(rendered))
                        self.wfile.write(rendered)
                        return
                if audio_master:
                    return self._reply(502)
                routes = session.register_many([ref.url for ref in playlist.uri_references],
                                               [ref.kind for ref in playlist.uri_references])
                registered = dict(zip([ref.url for ref in playlist.uri_references], routes))
                rewritten = rewrite_playlist(playlist, registered.__getitem__).encode('utf-8')
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
