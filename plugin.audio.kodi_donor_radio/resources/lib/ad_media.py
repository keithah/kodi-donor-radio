# SPDX-License-Identifier: GPL-2.0-or-later
"""Strict, bounded media validation for controlled AAC MPEG-TS creatives."""
import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

from ad_models import AdError, CachedSegment, MediaInfo, PreparedAd
from http_client import Cancelled
from url_policy import origin


MAX_BYTES = 32 * 1024 * 1024
MAX_SEGMENTS = 128
MAX_SAMPLES = 60 * 48000
MAX_MANIFEST = 256 * 1024
PREPARATION_SECONDS = 10
PTS_MOD = 1 << 33
FRAME_PTS = 1920
_PREPARATION_SLOTS = threading.BoundedSemaphore(2)
_ACTIVE_SESSIONS = set()
_SESSION_LOCK = threading.Lock()


def _bad(reason):
    raise AdError(403, reason)


def _crc32_mpeg(data):
    crc = 0xffffffff
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    return crc


def _section(raw, table_id):
    if len(raw) < 8 or raw[0] != table_id or not raw[1] & 0x80:
        _bad('Unsupported transport table')
    length = ((raw[1] & 15) << 8) | raw[2]
    if length < 9 or length > 1021 or len(raw) < length + 3:
        _bad('Incomplete transport table')
    section = raw[:length + 3]
    if _crc32_mpeg(section) or not section[5] & 1 or section[6:8] != b'\0\0':
        _bad('Invalid transport table')
    return section


def _pts(data):
    if len(data) < 5 or data[0] & 0xf1 != 0x21 or not data[2] & 1 or not data[4] & 1:
        _bad('Invalid audio timestamp')
    return (((data[0] >> 1) & 7) << 30) | (data[1] << 22) | ((data[2] >> 1) << 15) | (data[3] << 7) | (data[4] >> 1)


def _frames(data):
    offset = count = 0
    while offset < len(data):
        if len(data) - offset < 7:
            _bad('Truncated AAC frame')
        b = data[offset:offset + 7]
        if b[0] != 0xff or b[1] & 0xf6 != 0xf0 or (b[2] >> 6) & 3 != 1:
            _bad('Unsupported AAC profile')
        rate = (b[2] >> 2) & 15
        channels = ((b[2] & 1) << 2) | (b[3] >> 6)
        if rate != 3 or channels != 2 or b[6] & 3 != 0:
            _bad('Unsupported AAC format')
        header = 7 if b[1] & 1 else 9
        length = ((b[3] & 3) << 11) | (b[4] << 3) | (b[5] >> 5)
        if length < header or offset + length > len(data):
            _bad('Truncated AAC frame')
        offset += length
        count += 1
    if not count:
        _bad('Empty audio stream')
    return count


