import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ad_media
from ad_media import CreativeCache, inspect_ts
from ad_models import AdError, ResolvedAd


ASSETS = None


@pytest.fixture(scope='module', autouse=True)
def _use_generated_ad_assets(ad_assets):
    global ASSETS
    ASSETS = ad_assets


def segment(name='replacement000.ts'):
    return (ASSETS / name).read_bytes()


def ad(url='http://127.0.0.1:8766/ads/replacement.m3u8', duration=4.0):
    return ResolvedAd('ad', url, duration, (), ())


def test_real_segments_measure_frames_and_contiguous_pts():
    first, second = (inspect_ts(segment('replacement%03d.ts' % n)) for n in (0, 1))
    assert (first.codec, first.sample_rate, first.channels, first.samples) == ('aac', 48000, 2, 96256)
    assert second.first_pts == first.last_pts + 1920
    assert second.samples == 96256


def test_null_packets_with_arbitrary_continuity_counters_are_ignored():
    original = segment()
    null_packet = lambda counter: bytes((0x47, 0x1f, 0xff, 0x10 | counter)) + b'\xff' * 184
    padded = null_packet(3) + null_packet(12) + original
    assert inspect_ts(padded) == inspect_ts(original)


def test_ts_rejects_truncation_continuity_and_extra_stream():
    original = segment()
    with pytest.raises(AdError):
        inspect_ts(original[:-1])
    changed = bytearray(original)
    # Audio PID 0x100, second payload packet has a continuity counter.
    audio_packets = [i for i in range(0, len(changed), 188) if changed[i + 1] & 0x1f == 1 and changed[i + 2] == 0]
    changed[audio_packets[1] + 3] ^= 1
    with pytest.raises(AdError):
        inspect_ts(bytes(changed))
    changed = bytearray(original)
    pmt = 2 * 188
    assert changed[pmt + 1:pmt + 3] == b'P\x00'
    changed[pmt + 17] = 0x1b  # AAC elementary stream becomes H.264.
    section_start = pmt + 5
    section_length = ((changed[section_start + 1] & 0x0f) << 8) | changed[section_start + 2]
    section_end = section_start + 3 + section_length
    changed[section_end - 4:section_end] = ad_media._crc32_mpeg(
        bytes(changed[section_start:section_end - 4])).to_bytes(4, 'big')
    with pytest.raises(AdError, match='Unsupported or ambiguous media streams'):
        inspect_ts(bytes(changed))


def _split_first_audio_packet(body, first_payload_bytes):
    packets = [bytearray(body[i:i + 188]) for i in range(0, len(body), 188)]
    index = next(i for i, p in enumerate(packets) if p[1:3] == b'A\x00' and p[1] & 0x40)
    original = packets[index]
    offset = 5 + original[4]
    payload = bytes(original[offset:])
    head, tail = payload[:first_payload_bytes], payload[first_payload_bytes:]
    assert len(tail) <= 184
    cc = original[3] & 15
    adaptation_len = 183 - len(head)
    packets[index] = bytearray(original[:4] + bytes((adaptation_len, 0)) + b'\xff' * (adaptation_len - 1) + head)
    continuation = bytearray((0x47, 0x01, 0x00, 0x30 | ((cc + 1) & 15)))
    adaptation_len = 183 - len(tail)
    continuation.extend(bytes((adaptation_len, 0)) + b'\xff' * (adaptation_len - 1) + tail)
    packets.insert(index + 1, continuation)
    for packet in packets[index + 2:]:
        if packet[1] & 31 == 1 and packet[2] == 0:
            packet[3] = packet[3] & 0xf0 | ((packet[3] + 1) & 15)
    return b''.join(packets)


@pytest.mark.parametrize('first_payload_bytes', [8, 17])
def test_split_pes_or_adts_header_is_parsed(first_payload_bytes):
    original = segment()
    assert inspect_ts(_split_first_audio_packet(original, first_payload_bytes)) == inspect_ts(original)


def test_adts_profile_and_sample_rate_are_checked():
    original = segment()
    offset = original.index(b'\xff\xf1')
    changed = bytearray(original)
    changed[offset + 2] ^= 0x40  # AAC LC -> AAC Main
    with pytest.raises(AdError):
        inspect_ts(bytes(changed))
    changed = bytearray(original)
    changed[offset + 3] ^= 0x40  # Stereo -> mono.
    with pytest.raises(AdError):
        inspect_ts(bytes(changed))
    changed = bytearray(original)
    changed[offset + 2] ^= 0x04  # 48 kHz -> 44.1 kHz
    with pytest.raises(AdError):
        inspect_ts(bytes(changed))


