# SPDX-License-Identifier: GPL-2.0-or-later
"""The long developer fixture retires real media without an unbounded asset set."""
import json
from threading import Thread
from types import SimpleNamespace
from urllib.request import urlopen

from hls import parse_playlist
from tools import ad_fixture_server, generate_ad_audio


def test_extended_window_moves_past_retention_and_ends_only_at_its_last_window():
    initial = parse_playlist(ad_fixture_server.source_manifest('extended', 0),
                             'http://127.0.0.1/source/extended/index.m3u8')
    later = parse_playlist(ad_fixture_server.source_manifest('extended', 90),
                           'http://127.0.0.1/source/extended/index.m3u8')
    assert [segment.sequence for segment in initial.segments] == list(range(6))
    assert [segment.sequence for segment in later.segments] == list(range(90, 96))
    assert later.segments[0].uri.endswith('/ads/source090.ts')
    assert later.segments[-1].uri.endswith('/ads/source095.ts')
    assert '#EXT-X-ENDLIST' not in ad_fixture_server.source_manifest('extended', 121)

    final = ad_fixture_server.source_manifest('extended', 122)
    assert '#EXT-X-MEDIA-SEQUENCE:122\n' in final
    assert '/ads/source127.ts\n' in final
    assert '#EXT-X-ENDLIST\n' in final
    assert ad_fixture_server.source_manifest('extended', 10000) == final
    # The earlier starvation fixture remains intentionally unchanged.
    normal = ad_fixture_server.source_manifest('normal', 10000)
    assert '#EXT-X-MEDIA-SEQUENCE:18\n' in normal
    assert '/ads/source023.ts\n' in normal
    assert '#EXT-X-ENDLIST' not in normal


def test_extended_descriptions_preserve_existing_audio_and_identify_each_new_segment():
    original = json.loads(generate_ad_audio.DESCRIPTION.read_text())
    extended = generate_ad_audio.source_description(extended=True)
    assert generate_ad_audio.source_description() == original
    assert len(extended['segments']) == 133
    assert all(extended['segments'][name] == item
               for name, item in original['segments'].items())
    assert extended['segments']['source024.ts'] == {
        'section': 'programme_b_continuation', 'tone_hz': 1480, 'spoken': 'Programme B 24'}
    assert extended['segments']['source127.ts'] == {
        'section': 'programme_b_continuation', 'tone_hz': 3540, 'spoken': 'Programme B 127'}
    continuations = [extended['segments']['source%03d.ts' % index]
                     for index in range(6, 128)]
    assert len({item['tone_hz'] for item in continuations}) == 122
    assert len({item['spoken'] for item in continuations}) == 122
    assert 'source128.ts' not in extended['segments']


def test_extended_http_window_uses_media_time_and_reset_restores_its_origin(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(ad_fixture_server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    (tmp_path / 'source090.ts').write_bytes(b'controlled late segment')
    server = ad_fixture_server.make_server(tmp_path, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = 'http://127.0.0.1:%d' % server.server_port

    def read(path):
        with urlopen(origin + path, timeout=3) as response:
            return response.read()

    try:
        first = read('/source/extended/index.m3u8')
        assert read('/source/extended/index.m3u8') == first
        now[0] += 90 * 96256 / 48000 + .001
        later = read('/source/extended/index.m3u8')
        assert b'#EXT-X-MEDIA-SEQUENCE:90\n' in later
        assert b'/ads/source090.ts\n' in later
        assert read('/ads/source090.ts') == b'controlled late segment'
        assert json.loads(read('/stats'))['beacons'] == {}
        read('/reset')
        assert read('/source/extended/index.m3u8') == first
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
