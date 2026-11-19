#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Controlled MP3/ADTS/ICY fixtures with known tone transitions for splice tests.

Run: python3 tools/continuous_splice_fixtures.py
Writes tests/generated-audio/continuous/ (ignored by git). FFmpeg is a developer
tool only; the add-on never invokes it.

Source programme: 440 Hz for 10 s, then a 1000 Hz "source ad" for 8 s, then
660 Hz for 10 s, encoded as one continuous stream so every frame boundary is
real. Creatives: 1500 Hz for 6 s (short), 12 s (long), and a 6 s creative at a
different sample rate (format mismatch). The ICY builder interleaves metadata
at icy-metaint boundaries, places adw_ad=true/false at the first boundary at or
after each source transition, and can split metadata across reads.
"""
import argparse
from array import array
import math
from pathlib import Path
import shutil
import subprocess
import sys

_LIB = Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio' / 'resources' / 'lib'
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


RATE = 44100
PROGRAMME_A, SOURCE_AD, PROGRAMME_C, CREATIVE = 440, 1000, 660, 1500
SECTIONS = ((PROGRAMME_A, 10.0), (SOURCE_AD, 8.0), (PROGRAMME_C, 10.0))
AD_START_SECONDS, AD_END_SECONDS = 10.0, 18.0
METAINT = 16000
TONES = (PROGRAMME_A, SOURCE_AD, PROGRAMME_C, CREATIVE)
DEFAULT_ASSETS = Path(__file__).resolve().parents[1] / 'tests' / 'generated-audio' / 'continuous'

_CODECS = {
    'mp3': (['-c:a', 'libmp3lame', '-b:a', '128k', '-write_xing', '0',
             '-id3v2_version', '0', '-f', 'mp3'], 'mp3'),
    'aac': (['-c:a', 'aac', '-b:a', '128k', '-f', 'adts'], 'aac'),
}


def _tones(sections, rate):
    inputs, labels = [], []
    for index, (frequency, seconds) in enumerate(sections):
        inputs.append('sine=frequency=%d:sample_rate=%d:duration=%s[s%d]'
                      % (frequency, rate, seconds, index))
        labels.append('[s%d]' % index)
    return ';'.join(inputs) + ';%sconcat=n=%d:v=0:a=1,volume=0.5[out]' % (
        ''.join(labels), len(sections))


def encode(ffmpeg, sections, codec, path, rate=RATE):
    options, _ = _CODECS[codec]
    subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
                    '-filter_complex', _tones(sections, rate), '-map', '[out]',
                    '-ac', '2', '-ar', str(rate)] + options + [str(path)], check=True)


def generate(assets, ffmpeg):
    assets = Path(assets)
    assets.mkdir(parents=True, exist_ok=True)
    for codec in _CODECS:
        encode(ffmpeg, SECTIONS, codec, assets / ('source.' + codec))
        encode(ffmpeg, ((CREATIVE, 6.0),), codec, assets / ('creative-short.' + codec))
        encode(ffmpeg, ((CREATIVE, 12.0),), codec, assets / ('creative-long.' + codec))
        encode(ffmpeg, ((CREATIVE, 6.0),), codec, assets / ('creative-mismatch.' + codec), 48000)
    return assets


def frame_offsets(codec, data):
    """Return (offset, samples) for each frame using the add-on's own parser."""
    from continuous_splice import FrameAssembler
    assembler = FrameAssembler(codec)
    units = assembler.feed(data) + assembler.finish()
    return [(unit.offset, unit.header.samples) for unit in units if unit.kind == 'frame']


def byte_at_seconds(codec, data, seconds, rate=RATE):
    """Return the offset of the frame containing the given source time."""
    target = int(seconds * rate)
    sample = 0
    for offset, samples in frame_offsets(codec, data):
        if sample + samples > target:
            return offset
        sample += samples
    raise ValueError('Time beyond the fixture')


