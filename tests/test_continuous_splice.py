# SPDX-License-Identifier: GPL-2.0-or-later
"""Frame-aligned continuous-stream splicing: parsers, state machine, relay, decode."""
import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import random
import shutil
import threading
import time
from urllib.parse import urlsplit

import pytest

from ad_models import AdConfig
import continuous_splice as cs
from http_client import HttpClient
from icy import IcyDecoder
from models import Station
from url_policy import UrlPolicy


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
ASSETS = Path(__file__).resolve().parent / 'generated-audio' / 'continuous'


def _tools(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic frames: valid headers, letter payloads (no false sync bytes).

def mp3_frame(marker=b'P', reservoir=0, version=3, rate_index=0, bitrate_index=9,
              padding=0, mono=False, crc=False):
    b1 = 0xE0 | (version << 3) | (1 << 1) | (0 if crc else 1)
    b2 = (bitrate_index << 4) | (rate_index << 2) | (padding << 1)
    b3 = (0xC0 if mono else 0x40)
    mpeg1 = version == 3
    bitrate = cs._MP3_BITRATES[1 if mpeg1 else 2][bitrate_index] * 1000
    rate = cs._MP3_RATES[version][rate_index]
    length = (144 if mpeg1 else 72) * bitrate // rate + padding
    side = bytes([reservoir >> 1, (reservoir & 1) << 7]) if mpeg1 else bytes([reservoir, 0])
    head = bytes([0xFF, b1, b2, b3]) + (b'\0\0' if crc else b'') + side
    return head + marker * (length - len(head))


def adts_frame(marker=b'P', length=300, rate_index=4, channels=2, profile=1, blocks=1, crc=False):
    b = bytearray(7)
    b[0] = 0xFF
    b[1] = 0xF0 | (0 if crc else 1)
    b[2] = (profile << 6) | (rate_index << 2) | (channels >> 2)
    b[3] = ((channels & 3) << 6) | ((length >> 11) & 3)
    b[4] = (length >> 3) & 0xFF
    b[5] = ((length & 7) << 5) | 0x1F
    b[6] = 0xFC | (blocks - 1)
    head = bytes(b) + (b'\0\0' if crc else b'')
    return head + marker * (length - len(head))


FRAME = {cs.MP3: mp3_frame, cs.AAC: adts_frame}


def frames(codec, markers, **kwargs):
    return [FRAME[codec](bytes([m]) if isinstance(m, int) else m, **kwargs) for m in markers]


class Events(list):
    def __call__(self, kind, details):
        self.append((kind, dict(details)))

    def kinds(self):
        return [kind for kind, _ in self]

    def first(self, kind):
        return next(details for name, details in self if name == kind)


def run(codec, source, creative, signals, chunk=997, metaint=None):
    """Splice a frame list, optionally via ICY framing. signals: {frame index or byte: fields}."""
    events = Events()
    splicer = cs.ContinuousSplicer(codec, creative, events)
    audio = b''.join(source)
    output = bytearray()
    if metaint is None:
        offsets = [sum(len(f) for f in source[:i]) for i in range(len(source) + 1)]
        pending = sorted((offsets[i] + extra, fields) for (i, extra), fields in signals.items())
        position = 0
        while position < len(audio):
            end = min(len(audio), position + chunk)
            while pending and pending[0][0] <= end:
                at, fields = pending.pop(0)
                if at > position:
                    output += splicer.feed(audio[position:at])
                    position = at
                splicer.observe_metadata(fields, at)
            output += splicer.feed(audio[position:end])
            position = end
    else:
        fixture = _tools('continuous_splice_fixtures')
        stream, _ = fixture.icy_interleave(audio, metaint, signals)
        decoder = None
        decoder = IcyDecoder(metaint, lambda value: splicer.observe_metadata(
            value, decoder.audio_position))
        for start in range(0, len(stream), chunk):
            output += splicer.feed(decoder.feed(stream[start:start + chunk]))
    output += splicer.finish()
    return bytes(output), events, splicer


def creative_of(codec, markers, **kwargs):
    return cs.prepare_creative(codec, b''.join(frames(codec, markers, **kwargs)))


START = {'adw_ad': 'true', 'StreamTitle': ''}
END = {'adw_ad': 'false', 'StreamTitle': ''}


# ---------------------------------------------------------------------------
# Header parsers

def test_mp3_header_fields_for_mpeg1_mpeg2_and_mpeg25():
    header = cs.parse_mp3_header(mp3_frame(reservoir=397))
    assert header.length == 417 and header.samples == 1152 and header.sample_rate == 44100
    assert header.reservoir == 397 and header.format == (cs.MP3, 3, 3, 44100, 2, 1152)
    assert cs.parse_mp3_header(mp3_frame(padding=1)).length == 418
    assert cs.parse_mp3_header(mp3_frame(crc=True, reservoir=5)).reservoir == 5
    mpeg2 = cs.parse_mp3_header(mp3_frame(version=2, bitrate_index=8, reservoir=200))
    assert (mpeg2.samples, mpeg2.sample_rate, mpeg2.length, mpeg2.reservoir) == (576, 22050, 208, 200)
    mpeg25 = cs.parse_mp3_header(mp3_frame(version=0, rate_index=2, mono=True))
    assert mpeg25.sample_rate == 8000 and mpeg25.format[4] == 1


@pytest.mark.parametrize('mutate', [
    lambda f: f[:1] + bytes([f[1] & ~0x06 | 0x04]) + f[2:],    # Layer II
    lambda f: f[:1] + bytes([f[1] & ~0x18 | 0x08]) + f[2:],    # reserved version
    lambda f: f[:2] + bytes([f[2] & 0x0F]) + f[3:],            # free-format bitrate
    lambda f: f[:2] + bytes([f[2] | 0xF0]) + f[3:],            # bad bitrate
    lambda f: f[:2] + bytes([f[2] | 0x0C]) + f[3:],            # reserved sample rate
    lambda f: f[:3] + bytes([f[3] | 0x02]) + f[4:],            # reserved emphasis
    lambda f: b'\xff\x00' + f[2:],                             # no sync
    lambda f: f[:5],                                           # too short for side info
])
def test_mp3_header_rejects_unsupported_or_invalid(mutate):
    assert cs.parse_mp3_header(mutate(mp3_frame())) is None


def test_adts_header_fields_and_rejections():
    header = cs.parse_adts_header(adts_frame(length=371, blocks=2))
    assert header.length == 371 and header.samples == 2048 and header.sample_rate == 44100
    assert header.format == (cs.AAC, 1, 44100, 2, 2048)
    assert cs.parse_adts_header(adts_frame(crc=True)).length == 300
    assert cs.parse_adts_header(adts_frame(rate_index=3, channels=1)).format[2:4] == (48000, 1)
    for bad in (adts_frame(channels=0), adts_frame(channels=6), adts_frame(rate_index=13),
                adts_frame(length=7), adts_frame(length=9, crc=True), adts_frame()[:6]):
        assert cs.parse_adts_header(bad) is None
    layered = bytearray(adts_frame())
    layered[1] |= 0x02
    assert cs.parse_adts_header(bytes(layered)) is None


def test_codec_for_mime_and_adw_signal_contract():
    assert cs.codec_for_mime('audio/mpeg; charset=x') == cs.MP3
    assert cs.codec_for_mime('Audio/AACP') == cs.AAC
    assert cs.codec_for_mime('audio/ogg') is None and cs.codec_for_mime(None) is None
    assert cs.adw_signal({'adw_ad': 'TRUE', 'durationMilliseconds': '30000'}) == ('start', 30000)
    assert cs.adw_signal({'adw_ad': 'false'}) == ('end', None)
    for metadata in ({'adw_ad': 'yes'}, {'StreamTitle': 'AD_START_30'}, {}, None,
                     {'adw_ad': 1}):
        assert cs.adw_signal(metadata) is None
    for duration in ('0', '300001', '-5', '1e3', '١٢', '1234567'):
        assert cs.adw_signal({'adw_ad': 'true', 'durationMilliseconds': duration}) == ('start', None)


# ---------------------------------------------------------------------------
# Assembler

@pytest.mark.parametrize('codec', [cs.MP3, cs.AAC])
def test_assembler_never_drops_bytes_and_frames_whole_units(codec):
    rng = random.Random(7)
    source = b''.join(frames(codec, b'ABCDEFGH' * 4))
    data = b'ID3\x03\x00\x00\x00\x00\x00\x05hello' + source + b'trailing garbage' + source
    for _ in range(20):
        assembler = cs.FrameAssembler(codec)
        units, position = [], 0
        while position < len(data):
            step = rng.randint(1, 900)
            units += assembler.feed(data[position:position + step])
            position += step
            assert assembler.pending <= cs.MAX_PENDING_BYTES
        units += assembler.finish()
        assert b''.join(unit.data for unit in units) == data
        frame_units = [unit for unit in units if unit.kind == 'frame']
        assert len(frame_units) == 64
        assert all(unit.offset == sum(len(u.data) for u in units[:index])
                   for index, unit in enumerate(units))


def test_assembler_needs_two_headers_to_lock_and_resyncs_after_format_change():
    one = mp3_frame(b'A')
    assembler = cs.FrameAssembler(cs.MP3)
    units = assembler.feed(b'xx' + one)
    assert [(u.kind, u.data) for u in units] == [('raw', b'xx')] and assembler.format is None
    units = assembler.feed(one)
    assert [unit.kind for unit in units] == ['frame', 'frame'] and assembler.format is not None
    mono = b''.join(frames(cs.MP3, b'MNO', mono=True))
    units = assembler.feed(mono) + assembler.finish()
    assert b''.join(unit.data for unit in units) == mono
    assert {unit.header.format[4] for unit in units if unit.kind == 'frame'} == {1}


def test_assembler_bounds_pending_bytes_on_false_sync_noise():
    noise = (b'\xff\xfb\x90\x40' + b'\x01' * 60) * 2000
    assembler = cs.FrameAssembler(cs.MP3)
    output = bytearray()
    for start in range(0, len(noise), 4096):
        output += b''.join(unit.data for unit in assembler.feed(noise[start:start + 4096]))
        assert assembler.pending <= cs.MAX_PENDING_BYTES + 8
    output += b''.join(unit.data for unit in assembler.finish())
    assert bytes(output) == noise


# ---------------------------------------------------------------------------
# Creative preparation

def test_prepare_creative_validates_and_strips_tags():
    creative = cs.prepare_creative(cs.MP3, b'ID3\x03\x00\x00\x00\x00\x00\x02xx'
                                   + b''.join(frames(cs.MP3, b'XYZ')) + b'TAG' + b'\0' * 125)
    assert len(creative.frames) == 3 and creative.samples == 3456
    for data, reason in ((b'', 'no_fill'), (b'not audio' * 100, 'creative_unparseable'),
                         (b'\0' * (cs.CREATIVE_BYTES + 1), 'creative_too_large'),
                         (b''.join(frames(cs.MP3, b'AB') + frames(cs.MP3, b'CD', mono=True)),
                          'creative_mixed_format')):
        with pytest.raises(cs.CreativeRejected) as caught:
            cs.prepare_creative(cs.MP3, data)
        assert caught.value.reason == reason
    with pytest.raises(cs.CreativeRejected):
        cs.prepare_creative('ogg', b'x')
    hidden = b''.join(frames(cs.MP3, b'A' * 200) + [mp3_frame(b'R', rate_index=1)]
                      + frames(cs.MP3, b'B' * 200))
    with pytest.raises(cs.CreativeRejected) as caught:
        cs.prepare_creative(cs.MP3, hidden)
    assert caught.value.reason == 'creative_unparseable'
    borrowed = mp3_frame(b'X', reservoir=200) + b''.join(frames(cs.MP3, b'YZ'))
    with pytest.raises(cs.CreativeRejected) as caught:
        cs.prepare_creative(cs.MP3, borrowed)
    assert caught.value.reason == 'creative_reservoir'


# ---------------------------------------------------------------------------
# Splice state machine

@pytest.mark.parametrize('codec', [cs.MP3, cs.AAC])
@pytest.mark.parametrize('chunk', [1, 13, 997, 65536])
def test_no_signal_output_is_byte_identical(codec, chunk):
    source = frames(codec, b'ABCDEFGHIJ' * 5)
    output, events, _ = run(codec, source, creative_of(codec, b'xyz'),
                            {(3, 0): {'StreamTitle': 'AD_START_30'},
                             (6, 0): END, (9, 0): {'adw_ad': 'maybe'}}, chunk=chunk)
    assert output == b''.join(source) and not events


@pytest.mark.parametrize('codec', [cs.MP3, cs.AAC])
@pytest.mark.parametrize('chunk', [1, 7, 997])
def test_signalled_break_swaps_whole_frames_one_for_one(codec, chunk):
    source = frames(codec, b'AAAAABBBBBBCCCCC')
    creative = creative_of(codec, b'xxxxxxxxxx')
    # Start signal lands 5 bytes into frame 4; end lands exactly on frame 11.
    output, events, splicer = run(codec, source, creative,
                                  {(4, 5): START, (11, 0): END}, chunk=chunk)
    expected = b''.join(source[:5]) + b''.join(creative.frames[:6]) + b''.join(source[11:])
    assert output == expected
    start = events.first('continuous_replacement_start')
    frame_bytes = len(source[0])
    assert 0 < start['boundary_error_bytes'] < frame_bytes
    assert start['splice_offset'] == 5 * frame_bytes
    end = events.first('continuous_replacement_end')
    assert end['reason'] == 'truncated' and end['frames_replaced'] == 6
    assert end['rejoin_offset'] == 11 * frame_bytes
    assert end['samples_replaced'] == 6 * cs.parse_mp3_header(source[0]).samples if codec == cs.MP3 \
        else end['samples_replaced'] == 6 * 1024
    assert end['rejoin_sample'] - start['start_sample'] == end['samples_replaced']
    assert splicer.state == cs.DISABLED  # finish() releases the splicer


def test_signal_at_exact_frame_start_and_split_across_icy_metadata():
    codec = cs.MP3
    source = frames(codec, b'AAAAAAAABBBBBBBBCCCCCCCC')
    creative = creative_of(codec, b'xxxxxxxx')
    frame = len(source[0])
    # ICY places signals at metaint boundaries; split reads cut every block.
    reference = None
    for chunk in (1, 3, frame - 1, 4096):
        output, events, _ = run(codec, source, creative,
                                {8 * frame: dict(START, durationMilliseconds='5000'),
                                 16 * frame: END}, chunk=chunk, metaint=frame)
        reference = reference or output
        assert output == reference
        assert events.first('continuous_replacement_start')['boundary_error_bytes'] == 0
    assert reference == b''.join(source[:8]) + b''.join(creative.frames) + b''.join(source[16:])


def test_creative_exhausted_rejoins_source_and_reports_reservoir():
    source = [mp3_frame(bytes([m]), reservoir=r) for m, r in zip(b'AABBBBBBCC', [0] * 5 + [300] * 5)]
    creative = creative_of(cs.MP3, b'xy')
    output, events, _ = run(cs.MP3, source, creative, {(2, 0): START, (8, 0): END})
    assert output == b''.join(source[:2]) + b''.join(creative.frames) + b''.join(source[4:])
    end = events.first('continuous_replacement_end')
    assert end['reason'] == 'creative_exhausted' and end['frames_replaced'] == 2
    assert end['rejoin_offset'] == 4 * len(source[0]) and end['rejoin_reservoir_bytes'] == 0
    output, events, _ = run(cs.MP3, source, creative_of(cs.MP3, b'xyzxyz'), {(2, 0): START, (6, 0): END})
    assert events.first('continuous_replacement_end')['rejoin_reservoir_bytes'] == 300


@pytest.mark.parametrize('creative, reason', [
    (None, 'no_fill'),
    ('mono', 'format_mismatch'),
    ('rate', 'format_mismatch'),
])
def test_no_fill_and_format_mismatch_keep_source(creative, reason):
    source = frames(cs.MP3, b'AAABBBCCC')
    if creative == 'mono':
        creative = creative_of(cs.MP3, b'xyz', mono=True)
    elif creative == 'rate':
        creative = creative_of(cs.MP3, b'xyz', rate_index=1)
    output, events, _ = run(cs.MP3, source, creative, {(3, 0): START, (6, 0): END})
    assert output == b''.join(source)
    assert events.kinds() == ['continuous_replacement_skipped']
    assert events[0][1]['reason'] == reason


def test_repeated_start_ignored_and_second_break_replays_creative_from_start():
    source = frames(cs.AAC, b'AABBBCCDDDEE')
    creative = creative_of(cs.AAC, b'xyz')
    output, events, _ = run(cs.AAC, source, creative,
                            {(2, 0): START, (3, 0): START, (5, 0): END, (7, 0): START, (10, 0): END})
    expected = (b''.join(source[:2]) + b''.join(creative.frames) + b''.join(source[5:7])
                + b''.join(creative.frames) + b''.join(source[10:]))
    assert output == expected
    assert events.kinds().count('continuous_replacement_start') == 2


def test_duration_bound_closes_unanswered_break():
    codec = cs.MP3
    source = frames(codec, b'A' * 4 + b'B' * 200)
    long_creative = creative_of(codec, b'x' * 150)
    # 1 ms + 10 s grace = 441 044 samples = 382.9 frames > the 200 frames here.
    output, events, _ = run(codec, source, long_creative, {(4, 0): dict(START, durationMilliseconds='1')})
    assert events.kinds() == ['continuous_replacement_start', 'continuous_replacement_end']
    assert events[-1][1]['reason'] == 'creative_exhausted'
    old = cs.BREAK_GRACE_SECONDS
    cs.BREAK_GRACE_SECONDS = 0
    try:
        output, events, _ = run(codec, source, long_creative,
                                {(4, 0): dict(START, durationMilliseconds='100')})
    finally:
        cs.BREAK_GRACE_SECONDS = old
    end = events.first('continuous_replacement_end')
    assert end['reason'] == 'timeout' and end['frames_replaced'] == 4  # 4 x 1152 >= 4410
    assert output == b''.join(source[:4]) + b''.join(long_creative.frames[:4]) + b''.join(source[8:])
    output, events, _ = run(codec, source, None, {(4, 0): dict(START, durationMilliseconds='100')})
    cs.BREAK_GRACE_SECONDS = 0
    try:
        output, events, _ = run(codec, source, None, {(4, 0): dict(START, durationMilliseconds='100')})
    finally:
        cs.BREAK_GRACE_SECONDS = old
    assert events.kinds() == ['continuous_replacement_skipped', 'continuous_break_unresolved']
    assert output == b''.join(source)


def test_source_format_change_inside_break_disables_and_passes_through():
    source = frames(cs.MP3, b'AABB') + frames(cs.MP3, b'MMMM', mono=True)
    creative = creative_of(cs.MP3, b'xyzxyzxyz')
    output, events, splicer = run(cs.MP3, source, creative, {(2, 0): START})
    assert output == b''.join(source[:2]) + b''.join(creative.frames[:2]) + b''.join(source[4:])
    assert events.kinds() == ['continuous_replacement_start', 'continuous_replacement_end',
                              'continuous_splice_disabled']
    assert events[1][1]['reason'] == 'format_change'


def test_finish_flushes_programme_bytes_but_drops_break_bytes():
    source = frames(cs.AAC, b'AAAA')
    events = Events()
    splicer = cs.ContinuousSplicer(cs.AAC, creative_of(cs.AAC, b'xy'), events)
    held = splicer.feed(b''.join(source) + source[0][:50])
    assert splicer.finish() == source[0][:50] and held == b''.join(source)
    events = Events()
    splicer = cs.ContinuousSplicer(cs.AAC, creative_of(cs.AAC, b'xyzxyz'), events)
    splicer.observe_metadata(START, 0)
    out = splicer.feed(b''.join(source) + source[0][:50])
    assert splicer.finish() == b''
    assert len(out) == 4 * len(source[0])
    assert events.kinds()[-1] == 'continuous_replacement_end'
    assert events[-1][1]['reason'] == 'stopped' and 'rejoin_offset' not in events[-1][1]
    assert splicer.creative is None and splicer.finish() == b''  # idempotent


def test_splicer_fault_passes_source_through_and_emitter_errors_are_contained():
    source = frames(cs.MP3, b'AAAA')
    def broken(kind, details):
        raise RuntimeError('sink down')
    splicer = cs.ContinuousSplicer(cs.MP3, creative_of(cs.MP3, b'xy'), broken)
    splicer.observe_metadata(START, 0)
    first = splicer.feed(b''.join(source))
    assert splicer.state != cs.DISABLED and len(first) == len(b''.join(source))
    events = Events()
    splicer = cs.ContinuousSplicer(cs.MP3, None, events)
    splicer.assembler = None  # Any internal defect.
    assert splicer.feed(b'abc') == b'abc' and splicer.feed(b'def') == b'def'
    assert events.kinds() == ['continuous_splice_disabled']


# ---------------------------------------------------------------------------
# Relay integration: real loopback upstream, relay and session manager.

@pytest.fixture
def upstream():
    state = {'icy': b'', 'metaint': 0, 'creative': b'', 'creative_status': 200,
             'mime': 'audio/mpeg', 'drop_after': None, 'pace': 0, 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            state['requests'].append(self.path)
            if self.path == '/creative':
                body = state['creative']
                self.send_response(state['creative_status'])
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = state['icy']
            self.send_response(200)
            self.send_header('Content-Type', state['mime'])
            self.send_header('icy-metaint', str(state['metaint']))
            if state['drop_after']:
                # A declared length makes the early close a failure, not EOF.
                self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            limit = state['drop_after'] or len(body)
            try:
                for start in range(0, limit, 4096):
                    self.wfile.write(body[start:min(limit, start + 4096)])
                    self.wfile.flush()
                    if state['pace']:
                        time.sleep(state['pace'])
                if state['drop_after']:
                    self.connection.shutdown(2)  # Abrupt network failure.
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state['origin'] = 'http://127.0.0.1:%d' % server.server_port
    yield state
    server.shutdown()
    server.server_close()


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def kinds(self, prefix='continuous_'):
        return [event.kind for event in self.events if event.kind.startswith(prefix)]


def relay_for(upstream, continuous=True, creative_path='/creative'):
    from relay import RelayServer
    from sessions import SessionManager
    sink = Sink()
    origin = upstream['origin']
    config = AdConfig(True, False, False, '', origin, continuous, origin + creative_path)
    manager = SessionManager(HttpClient(UrlPolicy([origin]), read_timeout=1), sink, ad_config=config)
    server = RelayServer(manager)
    server.start()
    return server, manager, sink


def fetch(url, timeout=10):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    connection.request('GET', parts.path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def icy_source(upstream, codec=cs.MP3, markers=b'AAAAAABBBBBBBBCCCCCC', start=6, end=14):
    fixture = _tools('continuous_splice_fixtures')
    source = frames(codec, markers)
    frame = len(source[0])
    stream, _ = fixture.icy_interleave(b''.join(source), frame,
                                       {start * frame: START, end * frame: END})
    upstream.update(icy=stream, metaint=frame, mime='audio/mpeg' if codec == cs.MP3 else 'audio/aac')
    return source


@pytest.mark.parametrize('codec', [cs.MP3, cs.AAC])
def test_relay_splices_fixture_station_and_fires_no_tracking(upstream, codec):
    source = icy_source(upstream, codec)
    creative = b''.join(frames(codec, b'xyzxyzxy'))
    upstream['creative'] = creative
    server, manager, sink = relay_for(upstream)
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        status, body = fetch(session.playback_url)
    finally:
        server.close()
    assert status == 200
    assert body == b''.join(source[:6]) + creative + b''.join(source[14:])
    assert sink.kinds() == ['continuous_replacement_start', 'continuous_replacement_end']
    assert upstream['requests'].count('/creative') == 1
    assert not [path for path in upstream['requests'] if path not in ('/s', '/creative')]
    record = next(e for e in sink.events if e.kind == 'continuous_replacement_end').to_dict()
    assert record['details']['reason'] == 'creative_exhausted' and 'url' not in str(record).lower()


@pytest.mark.parametrize('case', ['default_off', 'not_fixture', 'other_origin'])
def test_relay_default_and_unqualified_sessions_are_unchanged(upstream, case):
    source = icy_source(upstream)
    upstream['creative'] = b''.join(frames(cs.MP3, b'xyz'))
    server, manager, sink = relay_for(upstream, continuous=case != 'default_off',
                                      creative_path='/creative')
    if case == 'other_origin':
        manager.ad_config = AdConfig(True, False, False, '', upstream['origin'], True,
                                     'http://127.0.0.1:9/creative')
    uuid = 'real-station' if case == 'not_fixture' else 'fixture-30218'
    try:
        session = manager.create(Station(uuid, 'Splice', upstream['origin'] + '/s', '', False))
        assert not session.continuous_splice_enabled
        status, body = fetch(session.playback_url)
    finally:
        server.close()
    assert status == 200 and body == b''.join(source)
    assert not sink.kinds() and '/creative' not in upstream['requests']
    assert [e.kind for e in sink.events].count('break_start') == 1  # Observation still logged.


@pytest.mark.parametrize('status, creative, reason', [
    (404, b'', 'no_fill'), (200, b'not audio at all' * 40, 'creative_unparseable'),
    (200, None, 'format_mismatch')])
def test_relay_no_fill_and_bad_creative_keep_source(upstream, status, creative, reason):
    source = icy_source(upstream)
    upstream['creative'] = (b''.join(frames(cs.MP3, b'xyz', mono=True))
                            if creative is None else creative)
    upstream['creative_status'] = status
    server, manager, sink = relay_for(upstream)
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        status, body = fetch(session.playback_url)
    finally:
        server.close()
    assert body == b''.join(source)
    skipped = next(e for e in sink.events if e.kind == 'continuous_replacement_skipped')
    assert skipped.details['reason'] == reason


def _wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


def _stream_in_background(url):
    result = {}
    def worker():
        try:
            result['response'] = fetch(url)
        except OSError as error:
            result['error'] = error
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, result


@pytest.mark.parametrize('action', ['stop', 'switch'])
def test_stop_and_switch_mid_break_release_splicer_and_restart_replays(upstream, action):
    icy_source(upstream, markers=b'AA' + b'B' * 400, start=2, end=402)
    upstream['pace'] = .02
    creative = b''.join(frames(cs.MP3, b'xyz' * 100))
    upstream['creative'] = creative
    server, manager, sink = relay_for(upstream)
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        thread, _ = _stream_in_background(session.playback_url)
        assert _wait(lambda: 'continuous_replacement_start' in sink.kinds())
        if action == 'stop':
            manager.stop(session.id)
        else:
            manager.create(Station('fixture-30219', 'Other', upstream['origin'] + '/other', '', False))
        thread.join(5)
        assert not thread.is_alive()
        assert _wait(lambda: 'continuous_replacement_end' in sink.kinds())
        end = next(e for e in sink.events if e.kind == 'continuous_replacement_end')
        assert end.details['reason'] == 'stopped'
        assert session.cancel.is_set() and session._splice_creative is None
        # Restart: a new session fetches again and replaces from creative frame 0.
        upstream['pace'] = 0
        icy_source(upstream)
        restarted = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        status, body = fetch(restarted.playback_url)
        assert body.startswith(b''.join(frames(cs.MP3, b'AAAAAA')) + creative[:len(frames(cs.MP3, b'x')[0])])
    finally:
        server.close()


def test_upstream_network_failure_mid_break_closes_cleanly(upstream):
    source = icy_source(upstream, markers=b'AA' + b'B' * 60, start=2, end=61)
    upstream['creative'] = b''.join(frames(cs.MP3, b'xyz' * 30))
    upstream['drop_after'] = len(upstream['icy']) // 2
    server, manager, sink = relay_for(upstream)
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        try:
            status, body = fetch(session.playback_url)
        except http.client.IncompleteRead:
            pass
        assert _wait(lambda: 'continuous_replacement_end' in sink.kinds())
        end = next(e for e in sink.events if e.kind == 'continuous_replacement_end')
        assert end.details['reason'] == 'stopped' and session.cancel.is_set()
        assert manager.get(session.id) is None
        assert any(e.kind == 'stream_error' for e in sink.events)
    finally:
        server.close()
    assert len(source) == 62


# ---------------------------------------------------------------------------
# Real encoded audio: decoded boundary accuracy (needs FFmpeg + generated assets).

FFMPEG = shutil.which('ffmpeg')


@pytest.fixture(scope='module')
def encoded():
    if FFMPEG is None:
        pytest.skip('FFmpeg not installed')
    fixture = _tools('continuous_splice_fixtures')
    names = [prefix + '.' + codec for codec in ('mp3', 'aac')
             for prefix in ('source', 'creative-short', 'creative-long', 'creative-mismatch')]
    if not all((ASSETS / name).is_file() for name in names):
        fixture.generate(ASSETS, FFMPEG)
    return fixture


@pytest.mark.parametrize('codec', [cs.MP3, cs.AAC])
@pytest.mark.parametrize('length', ['short', 'long'])
def test_decoded_boundaries_are_frame_accurate(encoded, codec, length):
    fixture = encoded
    source = (ASSETS / ('source.' + codec)).read_bytes()
    creative = cs.prepare_creative(codec, (ASSETS / ('creative-%s.%s' % (length, codec))).read_bytes())
    stream, placed, _ = fixture.looped_icy_stream(codec, source, 1)
    events = Events()
    splicer = cs.ContinuousSplicer(codec, creative, events)
    decoder = None
    decoder = IcyDecoder(fixture.METAINT, lambda value: splicer.observe_metadata(
        value, decoder.audio_position))
    output = bytearray()
    for start in range(0, len(stream), 4093):
        output += splicer.feed(decoder.feed(stream[start:start + 4093]))
    output += splicer.finish()
    start = events.first('continuous_replacement_start')
    end = events.first('continuous_replacement_end')
    # Bytes outside the replacement are the source's own bytes.
    assert bytes(output[:start['splice_offset']]) == source[:start['splice_offset']]
    tail = len(source) - end['rejoin_offset']
    assert bytes(output[-tail:]) == source[end['rejoin_offset']:]
    frame_samples = 1152 if codec == cs.MP3 else 1024
    pcm, warnings = fixture.decode_pcm(FFMPEG, bytes(output), codec)
    assert 'error' not in warnings.lower()
    runs = fixture.transitions(fixture.tone_track(pcm))
    tones = [tone for _, tone in runs]
    creative_at = tones.index(fixture.CREATIVE)
    # ICY can only signal at a metaint boundary, after the source ad began:
    # source-ad audio before the splice is the provider's quantisation.
    assert tones[:creative_at] in ([fixture.PROGRAMME_A],
                                   [fixture.PROGRAMME_A, fixture.SOURCE_AD])
    assert tones.count(fixture.CREATIVE) == 1 and tones[-1] == fixture.PROGRAMME_C
    after = tones[creative_at + 1:]
    if length == 'short':
        assert end['reason'] == 'creative_exhausted'
        assert after == [fixture.SOURCE_AD, fixture.PROGRAMME_C]
    else:
        assert end['reason'] == 'truncated' and after == [fixture.PROGRAMME_C]
    window = 441
    delay = 2 * frame_samples + window  # Codec delay plus one analysis window.
    # MDCT overlap-add can surface the new content up to one frame early.
    splice_heard = runs[creative_at][0]
    assert -frame_samples <= splice_heard - start['start_sample'] <= delay
    rejoin_heard = runs[creative_at + 1][0]
    assert -frame_samples <= rejoin_heard - end['rejoin_sample'] <= delay + window
    # Unclear audio around the rejoin is bounded (MP3 reservoir + overlap).
    gap = [g for g in fixture.gaps(fixture.tone_track(pcm))
           if abs(g[0] - end['rejoin_sample']) < 3 * frame_samples]
    assert sum(windows for _, windows in gap) * window <= 2 * frame_samples


def test_fixture_server_serves_paced_icy_splice_stream(encoded):
    server_module = _tools('fixture_server')
    server = server_module.make_server(ASSETS.parent, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        connection.request('GET', '/splice/source.mp3')
        response = connection.getresponse()
        assert response.getheader('icy-metaint') == str(encoded.METAINT)
        started = time.monotonic()
        body = response.read(encoded.METAINT * 5)
        elapsed = time.monotonic() - started
        connection.close()
        metadata = []
        decoder = IcyDecoder(encoded.METAINT, metadata.append)
        audio = decoder.feed(body)
        assert audio == (ASSETS / 'source.mp3').read_bytes()[:len(audio)]
        # Two burst intervals, then real time: ~1 s per 16000 bytes at 128 kb/s.
        assert elapsed >= 1.5
        status, creative = fetch('http://127.0.0.1:%d/splice/creative-short.aac' % server.server_port)
        assert status == 200 and cs.prepare_creative(cs.AAC, creative).frames
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()


def test_protected_mp3_header_split_inside_crc_keeps_lock():
    source = frames(cs.MP3, b'ABCDEFGH', crc=True)
    data = b''.join(source)
    for cut in range(len(source[0]) * 2, len(source[0]) * 2 + 9):
        assembler = cs.FrameAssembler(cs.MP3)
        units = assembler.feed(data[:cut]) + assembler.feed(data[cut:]) + assembler.finish()
        assert [u.kind for u in units] == ['frame'] * 8, cut


def test_lost_sync_during_break_returns_source_and_disables():
    source = frames(cs.MP3, b'AABB')
    noise = b'not an mp3 frame at all ' * 40
    events = Events()
    splicer = cs.ContinuousSplicer(cs.MP3, creative_of(cs.MP3, b'xyzxyzxyz'), events)
    splicer.observe_metadata(START, len(source[0]) * 2)
    output = splicer.feed(b''.join(source) + noise) + splicer.feed(noise) + splicer.finish()
    creative = creative_of(cs.MP3, b'xyzxyzxyz')
    assert output == b''.join(source[:2]) + b''.join(creative.frames[:2]) + noise + noise
    assert events.kinds() == ['continuous_replacement_start', 'continuous_replacement_end',
                              'continuous_splice_disabled']
    assert events[1][1]['reason'] == 'sync_lost'


def test_many_alternating_breaks_in_one_read_are_all_kept():
    source = frames(cs.AAC, b'P' * 200)
    frame = len(source[0])
    creative = creative_of(cs.AAC, b'xy')
    splicer = cs.ContinuousSplicer(cs.AAC, creative, Events())
    for index in range(100):  # 100 one-frame breaks queued before any audio.
        splicer.observe_metadata(START, (2 * index) * frame)
        splicer.observe_metadata(END, (2 * index + 1) * frame)
    output = splicer.feed(b''.join(source)) + splicer.finish()
    expected = b''.join(creative.frames[0] + source[i] for i in range(1, 200, 2))
    assert output == expected


def test_metadata_burst_cannot_crowd_out_the_end_signal():
    source = frames(cs.AAC, b'AABBCC')
    splicer = cs.ContinuousSplicer(cs.AAC, creative_of(cs.AAC, b'xyzxyz'), Events())
    for _ in range(200):
        splicer.observe_metadata(START, len(source[0]) * 2)
    splicer.observe_metadata(END, len(source[0]) * 4)
    output = splicer.feed(b''.join(source)) + splicer.finish()
    creative = creative_of(cs.AAC, b'xyzxyz')
    assert output == b''.join(source[:2]) + b''.join(creative.frames[:2]) + b''.join(source[4:])


def test_session_skips_creative_fetch_without_icy_or_supported_codec(upstream):
    for headers in ({'icy-metaint': '0', 'content-type': 'audio/mpeg'},
                    {'icy-metaint': '16000', 'content-type': 'audio/ogg'}):
        from sessions import PlaybackSession
        from types import SimpleNamespace
        origin = upstream['origin']
        config = AdConfig(True, False, False, '', origin, True, origin + '/creative')
        source = SimpleNamespace(protocol='continuous', final_url=origin + '/s',
                                 response=SimpleNamespace(headers=headers))
        session = PlaybackSession(Station('fixture-30218', 'S', origin + '/s', '', False), source,
                                  threading.Event(), '', Sink(), 8, config,
                                  HttpClient(UrlPolicy([origin])))
        assert not session.continuous_splice_enabled
    assert '/creative' not in upstream['requests']


def test_back_to_back_breaks_without_programme_frame_keep_event_pairs():
    source = frames(cs.MP3, b'AABBBBCC')
    frame = len(source[0])
    events = Events()
    splicer = cs.ContinuousSplicer(cs.MP3, creative_of(cs.MP3, b'xy'), events)
    splicer.observe_metadata(START, 2 * frame)
    splicer.observe_metadata(END, 4 * frame)
    splicer.observe_metadata(dict(START, durationMilliseconds='1'), 4 * frame)
    splicer.feed(b''.join(source))
    splicer.finish()
    assert events.kinds() == ['continuous_replacement_start', 'continuous_replacement_end',
                              'continuous_replacement_start', 'continuous_replacement_end']


def test_start_after_an_unanswered_break_times_out_is_kept():
    codec = cs.MP3
    source = frames(codec, b'P' * 30)
    frame = len(source[0])
    creative = creative_of(codec, b'xyxyxyxyxy')
    old = cs.BREAK_GRACE_SECONDS
    cs.BREAK_GRACE_SECONDS = 0
    try:
        events = Events()
        splicer = cs.ContinuousSplicer(codec, creative, events)
        splicer.observe_metadata(dict(START, durationMilliseconds='50'), 2 * frame)
        splicer.observe_metadata(dict(START, durationMilliseconds='50'), 20 * frame)
        splicer.feed(b''.join(source))
        splicer.finish()
    finally:
        cs.BREAK_GRACE_SECONDS = old
    assert events.kinds().count('continuous_replacement_start') == 2


def test_fixture_builder_rejects_signals_sharing_a_metadata_block():
    fixture = _tools('continuous_splice_fixtures')
    with pytest.raises(ValueError):
        fixture.icy_interleave(b'a' * 64, 16, {0: START, 16: END})
    stream, placed = fixture.icy_interleave(b'a' * 64, 16, {16: START, 32: END})
    assert [p[1] for p in placed] == [16, 32]


# ---------------------------------------------------------------------------
# Follow-ups to the PR #9 review.

def test_signals_held_across_reads_cannot_evict_a_pending_end(monkeypatch):
    monkeypatch.setattr(cs, 'MAX_SIGNALS', 8)
    source = frames(cs.AAC, b'AABBCCCC')
    frame = len(source[0])
    creative = creative_of(cs.AAC, b'xyzxyz')
    events = Events()
    splicer = cs.ContinuousSplicer(cs.AAC, creative, events)
    audio = b''.join(source)
    splicer.observe_metadata(START, 2 * frame)
    output = bytearray(splicer.feed(audio[:4 * frame]))
    # The end arrives at frame 4's start; repeats arrive on later reads while
    # that frame is still incomplete, so it stays held across every read.
    splicer.observe_metadata(END, 4 * frame)
    for position in range(4 * frame, 4 * frame + cs.MAX_SIGNALS):
        output += splicer.feed(audio[position:position + 1])
        splicer.observe_metadata(END, position + 1)
    output += splicer.feed(audio[4 * frame + cs.MAX_SIGNALS:]) + splicer.finish()
    assert bytes(output) == b''.join(source[:2]) + b''.join(creative.frames[:2]) + b''.join(source[4:])
    assert events.kinds()[-1] == 'continuous_splice_disabled'
    assert events[-1][1]['reason'] == 'signal_overflow'


def test_break_timeout_is_media_time_across_a_source_rate_change(monkeypatch):
    monkeypatch.setattr(cs, 'BREAK_GRACE_SECONDS', 0)
    # 48 kHz frames, then 24 kHz frames: each 1024-sample frame is 21.3 ms,
    # then 42.7 ms. A 100 ms break opened at 48 kHz has ended by frame 5.
    source = frames(cs.AAC, b'AABB', rate_index=3) + frames(cs.AAC, b'CCDDEEFF', rate_index=6)
    frame = len(source[0])
    creative = creative_of(cs.AAC, b'xyzxyz', rate_index=6)
    output, events, _ = run(cs.AAC, source, creative, {
        (2, 0): dict(START, durationMilliseconds='100'), (6, 0): START, (8, 0): END})
    assert events.kinds() == ['continuous_replacement_skipped', 'continuous_break_unresolved',
                              'continuous_replacement_start', 'continuous_replacement_end']
    assert output == b''.join(source[:6]) + b''.join(creative.frames[:2]) + b''.join(source[8:])
    assert frame == len(source[-1])


def test_rejoin_is_reported_only_for_a_forwarded_source_frame():
    source = frames(cs.MP3, b'AABBBBCC')
    frame = len(source[0])
    creative = creative_of(cs.MP3, b'xyzxyz')
    output, events, _ = run(cs.MP3, source, creative,
                            {(2, 0): START, (3, 1): END, (4, 0): START, (6, 0): END})
    assert events.kinds() == ['continuous_replacement_start', 'continuous_replacement_end',
                              'continuous_replacement_start', 'continuous_replacement_end']
    first, second = events[1][1], events[3][1]
    assert first['reason'] == 'truncated' and first['frames_replaced'] == 2
    assert 'rejoin_offset' not in first and 'rejoin_sample' not in first
    assert 'rejoin_reservoir_bytes' not in first
    assert second['reason'] == 'truncated' and second['rejoin_offset'] == 6 * frame
    assert second['rejoin_sample'] == 6 * 1152
    assert output == (b''.join(source[:2]) + b''.join(creative.frames[:2])
                      + b''.join(creative.frames[:2]) + b''.join(source[6:]))


def _splice_stream(upstream, audio, signals, frame):
    fixture = _tools('continuous_splice_fixtures')
    stream, _ = fixture.icy_interleave(audio, frame, signals)
    upstream.update(icy=stream, metaint=frame, mime='audio/mpeg')
    return stream


def test_relay_rechecks_stop_after_finish_before_writing_the_tail(upstream):
    source = frames(cs.MP3, b'AAAA')
    frame = len(source[0])
    tail = source[0][:frame // 2]
    _splice_stream(upstream, b''.join(source) + tail, {2 * frame: START}, frame)
    upstream['creative'] = b''.join(frames(cs.MP3, b'xy'))
    server, manager, sink = relay_for(upstream)
    sessions = []
    emit = sink.emit
    def stopping(event):
        emit(event)
        # The listener stops while finish() reports the pending end.
        if event.kind == 'continuous_replacement_end' and sessions:
            sessions[0].cancel.set()
    sink.emit = stopping
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        sessions.append(session)
        status, body = fetch(session.playback_url)
    finally:
        server.close()
    assert status == 200
    assert body == b''.join(source[:2]) + upstream['creative']
    assert sink.kinds() == ['continuous_replacement_start', 'continuous_replacement_end']


def test_relay_flushes_held_programme_tail_on_upstream_read_failure(upstream):
    source = frames(cs.MP3, b'AAAA')
    frame = len(source[0])
    stream = _splice_stream(upstream, b''.join(source), {}, frame)
    upstream['creative'] = b''.join(frames(cs.MP3, b'xy'))
    # Three frames and their metadata bytes, then half a frame, then a reset.
    upstream['drop_after'] = 3 * (frame + 1) + frame // 2
    server, manager, sink = relay_for(upstream)
    try:
        session = manager.create(Station('fixture-30218', 'Splice', upstream['origin'] + '/s', '', False))
        assert session.continuous_splice_enabled
        status, body = fetch(session.playback_url)
        assert _wait(lambda: any(e.kind == 'stream_error' for e in sink.events))
    finally:
        server.close()
    assert len(stream) > upstream['drop_after']
    assert status == 200 and body == b''.join(source[:3]) + source[3][:frame // 2]
    error = next(e for e in sink.events if e.kind == 'stream_error')
    assert error.details == {'reason': 'incomplete_upstream_body'}


def test_continuous_fixture_create_uses_extended_control_timeout(kodi, monkeypatch):
    from control import ServiceDescriptor
    from urllib.parse import urlencode
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_continuous=True,
                          ad_continuous_creative='http://127.0.0.1:8765/splice/creative.mp3')
    descriptor = ServiceDescriptor(32123, 'a' * 48, 'b' * 64)
    runtime.publish_descriptor(profile, descriptor)
    timeouts = []

    class Client:
        def __init__(self, descriptor, timeout=10):
            self.descriptor = descriptor
            timeouts.append(timeout)
        def health(self): return {'status': 'ok', 'instance_id': descriptor.instance_id}
        def create(self, station):
            return {'id': 'c' * 48, 'mime_type': 'audio/mpeg',
                    'playback_url': 'http://127.0.0.1:32123/media/' + 'c' * 48 + '/' + 'd' * 48}
        def stop(self, session_id): pass

    monkeypatch.setattr(runtime, 'ControlClient', Client)
    monkeypatch.setattr(runtime.RadioBrowser, 'click', lambda *args: None)
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', '7',
                        '?' + urlencode({'mode': 'play_fixture', 'id': 'fixture-30218'})])
    assert timeouts[-1] == runtime.AD_CREATE_TIMEOUT
    assert state.resolved[0][0]
