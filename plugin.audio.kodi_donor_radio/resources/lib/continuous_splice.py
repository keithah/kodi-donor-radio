# SPDX-License-Identifier: GPL-2.0-or-later
"""Frame-aligned replacement of signalled breaks in continuous MP3/ADTS streams.

Pure Python: no decoding, encoding or external executable. Whole compressed
frames from a prepared creative replace whole source frames one-for-one, so the
output keeps the source's media duration and pacing. Outside a replaced break
every source byte passes through unchanged. See
docs/continuous-splice-design.md for the contract this module implements.
"""
from collections import namedtuple
from fractions import Fraction
import time

from http_client import Cancelled


MP3, AAC = 'mp3', 'aac'
MAX_PENDING_BYTES = 16 * 1024
MAX_SIGNALS = 4096
CREATIVE_BYTES = 4 * 1024 * 1024
CREATIVE_SECONDS = 5
DEFAULT_BREAK_SECONDS = 300
BREAK_GRACE_SECONDS = 10

# The most bytes either parser reads: an MP3 header, CRC and two side-info
# bytes, or an ADTS header with CRC. Fewer bytes are incomplete, not invalid.
_HEADER_BYTES = {MP3: 8, AAC: 9}
_MIME_CODECS = {'audio/mpeg': MP3, 'audio/mp3': MP3, 'audio/aac': AAC,
                'audio/aacp': AAC, 'audio/x-aac': AAC}

_MP3_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
              16000, 12000, 11025, 8000, 7350)

# format: hashable tuple compared for splice compatibility.
# reservoir: MP3 main_data_begin in bytes (always 0 for ADTS).
Header = namedtuple('Header', 'format length samples sample_rate reservoir')
Unit = namedtuple('Unit', 'kind data offset header')  # kind: 'frame' or 'raw'