def _shift_pts(body, shift):
    result = bytearray(body)
    for packet in range(0, len(result), 188):
        if result[packet + 1:packet + 3] != b'A\x00':
            continue
        if not result[packet + 1] & 0x40:
            continue
        offset = packet + 4
        if result[packet + 3] & 0x20:
            offset += 1 + result[offset]
        if result[offset:offset + 3] != b'\0\0\1':
            continue
        pos = offset + 9
        raw = result[pos:pos + 5]
        pts = (((raw[0] >> 1) & 7) << 30) | (raw[1] << 22) | ((raw[2] >> 1) << 15) | (raw[3] << 7) | (raw[4] >> 1)
        pts = (pts + shift) % (1 << 33)
        result[pos:pos + 5] = bytes((0x21 | ((pts >> 29) & 0x0e), pts >> 22 & 255,
                                     ((pts >> 14) & 0xfe) | 1, pts >> 7 & 255,
                                     ((pts << 1) & 0xfe) | 1))
    return bytes(result)


def test_pts_wrap_is_normalized_across_segments(tmp_path):
    first = inspect_ts(segment())
    shift = (1 << 33) - first.last_pts - 960
    bodies = {name: _shift_pts(segment(name), shift)
              for name in ('replacement000.ts', 'replacement001.ts')}
    assert inspect_ts(bodies['replacement000.ts']).last_pts < (1 << 33)
    assert inspect_ts(bodies['replacement001.ts']).first_pts < 1920
    client = Client()
    client.bodies = bodies
    cache = CreativeCache(tmp_path, client, 'http://127.0.0.1:8766')
    prepared = cache.prepare(ad(), threading.Event())
    assert prepared.segments[1].info.first_pts == prepared.segments[0].info.last_pts + 1920
    cache.close()


class Response:
    def __init__(self, body, url, on_read=None):
        self.body, self.final_url, self.status, self.headers = body, url, 200, {}
        self.offset, self.on_read = 0, on_read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, count):
        if self.on_read:
            self.on_read()
        part = self.body[self.offset:self.offset + count]
        self.offset += len(part)
        return part


class Client:
    def __init__(self, manifest=None, on_read=None):
        self.manifest = manifest or '#EXTM3U\n#EXTINF:2.005333,\nreplacement000.ts\n#EXTINF:2.005333,\nreplacement001.ts\n#EXT-X-ENDLIST\n'
        self.on_read = on_read
        self.calls = []

    def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
        self.calls.append((url, deadline, allowed_origin))
        assert deadline is not None and allowed_origin == 'http://127.0.0.1:8766'
        if url.endswith('.m3u8'):
            return Response(self.manifest.encode(), url)
        name = url.rsplit('/', 1)[-1]
        return Response(getattr(self, 'bodies', {}).get(name, segment(name)), url, self.on_read)


def test_complete_cache_is_private_and_pinned(tmp_path):
    client = Client()
    cache = CreativeCache(tmp_path, client, 'http://127.0.0.1:8766')
    prepared = cache.prepare(ad(duration=99), threading.Event())
    assert all(0 < deadline - time.monotonic() <= 10 for _, deadline, _ in client.calls)
    assert prepared.samples == 192512  # Measured duration is authoritative.
    assert cache.total_bytes == len(segment()) * 2 and cache.reserved_bytes == 0
    assert len(prepared.segments) == 2
    assert all(s.path.is_file() and s.path.parent != tmp_path for s in prepared.segments)
    assert cache.read(prepared.segments[0].id) == segment()
    cache.pin(prepared.segments[0].id)
    cache.unpin(prepared.segments[0].id)
    cache.close()
    assert all(not s.path.exists() for s in prepared.segments)


def test_redirected_manifest_resolves_relative_segments_from_final_url(tmp_path):
    origin = 'http://127.0.0.1:8766'
    manifest = ('#EXTM3U\n#EXTINF:2.005333,\nreplacement000.ts\n'
                '#EXT-X-ENDLIST\n').encode()
    client = Client()
    calls = []

    def open_url(url, headers, cancel, deadline=None, allowed_origin=None):
        calls.append(url)
        if url.endswith('.m3u8'):
            return Response(manifest, origin + '/redirected/playlist.m3u8')
        return Response(segment(url.rsplit('/', 1)[-1]), url)

    client.open = open_url
    cache = CreativeCache(tmp_path, client, origin)
    prepared = cache.prepare(ad(origin + '/original/playlist.m3u8'), threading.Event())
    assert calls == [origin + '/original/playlist.m3u8',
                     origin + '/redirected/replacement000.ts']
    assert len(prepared.segments) == 1
    cache.close()