def inspect_ts(data: bytes, _check=None) -> MediaInfo:
    """Inspect one complete single-program, AAC-LC MPEG-TS segment."""
    if not data or len(data) % 188:
        _bad('Truncated transport stream')
    payloads = {}
    continuity = {}
    pmt_pid = audio_pid = None
    pes = bytearray()
    pes_started = False
    first_pts = last_pts = None
    expected_next = None
    frame_count = 0

    def finish_pes():
        nonlocal first_pts, last_pts, expected_next, frame_count
        if not pes:
            return
        if len(pes) < 14 or pes[:3] != b'\0\0\1' or pes[3] != 0xc0:
            _bad('Unsupported audio packet')
        packet_length = int.from_bytes(pes[4:6], 'big')
        if packet_length == 0 or len(pes) != 6 + packet_length:
            _bad('Truncated audio packet')
        if pes[6] & 0xc0 != 0x80 or pes[7] != 0x80 or pes[8] != 5:
            _bad('Unsupported audio timestamp')
        timestamp = _pts(pes[9:14])
        if expected_next is not None:
            # Normalize the sole legal 33-bit wrap. Reject discontinuities.
            while timestamp < expected_next - (PTS_MOD // 2):
                timestamp += PTS_MOD
            if timestamp != expected_next:
                _bad('Audio timestamps are discontinuous')
        count = _frames(pes[14:])
        if first_pts is None:
            first_pts = timestamp
        last_pts = timestamp + (count - 1) * FRAME_PTS
        expected_next = timestamp + count * FRAME_PTS
        frame_count += count

    for start in range(0, len(data), 188):
        if _check is not None and start % (188 * 256) == 0:
            _check()
        packet = data[start:start + 188]
        if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xc0:
            _bad('Invalid transport packet')
        pid = ((packet[1] & 31) << 8) | packet[2]
        pusi = bool(packet[1] & 0x40)
        adaptation = (packet[3] >> 4) & 3
        if adaptation == 0:
            _bad('Invalid transport adaptation')
        offset = 4
        if adaptation & 2:
            size = packet[offset]
            offset += size + 1
            if offset > 188:
                _bad('Invalid transport adaptation')
        if not adaptation & 1:
            continue
        cc = packet[3] & 15
        if pid in continuity and cc != (continuity[pid] + 1) & 15:
            _bad('Transport continuity error')
        continuity[pid] = cc
        if pid == 0x1fff:
            continue
        if pid in (0, pmt_pid):
            if pusi:
                pointer = packet[offset]
                offset += 1 + pointer
                if offset > 188:
                    _bad('Invalid transport pointer')
                payloads[pid] = bytearray()
            if pid not in payloads:
                _bad('Missing transport table start')
            payloads[pid].extend(packet[offset:])
            raw = payloads[pid]
            if len(raw) < 3 or len(raw) < 3 + (((raw[1] & 15) << 8) | raw[2]):
                continue
            if pid == 0:
                section = _section(raw, 0)
                if len(section) != 16 or section[8:10] == b'\0\0':
                    _bad('Ambiguous transport program')
                found = ((section[10] & 31) << 8) | section[11]
                if pmt_pid is not None and pmt_pid != found:
                    _bad('Transport program changed')
                pmt_pid = found
            else:
                section = _section(raw, 2)
                pos = 12 + (((section[10] & 15) << 8) | section[11])
                end = len(section) - 4
                streams = []
                while pos < end:
                    if pos + 5 > end:
                        _bad('Invalid transport program')
                    streams.append((section[pos], ((section[pos + 1] & 31) << 8) | section[pos + 2]))
                    pos += 5 + (((section[pos + 3] & 15) << 8) | section[pos + 4])
                if pos != end or len(streams) != 1 or streams[0][0] != 0x0f:
                    _bad('Unsupported or ambiguous media streams')
                found = streams[0][1]
                if audio_pid is not None and audio_pid != found:
                    _bad('Audio stream changed')
                audio_pid = found
            continue
        if pid == 0x11:  # FFmpeg service description table, outside the program.
            continue
        if audio_pid is None or pid != audio_pid:
            _bad('Unexpected transport stream')
        if pusi:
            if pes_started:
                finish_pes()
            pes = bytearray()
            pes_started = True
        if not pes_started:
            _bad('Missing audio packet start')
        pes.extend(packet[offset:])
    if pmt_pid is None or audio_pid is None or not pes_started:
        _bad('Incomplete transport stream')
    finish_pes()
    return MediaInfo('aac', 48000, 2, frame_count * 1024, first_pts, last_pts)


class CreativeCache:
    """Session-owned cache. Prepared IDs carry one pin until caller unpins them.

    A publisher may add pins for further references. It must release the
    prepare-owned pin for every segment once the presentation no longer needs
    the creative. Only opaque IDs should be used as HLS resource URLs.
    """
    def __init__(self, profile: Path, client, fixture_origin):
        self.client, self.fixture_origin = client, fixture_origin
        self.root = Path(profile) / 'controlled-ad-creative-cache'
        if self.root.is_symlink():
            raise ValueError('Creative cache root cannot be a symlink')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        # Only descendants of our owned namespace are eligible for cleanup.
        with _SESSION_LOCK:
            for path in self.root.iterdir():
                if path.is_dir() and not path.is_symlink() and path not in _ACTIVE_SESSIONS:
                    shutil.rmtree(path)
            self.session = self.root / uuid.uuid4().hex
            self.session.mkdir(mode=0o700)
            _ACTIVE_SESSIONS.add(self.session)
        self.entries = {}
        self.total_bytes = 0
        self.reserved_bytes = 0
        self.lock = threading.RLock()
        self.closed = False

    def _check(self, cancel, deadline):
        if cancel.is_set():
            raise Cancelled('Creative preparation cancelled')
        if time.monotonic() >= deadline:
            raise TimeoutError('Creative preparation timed out')

    def _fetch(self, url, cancel, deadline, limit, reserve=None):
        self._check(cancel, deadline)
        if origin(url) != origin(self.fixture_origin):
            _bad('Advertising origin rejected')
        with self.client.open(url, {'Accept': '*/*'}, cancel, deadline=deadline,
                              allowed_origin=self.fixture_origin) as response:
            if response.status != 200 or origin(response.final_url) != origin(self.fixture_origin):
                _bad('Creative request failed')
            body = bytearray()
            while True:
                self._check(cancel, deadline)
                block = response.read(min(65536, limit + 1 - len(body)))
                self._check(cancel, deadline)
                if not block:
                    return bytes(body)
                if reserve is not None:
                    reserve(len(block))
                body.extend(block)
                if len(body) > limit:
                    _bad('Creative exceeds byte limit')

    def _manifest(self, body, url):
        try:
            lines = body.decode('utf-8').splitlines()
        except UnicodeDecodeError:
            _bad('Invalid creative manifest')
        if not lines or lines[0] != '#EXTM3U' or lines[-1] != '#EXT-X-ENDLIST':
            _bad('Incomplete creative manifest')
        if any(line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-KEY', '#EXT-X-MAP', '#EXT-X-BYTERANGE',
                                '#EXT-X-DISCONTINUITY', '#EXT-X-I-FRAME-STREAM-INF')) for line in lines):
            _bad('Unsupported creative manifest')
        allowed_tags = ('#EXT-X-VERSION:', '#EXT-X-TARGETDURATION:', '#EXT-X-MEDIA-SEQUENCE:')
        items = []
        expected = None
        for line in lines[1:-1]:
            if line.startswith('#EXTINF:'):
                if expected is not None:
                    _bad('Invalid creative manifest')
                try:
                    expected = float(line[8:].split(',', 1)[0])
                except ValueError:
                    _bad('Invalid creative duration')
                if not math.isfinite(expected) or expected <= 0:
                    _bad('Invalid creative duration')
            elif line and not line.startswith('#'):
                if expected is None or not line.lower().split('?', 1)[0].endswith('.ts'):
                    _bad('Unsupported creative segment')
                resolved = urljoin(url, line)
                if urlsplit(resolved).scheme not in ('http', 'https'):
                    _bad('Invalid creative segment URL')
                items.append((resolved, expected))
                expected = None
            elif line.startswith('#EXT-X-') and not line.startswith(allowed_tags):
                _bad('Unsupported creative manifest')
        if expected is not None or not items or len(items) > MAX_SEGMENTS:
            _bad('Invalid creative segment count')
        return items

    def _reserve(self, size):
        with self.lock:
            if self.closed:
                _bad('Creative cache is closed')
            for key, entry in list(self.entries.items()):
                if self.total_bytes + self.reserved_bytes + size <= MAX_BYTES:
                    break
                if entry[2] == 0:
                    entry[0].unlink(missing_ok=True)
                    self.total_bytes -= entry[1]
                    del self.entries[key]
            if self.total_bytes + self.reserved_bytes + size > MAX_BYTES:
                _bad('Creative cache is full or pinned')
            self.reserved_bytes += size

    def prepare(self, ad, cancel) -> PreparedAd:
        deadline = time.monotonic() + PREPARATION_SECONDS
        while not _PREPARATION_SLOTS.acquire(blocking=False):
            self._check(cancel, deadline)
            cancel.wait(.02)
        created = []
        reserved = 0
        def reserve(size):
            nonlocal reserved
            self._reserve(size)
            reserved += size
        try:
            manifest = self._fetch(ad.media_url, cancel, deadline, MAX_MANIFEST)
            items = self._manifest(manifest, ad.media_url)
            self._check(cancel, deadline)
            segments = []
            total_samples = 0
            previous = None
            remaining = MAX_BYTES
            for url, advertised in items:
                body = self._fetch(url, cancel, deadline, remaining, reserve)
                info = inspect_ts(body, lambda: self._check(cancel, deadline))
                self._check(cancel, deadline)
                if abs(info.samples / 48000 - advertised) > 1024 / 48000:
                    _bad('Creative manifest duration mismatch')
                if previous:
                    expected_pts = previous.last_pts + FRAME_PTS
                    normalized = info.first_pts
                    while normalized < expected_pts - (PTS_MOD // 2):
                        normalized += PTS_MOD
                    if normalized != expected_pts:
                        _bad('Creative timestamp discontinuity')
                    offset = normalized - info.first_pts
                    info = replace(info, first_pts=normalized, last_pts=info.last_pts + offset)
                previous = info
                total_samples += info.samples
                if total_samples > MAX_SAMPLES:
                    _bad('Creative exceeds duration limit')
                remaining -= len(body)
                resource_id = uuid.uuid4().hex
                path = self.session / (resource_id + '.ts')
                temp = self.session / (resource_id + '.tmp')
                try:
                    with open(temp, 'xb') as handle:
                        os.chmod(temp, 0o600)
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
                created.append(path)
                segments.append(CachedSegment(resource_id, path, info))
            self._check(cancel, deadline)
            with self.lock:
                incoming = sum(p.stat().st_size for p in created)
                if self.closed or incoming != reserved:
                    _bad('Creative cache is full or closed')
                for item in segments:
                    size = item.path.stat().st_size
                    # A successful prepare transfers one pin to its caller.
                    self.entries[item.id] = [item.path, size, 1]
                    self.total_bytes += size
                self.reserved_bytes -= reserved
                reserved = 0
            return PreparedAd(ad, tuple(segments), total_samples)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        finally:
            with self.lock:
                self.reserved_bytes -= reserved
            _PREPARATION_SLOTS.release()

    def pin(self, resource_id):
        with self.lock:
            self.entries[resource_id][2] += 1

    def unpin(self, resource_id):
        with self.lock:
            entry = self.entries[resource_id]
            if entry[2] == 0:
                raise ValueError('Resource is not pinned')
            entry[2] -= 1

    def read(self, resource_id, start=None, end=None):
        with self.lock:
            path, size, _ = self.entries[resource_id]
            lower = 0 if start is None else start
            upper = size if end is None else end
            if not 0 <= lower <= upper <= size:
                raise ValueError('Invalid resource range')
            with path.open('rb') as handle:
                handle.seek(lower)
                return handle.read(upper - lower)

    def close(self):
        with self.lock:
            self.closed = True
            self.entries.clear()
            self.total_bytes = 0
            if self.session.exists():
                shutil.rmtree(self.session)
            with _SESSION_LOCK:
                _ACTIVE_SESSIONS.discard(self.session)