class CreativeRejected(ValueError):
    """The creative cannot fill a break; reason is a fixed diagnostic token."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def codec_for_mime(content_type):
    """Map a response Content-Type to a supported codec, or None."""
    if not isinstance(content_type, str):
        return None
    return _MIME_CODECS.get(content_type.split(';', 1)[0].strip().lower())


def parse_mp3_header(data, position=0):
    """Return a Header for an MPEG Layer III frame at position, or None."""
    if len(data) - position < 6:
        return None
    b0, b1, b2, b3 = data[position:position + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 3   # 3 MPEG-1, 2 MPEG-2, 0 MPEG-2.5, 1 reserved
    layer = (b1 >> 1) & 3     # 1 Layer III
    if version == 1 or layer != 1:
        return None
    protected = not (b1 & 1)
    bitrate_index, rate_index, padding = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
    if bitrate_index in (0, 15) or rate_index == 3 or (b3 & 3) == 2:
        return None
    mpeg1 = version == 3
    bitrate = _MP3_BITRATES[1 if mpeg1 else 2][bitrate_index] * 1000
    sample_rate = _MP3_RATES[version][rate_index]
    samples = 1152 if mpeg1 else 576
    length = (144 if mpeg1 else 72) * bitrate // sample_rate + padding
    channels = 1 if (b3 >> 6) == 3 else 2
    side = position + 4 + (2 if protected else 0)
    if len(data) - side < 2:
        return None
    if mpeg1:
        reservoir = (data[side] << 1) | (data[side + 1] >> 7)
    else:
        reservoir = data[side]
    return Header((MP3, version, 3, sample_rate, channels, samples), length,
                  samples, sample_rate, reservoir)


def parse_adts_header(data, position=0):
    """Return a Header for an ADTS AAC frame at position, or None."""
    if len(data) - position < 7:
        return None
    b = data[position:position + 7]
    if b[0] != 0xFF or (b[1] & 0xF6) != 0xF0:  # sync 0xFFF, layer 00
        return None
    protected = not (b[1] & 1)
    profile, rate_index = b[2] >> 6, (b[2] >> 2) & 0xF
    channels = ((b[2] & 1) << 2) | (b[3] >> 6)
    if rate_index >= len(_AAC_RATES) or channels not in (1, 2):
        return None
    length = ((b[3] & 3) << 11) | (b[4] << 3) | (b[5] >> 5)
    header_bytes = 9 if protected else 7
    if length <= header_bytes:
        return None
    blocks = (b[6] & 3) + 1
    sample_rate = _AAC_RATES[rate_index]
    samples = 1024 * blocks
    return Header((AAC, profile, sample_rate, channels, samples), length,
                  samples, sample_rate, 0)


_PARSERS = {MP3: parse_mp3_header, AAC: parse_adts_header}


class FrameAssembler:
    """Split a byte stream into whole frames and opaque raw runs.

    Sync needs two consecutive same-format headers; once locked, each frame
    must keep the locked format. Nothing is dropped: concatenating every
    returned unit's data reproduces the input exactly. At most
    MAX_PENDING_BYTES are held.
    """

    def __init__(self, codec):
        if codec not in _PARSERS:
            raise ValueError('Unsupported continuous codec')
        self.codec = codec
        self._parse = _PARSERS[codec]
        self._header_bytes = _HEADER_BYTES[codec]
        self._buffer = bytearray()
        self._offset = 0  # Stream offset of _buffer[0].
        self.format = None

    @property
    def pending(self):
        return len(self._buffer)

    def _take(self, count, kind, header=None):
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        unit = Unit(kind, data, self._offset, header)
        self._offset += count
        return unit

    def feed(self, data):
        self._buffer.extend(data)
        units = []
        buffer = self._buffer
        while buffer:
            if self.format is not None:
                header = self._parse(buffer)
                if header is not None and header.format == self.format:
                    if len(buffer) < header.length:
                        break  # Frames are < 8 KiB: always within the bound.
                    units.append(self._take(header.length, 'frame', header))
                    continue
                if header is None and len(buffer) < self._header_bytes:
                    break
                self.format = None  # Lost sync or the format changed.
            position, locked = 0, False
            limit = len(buffer) - self._header_bytes
            while position <= limit:
                header = self._parse(buffer, position)
                if header is None:
                    position += 1
                    continue
                following = position + header.length
                if following + self._header_bytes > len(buffer):
                    if len(buffer) - position > MAX_PENDING_BYTES:
                        position += 1  # Cannot be a frame within the bound.
                        continue
                    break  # Wait for the confirming header.
                second = self._parse(buffer, following)
                if second is not None and second.format == header.format:
                    locked = True
                    break
                position += 1
            else:
                # No candidate: keep only a tail that may begin a header.
                position = max(0, limit + 1)
            if position:
                units.append(self._take(position, 'raw'))
            if locked:
                self.format = header.format
                continue
            break
        return units

    def finish(self):
        """Return held bytes as one raw unit (or nothing)."""
        if not self._buffer:
            return []
        return [self._take(len(self._buffer), 'raw')]


Creative = namedtuple('Creative', 'format frames samples')


def prepare_creative(codec, data):
    """Validate a creative as whole frames of exactly one format."""
    if codec not in _PARSERS:
        raise CreativeRejected('unsupported_codec')
    if not data:
        raise CreativeRejected('no_fill')
    if len(data) > CREATIVE_BYTES:
        raise CreativeRejected('creative_too_large')
    data = _strip_id3(bytes(data)) if codec == MP3 else bytes(data)
    assembler = FrameAssembler(codec)
    units = assembler.feed(data) + assembler.finish()
    frames = [unit for unit in units if unit.kind == 'frame']
    # Tags are already stripped: any other unframed byte hides a frame the
    # creative would silently lose, such as one at a different sample rate.
    if not frames or any(unit.kind == 'raw' for unit in units):
        raise CreativeRejected('creative_unparseable')
    formats = {unit.header.format for unit in frames}
    if len(formats) != 1:
        raise CreativeRejected('creative_mixed_format')
    if frames[0].header.reservoir:
        # The splice-in frame follows unrelated source data, so it must not
        # borrow MP3 bit-reservoir bytes from frames that are never sent.
        raise CreativeRejected('creative_reservoir')
    return Creative(formats.pop(), tuple(unit.data for unit in frames),
                    sum(unit.header.samples for unit in frames))


def _strip_id3(data):
    if data.startswith(b'ID3') and len(data) >= 10 and not any(b & 0x80 for b in data[6:10]):
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        data = data[10 + size + (10 if data[5] & 0x10 else 0):]
    if len(data) >= 128 and data[-128:-125] == b'TAG':
        data = data[:-128]
    return data


def fetch_creative(client, url, fixture_origin, cancel):
    """Fetch creative bytes from the exact fixture origin, bounded in time and size."""
    deadline = time.monotonic() + CREATIVE_SECONDS
    with client.open(url, {'Accept': 'audio/*'}, cancel, deadline=deadline,
                     allowed_origin=fixture_origin) as response:
        if response.status != 200:
            raise CreativeRejected('no_fill')
        body = bytearray()
        while True:
            if cancel.is_set():
                raise Cancelled('Creative fetch cancelled')
            block = response.read(min(65536, CREATIVE_BYTES + 1 - len(body)))
            if not block:
                return bytes(body)
            body.extend(block)
            if len(body) > CREATIVE_BYTES:
                raise CreativeRejected('creative_too_large')


def adw_signal(metadata):
    """Return ('start'|'end', duration_ms or None) for AdsWizz adw_ad, else None."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get('adw_ad')
    if not isinstance(value, str) or value.lower() not in ('true', 'false'):
        return None
    duration = metadata.get('durationMilliseconds')
    milliseconds = None
    if isinstance(duration, str) and duration.isascii() and duration.isdigit() and len(duration) <= 6:
        milliseconds = int(duration)
        if not 1 <= milliseconds <= 300000:
            milliseconds = None
    return ('start' if value.lower() == 'true' else 'end', milliseconds)


