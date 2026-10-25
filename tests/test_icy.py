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


def test_metadata_observer_failure_is_logged_once_without_discarding_audio(caplog):
    def fail(_metadata):
        raise RuntimeError('private response body')

    decoder = IcyDecoder(2, fail)
    wire = (b'ab' + block(b"StreamTitle='ad';") + b'cd' +
            block(b"StreamTitle='more';") + b'ef')
    assert decoder.feed(wire) == b'abcdef'
    warnings = [record for record in caplog.records
                if record.getMessage() == 'ICY metadata observer failed']
    assert len(warnings) == 1
    assert 'private response body' not in caplog.text


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


@pytest.fixture
def marker_session():
    from types import SimpleNamespace
    from models import Station
    from sessions import SessionManager
    from station_resolver import ResolvedSource
    events = []
    manager = SessionManager(
        sink=SimpleNamespace(emit=events.append), idle_timeout=1000,
        resolver=lambda *args: ResolvedSource('http://radio.example/live', 'continuous'))
    session = manager.create(Station('station', 'Radio', 'http://radio.example/live', '', False))
    yield manager, session, events
    manager.close()


@pytest.mark.parametrize('length,expected', [(256, '*' * 256), (257, None), (4000, None)])
def test_session_latest_candidate_is_bounded_and_isolated(marker_session, length, expected):
    manager, session, events = marker_session
    session.observe_icy_metadata({'StreamTitle': '*' * length}, '2026-10-23T00:00:00Z', 10)
    assert session.latest_icy_candidate() == expected
    assert events[0].kind == 'metadata_observed'
    assert events[0].evidence['StreamTitle'] == '*' * length
    marker_events = [event for event in events if event.source == 'icy_marker']
    if length == 256:
        assert marker_events[0].kind == 'icy_marker_candidate'
        assert marker_events[0].evidence == {'StreamTitle': '*' * 256}
    else:
        assert marker_events == []
    replacement = manager.create(session.station)
    assert replacement.latest_icy_candidate() is None
    replacement.observe_icy_metadata({'StreamTitle': ' ad break '}, 'now', 11)
    assert replacement.latest_icy_candidate() == 'AD BREAK'
    assert events[-1].session_id == replacement.id


def test_service_tick_expires_markers_during_active_audio(marker_session, monkeypatch):
    import sessions
    from types import SimpleNamespace
    manager, session, events = marker_session
    session.observe_icy_metadata({'StreamTitle': '-'}, 'start', 10)
    clock = SimpleNamespace(monotonic=lambda: 310)
    monkeypatch.setattr(sessions, 'time', clock)
    with session.acquire(session.playback_url):
        manager.expire()
        assert not any(event.kind == 'break_unresolved' for event in events)
        clock.monotonic = lambda: 311
        manager.expire()
        manager.expire()
        assert not session.cancel.is_set()
        assert session.latest_icy_candidate() is None
    unresolved = [event for event in events if event.kind == 'break_unresolved']
    assert len(unresolved) == 1
    assert unresolved[0].details['reason'] == 'timeout'
    assert unresolved[0].monotonic_at == 311
    assert not any(event.kind == 'break_end' for event in events)


def test_session_expire_markers_prunes_candidate_without_inventing_end(marker_session):
    _, session, events = marker_session
    session.observe_icy_metadata({'StreamTitle': '#'}, 'start', 10)
    session.expire_icy_markers(311)
    assert session.latest_icy_candidate() is None
    assert [event.kind for event in events] == [
        'metadata_observed', 'icy_marker_candidate', 'break_start', 'break_unresolved']