def test_cache_close_releases_session_when_directory_removal_fails(tmp_path, monkeypatch):
    import ad_media

    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    session = cache.session
    assert session in ad_media._ACTIVE_SESSIONS
    calls = []

    def remove(path, ignore_errors=False):
        calls.append(ignore_errors)
        if not ignore_errors:
            raise OSError('directory changed during cleanup')

    monkeypatch.setattr(ad_media.shutil, 'rmtree', remove)
    cache.close()
    assert calls == [True]
    assert session not in ad_media._ACTIVE_SESSIONS


@pytest.mark.parametrize('manifest', [
    '#EXTM3U\n#EXTINF:nan,\nreplacement000.ts\n#EXT-X-ENDLIST\n',
    '#EXTM3U\n#EXTINF:inf,\nreplacement000.ts\n#EXT-X-ENDLIST\n',
    '#EXTM3U\n#EXTINF:2,\nreplacement000.ts\n',
    '#EXTM3U\n#EXTINF:9,\nreplacement000.ts\n#EXT-X-ENDLIST\n',
])
def test_invalid_manifest_never_returns_creative(tmp_path, manifest):
    cache = CreativeCache(tmp_path, Client(manifest), 'http://127.0.0.1:8766')
    with pytest.raises(AdError):
        cache.prepare(ad(), threading.Event())
    assert cache.reserved_bytes == 0
    cache.close()


def test_cancel_during_body_never_publishes(tmp_path):
    from http_client import Cancelled
    stop = threading.Event()
    client = Client(on_read=stop.set)
    cache = CreativeCache(tmp_path, client, 'http://127.0.0.1:8766')
    with pytest.raises(Cancelled):
        cache.prepare(ad(), stop)
    assert not list(tmp_path.rglob('*.ts'))
    cache.close()


def test_segment_and_cache_limits(tmp_path, monkeypatch):
    many = '#EXTM3U\n' + '#EXTINF:2.005333,\nreplacement000.ts\n' * 129 + '#EXT-X-ENDLIST\n'
    cache = CreativeCache(tmp_path, Client(many), 'http://127.0.0.1:8766')
    with pytest.raises(AdError):
        cache.prepare(ad(), threading.Event())
    cache.close()

    import ad_media
    monkeypatch.setattr(ad_media, 'MAX_BYTES', len(segment()) + 10)
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    with pytest.raises(AdError):
        cache.prepare(ad(), threading.Event())
    assert not list(cache.session.glob('*.ts'))
    assert cache.reserved_bytes == 0
    cache.close()

    monkeypatch.setattr(ad_media, 'MAX_BYTES', 32 * 1024 * 1024)
    monkeypatch.setattr(ad_media, 'MAX_SAMPLES', 100000)
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    with pytest.raises(AdError):
        cache.prepare(ad(), threading.Event())
    assert not list(cache.session.glob('*.ts'))
    cache.close()


def test_pinned_entry_survives_cache_eviction(tmp_path, monkeypatch):
    import ad_media
    monkeypatch.setattr(ad_media, 'MAX_BYTES', len(segment()) * 3)
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    first = cache.prepare(ad(), threading.Event())
    cache.unpin(first.segments[1].id)
    cache.prepare(ad(), threading.Event())
    assert cache.read(first.segments[0].id) == segment()
    assert not first.segments[1].path.exists()
    cache.unpin(first.segments[0].id)
    cache.close()


def test_prepare_handoff_is_pinned_before_return(tmp_path, monkeypatch):
    import ad_media
    monkeypatch.setattr(ad_media, 'MAX_BYTES', len(segment()) * 3)
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    prepared = cache.prepare(ad(), threading.Event())
    errors = []
    def compete():
        try:
            cache.prepare(ad(), threading.Event())
        except Exception as error:
            errors.append(error)
    competitor = threading.Thread(target=compete)
    competitor.start()
    competitor.join(2)
    assert not competitor.is_alive() and len(errors) == 1 and isinstance(errors[0], AdError)
    assert all(cache.read(item.id) == segment('replacement%03d.ts' % i)
               for i, item in enumerate(prepared.segments))
    for item in prepared.segments:
        cache.unpin(item.id)
    cache.prepare(ad(), threading.Event())
    cache.close()