def metadata_block(fields):
    """Encode one ICY metadata block (length byte plus padded text)."""
    items = fields.items() if isinstance(fields, dict) else fields
    text = ''.join("%s='%s';" % (key, value) for key, value in items)
    body = text.encode('utf-8')
    padded = body.ljust(((len(body) + 15) // 16) * 16, b'\0')
    if len(padded) > 255 * 16:
        raise ValueError('ICY metadata too long')
    return bytes([len(padded) // 16]) + padded


def adw(flag, duration_ms=None, title='Fixture'):
    fields = [('StreamTitle', title), ('adw_ad', flag)]
    if duration_ms is not None:
        fields.append(('durationMilliseconds', str(duration_ms)))
    fields.append(('insertionType', 'midroll'))
    return fields


def icy_interleave(audio, metaint, signals):
    """Interleave audio with metadata. signals maps byte offset -> fields.

    Each signal is written at the first metaint boundary at or after its byte
    offset, as an ICY server does. Other boundaries carry empty blocks. Returns
    (stream bytes, {fields index: audio offset where it was placed}).
    """
    pending = sorted(signals.items())
    boundaries = [-(-offset // metaint) for offset, _ in pending]
    if len(set(boundaries)) != len(boundaries):
        # One metadata block cannot carry two transitions; do not invent a break.
        raise ValueError('Two signals share one metadata boundary')
    out = bytearray()
    placed = []
    position = 0
    while position < len(audio):
        chunk = audio[position:position + metaint]
        out.extend(chunk)
        position += len(chunk)
        if len(chunk) < metaint:
            break
        if pending and pending[0][0] <= position:
            offset, fields = pending.pop(0)
            out.extend(metadata_block(fields))
            placed.append((offset, position, fields))
        else:
            out.append(0)
    return bytes(out), placed


def looped_icy_stream(codec, source, loops, metaint=METAINT):
    """Repeat the source with adw_ad signals at each loop's ad start and end.

    Returns (stream bytes, placements, audio bytes per second).
    """
    start = byte_at_seconds(codec, source, AD_START_SECONDS)
    end = byte_at_seconds(codec, source, AD_END_SECONDS)
    signals = {}
    for loop in range(loops):
        base = loop * len(source)
        signals[base + start] = adw('true', int((AD_END_SECONDS - AD_START_SECONDS) * 1000))
        signals[base + end] = adw('false')
    stream, placed = icy_interleave(source * loops, metaint, signals)
    seconds = sum(section[1] for section in SECTIONS)
    return stream, placed, len(source) / seconds


def decode_pcm(ffmpeg, data, suffix, rate=RATE):
    """Decode compressed bytes to mono signed 16-bit samples; return (samples, stderr)."""
    fmt = {'mp3': 'mp3', 'aac': 'aac'}[suffix]
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'warning',
                             '-f', fmt, '-i', 'pipe:0', '-ac', '1', '-ar', str(rate),
                             '-f', 's16le', 'pipe:1'], input=data, capture_output=True, check=True)
    samples = array('h')
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    return samples, result.stderr.decode('utf-8', 'replace')


def _goertzel(samples, start, length, frequency, rate):
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    previous = previous2 = 0.0
    for value in samples[start:start + length]:
        current = value + coefficient * previous - previous2
        previous2, previous = previous, current
    return previous2 ** 2 + previous ** 2 - coefficient * previous * previous2


def tone_track(samples, rate=RATE, window=441, tones=TONES):
    """Return [(start sample, dominant tone or None)] for consecutive windows."""
    track = []
    for start in range(0, len(samples) - window + 1, window):
        powers = [(_goertzel(samples, start, window, tone, rate), tone) for tone in tones]
        energy = sum(float(v) * v for v in samples[start:start + window])
        best = max(powers)
        # A clean tone carries about half the window energy in its bin.
        dominant = best[1] if energy and best[0] / (energy * window / 4) > 0.5 else None
        track.append((start, dominant))
    return track


def transitions(track):
    """Collapse a tone track into [(first sample, tone)] runs, dropping unclear windows."""
    runs = []
    for start, tone in track:
        if tone is None:
            continue
        if not runs or runs[-1][1] != tone:
            runs.append((start, tone))
    return runs


def gaps(track, minimum_windows=1):
    """Return [(start sample, windows)] of consecutive windows without a clear tone."""
    found, run = [], None
    for start, tone in track:
        if tone is None:
            run = (run[0], run[1] + 1) if run else (start, 1)
        elif run:
            if run[1] >= minimum_windows:
                found.append(run)
            run = None
    if run and run[1] >= minimum_windows:
        found.append(run)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--assets', type=Path, default=DEFAULT_ASSETS)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error('FFmpeg is required to generate developer fixtures')
    print('Generated continuous splice fixtures in %s' % generate(args.assets, args.ffmpeg))


if __name__ == '__main__':
    main()