PROGRAMME, REPLACING, BREAK_SOURCE, DISABLED = 'programme', 'replacing', 'break_source', 'disabled'


class ContinuousSplicer:
    """Swap whole creative frames into signalled breaks of one continuous stream.

    ``signal`` records a boundary at an offset in the de-framed audio stream;
    ``feed`` takes the next de-framed audio bytes and returns output bytes.
    ``emit(kind, details)`` receives fixed-schema diagnostics. A ``creative``
    of None (with ``skip_reason``) makes every break pass the source through.
    """

    def __init__(self, codec, creative, emit, skip_reason='no_fill'):
        self.assembler = FrameAssembler(codec)
        self.codec = codec
        self.creative = creative
        self.skip_reason = skip_reason
        self._sink = emit
        self.state = PROGRAMME
        self._signals = []
        self._sample = 0          # Source media samples processed so far.
        self._break = None        # Open-break bookkeeping.
        self._rejoin = None       # End event waiting for its first source frame.

    def _emit(self, kind, details):
        try:
            self._sink(kind, details)
        except Exception:
            pass  # Diagnostics must never cost audio.

    def signal(self, kind, offset, duration_ms=None):
        if kind not in ('start', 'end'):
            raise ValueError('Invalid boundary signal')
        if self.state == DISABLED:
            return
        # Every signal is kept in order: a repeat can matter after a timeout,
        # and _apply ignores the ones that do not. Signals wait until a frame
        # at or after their offset is released, so a frame held across many
        # reads can collect them from all of those reads. Dropping one could
        # lose a pending end and replace programme audio, so overflowing the
        # bound gives the listener the source instead.
        if len(self._signals) >= MAX_SIGNALS:
            self._disable('signal_overflow')
            return
        self._signals.append((kind, offset, duration_ms))

    def observe_metadata(self, metadata, offset):
        signal = adw_signal(metadata)
        if signal is not None:
            self.signal(signal[0], offset, signal[1])

    def _drain(self):
        """Release bytes the assembler still holds; used once splicing stops."""
        assembler, self.assembler = self.assembler, None
        if assembler is None:
            return b''
        try:
            return b''.join(unit.data for unit in assembler.finish())
        except Exception:
            return b''

    def feed(self, data):
        if self.state == DISABLED:
            return self._drain() + bytes(data)
        try:
            output = bytearray()
            for unit in self.assembler.feed(data):
                self._unit(unit, output)
            return bytes(output)
        except Exception:
            # Never risk the source on a splicer defect: pass through from here.
            # Bytes the assembler still held from earlier feeds are dropped
            # (at most one pending frame); they cannot be re-ordered safely.
            self.assembler = None
            self._disable('splicer_error')
            return bytes(data)

    def finish(self):
        """Close the stream; return held source bytes that may still be sent."""
        output = bytearray()
        held = self._drain()
        if self.state != REPLACING:
            output.extend(held)
        if self.state == REPLACING:
            self._end('stopped')
        self._flush_rejoin(None)
        self.creative = None
        self._signals.clear()
        self.state = DISABLED
        return bytes(output)

    def _disable(self, reason):
        if self.state == REPLACING:
            self._end(reason)
        self._flush_rejoin(None)
        self.state = DISABLED
        self._signals.clear()
        self._emit('continuous_splice_disabled', {'reason': reason})

    def _limit_seconds(self, duration_ms):
        if duration_ms:
            return Fraction(duration_ms, 1000) + BREAK_GRACE_SECONDS
        return Fraction(DEFAULT_BREAK_SECONDS)

    def _advance_break(self, header):
        """Count one frame against the open break; True once it reaches its bound.

        The bound is media time: each frame adds its own duration, so a source
        sample-rate change inside the break cannot stretch or shrink it.
        """
        record = self._break
        record['samples'] += header.samples  # Diagnostics only.
        record['elapsed'] += Fraction(header.samples, header.sample_rate)
        return record['elapsed'] >= record['limit']

    def _unit(self, unit, output):
        header = unit.header
        # Signals apply at the first frame starting at or after the boundary.
        if unit.kind == 'frame':
            while self._signals and self._signals[0][1] <= unit.offset:
                self._apply(self._signals.pop(0), unit)
        if unit.kind == 'raw':
            if self.state == REPLACING:
                # The break began on a locked frame, so raw bytes mean the
                # source lost sync: give the listener the source back.
                self.state = BREAK_SOURCE
                self._end('sync_lost')
                output.extend(unit.data)
                self._disable('sync_lost')
                return
            output.extend(unit.data)
            return
        if self.state == REPLACING:
            if header.format != self.creative.format:
                self.state = BREAK_SOURCE
                self._end('format_change')
                output.extend(unit.data)
                self._disable('format_change')
                return
            index = self._break['index']
            output.extend(self.creative.frames[index])
            self._break['index'] = index + 1
            self._sample += header.samples
            if self._advance_break(header):
                self._end('timeout')
                self._break = None
                self.state = PROGRAMME
            elif self._break['index'] == len(self.creative.frames):
                self._end('creative_exhausted')
                self.state = BREAK_SOURCE
            return
        self._flush_rejoin(unit)
        output.extend(unit.data)
        self._sample += header.samples
        if self.state == BREAK_SOURCE and self._break is not None:
            if self._advance_break(header):
                self._break = None
                self.state = PROGRAMME
                self._emit('continuous_break_unresolved', {'reason': 'timeout'})

    def _apply(self, signal, unit):
        kind, offset, duration_ms = signal
        header = unit.header
        if kind == 'start':
            if self.state != PROGRAMME:
                return
            # A previous break that ended without a source frame in between
            # still reports its end before the new break starts.
            self._flush_rejoin(None)
            details = {'signal_offset': offset, 'splice_offset': unit.offset,
                       'boundary_error_bytes': unit.offset - offset,
                       'start_sample': self._sample, 'codec': self.codec,
                       'sample_rate': header.sample_rate}
            self._break = {'index': 0, 'samples': 0, 'elapsed': Fraction(0),
                           'limit': self._limit_seconds(duration_ms), 'start': details}
            if self.creative is None:
                self.state = BREAK_SOURCE
                self._emit('continuous_replacement_skipped', dict(details, reason=self.skip_reason))
            elif self.creative.format != header.format:
                self.state = BREAK_SOURCE
                self._emit('continuous_replacement_skipped', dict(details, reason='format_mismatch'))
            else:
                self.state = REPLACING
                self._emit('continuous_replacement_start', details)
            return
        if self.state == REPLACING:
            # Exhaustion leaves REPLACING at once, so creative frames remain.
            # A new start may still claim this same frame, so the rejoin is
            # reported only once _unit actually forwards a source frame.
            self._end('truncated')
        self._break = None
        self.state = PROGRAMME

    def _end(self, reason):
        record = self._break
        details = {'reason': reason, 'frames_replaced': record['index'],
                   'samples_replaced': record['samples'],
                   'start_sample': record['start']['start_sample'],
                   'splice_offset': record['start']['splice_offset'],
                   'signal_offset': record['start']['signal_offset']}
        self._rejoin = details
        if reason in ('stopped', 'format_change', 'sync_lost', 'splicer_error',
                      'signal_overflow'):
            self._flush_rejoin(None)

    def _flush_rejoin(self, unit):
        details, self._rejoin = self._rejoin, None
        if details is None:
            return
        if unit is not None:
            details['rejoin_offset'] = unit.offset
            details['rejoin_sample'] = self._sample
            if self.codec == MP3:
                details['rejoin_reservoir_bytes'] = unit.header.reservoir
        self._emit('continuous_replacement_end', details)