def test_concurrent_downloads_share_cache_reservation(tmp_path, monkeypatch):
    import ad_media
    monkeypatch.setattr(ad_media, 'MAX_BYTES', len(segment()) + 100)
    manifest = '#EXTM3U\n#EXTINF:2.005333,\nreplacement000.ts\n#EXT-X-ENDLIST\n'
    cache = CreativeCache(tmp_path, Client(manifest), 'http://127.0.0.1:8766')
    entered, release = threading.Event(), threading.Event()
    real_inspect = ad_media.inspect_ts
    calls = []

    def waiting_inspect(data, *args):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return real_inspect(data, *args)

    monkeypatch.setattr(ad_media, 'inspect_ts', waiting_inspect)
    results = []
    def run():
        try:
            results.append(cache.prepare(ad(), threading.Event()))
        except Exception as error:
            results.append(error)
    first = threading.Thread(target=run)
    first.start()
    assert entered.wait(1)
    assert cache.reserved_bytes == len(segment())
    second = threading.Thread(target=run)
    second.start()
    second.join(1)
    assert not second.is_alive()
    assert len(calls) == 1
    assert cache.total_bytes + cache.reserved_bytes <= ad_media.MAX_BYTES
    release.set()
    first.join(2)
    assert len(results) == 2 and sum(not isinstance(r, Exception) for r in results) == 1
    cache.close()


def test_real_http_deadline_expires_without_publication(tmp_path, monkeypatch):
    import ad_media
    from http_client import HttpClient
    from url_policy import UrlPolicy
    monkeypatch.setattr(ad_media, 'PREPARATION_SECONDS', .1)
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(.3)
            try:
                self.send_response(200)
                self.send_header('Content-Length', '8')
                self.end_headers()
                self.wfile.write(b'#EXTM3U\n')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%d' % server.server_port
    try:
        cache = CreativeCache(tmp_path, HttpClient(policy=UrlPolicy(test_origins={url})), url)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            cache.prepare(ad(url + '/slow.m3u8'), threading.Event())
        assert time.monotonic() - started < .8
        assert not list(cache.session.glob('*.ts'))
        assert cache.total_bytes + cache.reserved_bytes == 0
        cache.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_validation_time_is_inside_preparation_deadline(tmp_path, monkeypatch):
    import ad_media
    monkeypatch.setattr(ad_media, 'PREPARATION_SECONDS', .01)
    original = ad_media.inspect_ts

    def slow_parse(data, check):
        time.sleep(.03)
        return original(data, check)

    monkeypatch.setattr(ad_media, 'inspect_ts', slow_parse)
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    with pytest.raises(TimeoutError):
        cache.prepare(ad(), threading.Event())
    assert cache.total_bytes == cache.reserved_bytes == 0
    assert not list(cache.session.glob('*.ts'))
    cache.close()


def test_active_session_is_not_orphan_cleanup(tmp_path):
    first = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    prepared = first.prepare(ad(), threading.Event())
    second = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    assert first.read(prepared.segments[0].id) == segment()
    first.close()
    second.close()


def test_orphan_cleanup_stays_in_owned_namespace(tmp_path):
    owned = tmp_path / 'controlled-ad-creative-cache' / 'abandoned'
    owned.mkdir(parents=True)
    (owned / 'old.ts').write_bytes(b'old')
    other = tmp_path / 'keep.ts'
    other.write_bytes(b'keep')
    cache = CreativeCache(tmp_path, Client(), 'http://127.0.0.1:8766')
    assert not owned.exists()
    assert other.read_bytes() == b'keep'
    cache.close()


def test_aggregate_parallel_preparation_is_bounded(tmp_path, monkeypatch):
    import ad_media
    monkeypatch.setattr(ad_media, '_PREPARATION_SLOTS', threading.BoundedSemaphore(1))
    entered, release = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert release.wait(2)

    first = CreativeCache(tmp_path, Client(on_read=pause), 'http://127.0.0.1:8766')
    second_client = Client()
    second = CreativeCache(tmp_path, second_client, 'http://127.0.0.1:8766')
    errors = []
    worker = threading.Thread(target=lambda: first.prepare(ad(), threading.Event()))
    worker.start()
    assert entered.wait(1)
    cancel = threading.Event()

    def attempt():
        try:
            second.prepare(ad(), cancel)
        except Exception as error:
            errors.append(error)

    waiter = threading.Thread(target=attempt)
    waiter.start()
    cancel.set()
    waiter.join(1)
    assert not waiter.is_alive() and errors
    assert not second_client.calls
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    first.close()
    second.close()
