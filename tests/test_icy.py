# SPDX-License-Identifier: GPL-2.0-or-later
"""ICY framing must never lose audio at arbitrary upstream read boundaries."""
import pytest

from icy import IcyDecoder
from models import Event


def block(payload):
    padding = (-len(payload)) % 16
    padded = payload + b'\0' * padding
    return bytes([len(padded) // 16]) + padded


def test_every_split_point_preserves_audio_and_emits_each_metadata_block():
    wire = (b'abcde' + block(b"StreamTitle='One';") + b'12345' + b'\0'
            + b'vwxyz' + block(b"StreamTitle='Two';StreamUrl='';") + b'END')
    for split in range(len(wire) + 1):
        metadata = []
        decoder = IcyDecoder(5, metadata.append)
        output = decoder.feed(wire[:split]) + decoder.feed(wire[split:])
        decoder.finish()
        assert output == b'abcde12345vwxyzEND', split
        assert metadata == [{'StreamTitle': 'One'}, {'StreamTitle': 'Two', 'StreamUrl': ''}], split


def test_single_byte_reads_and_duplicate_blocks_are_observed_individually():
    metadata = []
    decoder = IcyDecoder(1, metadata.append)
    wire = b'a' + block(b"StreamTitle='Same';") + b'b' + block(b"StreamTitle='Same';")
    output = b''.join(decoder.feed(bytes([value])) for value in wire)
    decoder.finish()
    assert output == b'ab'
    assert metadata == [{'StreamTitle': 'Same'}, {'StreamTitle': 'Same'}]


@pytest.mark.parametrize('metaint', [0, -1, None, '5', 1.5, True])
def test_invalid_metaint_is_rejected(metaint):
    with pytest.raises(ValueError):
        IcyDecoder(metaint, lambda metadata: None)


def test_invalid_encoding_does_not_interrupt_audio():
    metadata = []
    decoder = IcyDecoder(2, metadata.append)
    assert decoder.feed(b'ab' + block(b"StreamTitle='bad\xff';") + b'cd') == b'abcd'
    decoder.finish()
    assert metadata == [{'StreamTitle': 'bad\ufffd'}]


@pytest.mark.parametrize('tail', [b'', b'\x01', b'\x02short'])
def test_truncated_length_or_metadata_does_not_leak_into_audio(tail):
    metadata = []
    decoder = IcyDecoder(4, metadata.append)
    assert decoder.feed(b'abcd' + tail) == b'abcd'
    decoder.finish()
    decoder.finish()
    assert metadata == []
    with pytest.raises(ValueError):
        decoder.feed(b'more')


def test_maximum_metadata_block_and_following_audio():
    metadata = []
    decoder = IcyDecoder(3, metadata.append)
    value = 'x' * (4080 - len("StreamTitle='';"))
    payload = ("StreamTitle='" + value + "';").encode('ascii')
    wire = b'abc\xff' + payload + b'XYZ\x00!'
    assert b''.join(decoder.feed(wire[index:index + 7]) for index in range(0, len(wire), 7)) == b'abcXYZ!'
    decoder.finish()
    assert metadata == [{'StreamTitle': value}]


def test_title_semicolons_and_apostrophes_are_preserved():
    metadata = []
    decoder = IcyDecoder(1, metadata.append)
    assert decoder.feed(b'a' + block(b"StreamTitle='DJ's song; live';StreamUrl='';")) == b'a'
    decoder.finish()
    assert metadata == [{'StreamTitle': "DJ's song; live", 'StreamUrl': ''}]


def test_advertisement_title_only_emits_metadata_observation():
    events = []
    def observe(metadata):
        events.append(Event('metadata_observed', 'session', 'station', 'icy',
                            'icy_metadata', metadata, '2026-10-21T00:00:00Z', 1.0))
    decoder = IcyDecoder(1, observe)
    assert decoder.feed(b'a' + block(b"StreamTitle='advertisement';")) == b'a'
    decoder.finish()
    assert [event.kind for event in events] == ['metadata_observed']
    assert events[0].evidence == {'StreamTitle': 'advertisement'}
