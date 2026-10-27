# SPDX-License-Identifier: GPL-2.0-or-later
"""Controlled late breaks and starvation recovery retain consecutive media."""
import http.client
import json
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from ad_media import extract_adts
from ad_models import AdConfig, CachedSegment, MediaInfo, PreparedAd, ResolvedAd
from ad_timeline import AdTimeline
from hls import parse_playlist
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from url_policy import UrlPolicy
from tools import ad_fixture_server


SAMPLES = 94 * 1024
URL = 'http://127.0.0.1/source/fixture/index.m3u8'


def playlist(scenario, window):
    return parse_playlist(ad_fixture_server.source_manifest(scenario, window), URL)


def cached(name):
    return CachedSegment(name, Path('/unused/' + name),
                         MediaInfo('aac', 48000, 2, SAMPLES, 0, 93 * 1920))


def creative(name):
    items = tuple(cached(name + str(index)) for index in range(2))
    return PreparedAd(ResolvedAd(name, URL, 2 * SAMPLES / 48000, (), ()),
                      items, 2 * SAMPLES)


@pytest.mark.parametrize('scenario,start,end', [('late-break', 70, 72), ('recovery', 6, 8)])
def test_new_scenarios_have_one_complete_break_and_no_early_ad_cues(scenario, start, end):
    initial = playlist(scenario, 0)
    assert [segment.sequence for segment in initial.segments] == list(range(6))
    assert initial.cues == ()
    assert not any(line.startswith('#EXT-X-ENDLIST') for line in initial.lines)

    # A partially exposed break would already be immutable programme by the
    # time cue-in arrived, so hold that tail until the complete interval exists.
    for window in (start - 5, start - 4):
        partial = playlist(scenario, window)
        assert partial.segments[-1].sequence == start - 1
        assert partial.cues == ()
    complete = playlist(scenario, start - 3)
    assert [(cue.source, cue.sequence) for cue in complete.cues] == [
        ('EXT-X-CUE-OUT', start), ('EXT-X-CUE-IN', end)]
    assert [segment.sequence for segment in complete.segments] == list(range(start - 3, end + 1))

    last = playlist(scenario, 122)
    assert last.segments[-1].sequence == 127
    assert any(line.startswith('#EXT-X-ENDLIST') for line in last.lines)
    assert not any(line.startswith('#EXT-X-ENDLIST') for line in playlist(scenario, 121).lines)
    assert ad_fixture_server.source_manifest(scenario, 999) == ad_fixture_server.source_manifest(scenario, 122)


@pytest.mark.parametrize('scenario,start,end', [('late-break', 70, 72), ('recovery', 6, 8)])
def test_each_break_remains_replaceable_after_earlier_windows_were_delivered(scenario, start, end):
    timeline = AdTimeline(creative('pre'), 64)
    replacement = creative('ad')
    timeline.offer(replacement)
    published = {}
    placement = None
    for window in range(123):
        current = playlist(scenario, window)
        sources = {segment.uri: cached('source%03d' % segment.sequence)
                   for segment in current.segments}
        presentation = timeline.update(current, sources)
        for segment in presentation.segments:
            if segment.sequence in published:
                assert published[segment.sequence] == segment
            published[segment.sequence] = segment
        replacements = [item for item in presentation.placements if item.ad is replacement.ad]
        if replacements:
            assert window >= start - 3
            placement = replacements[0]
        elif window < start - 3:
            assert all(segment.placement_id is None or segment.resource_id.startswith('pre')
                       for segment in presentation.segments)

    expected = (['pre0', 'pre1'] + ['source%03d' % index for index in range(start)] +
                ['ad0', 'ad1'] + ['source%03d' % index for index in range(end, 128)])
    assert sorted(published) == list(range(130))
    assert [published[index].resource_id for index in range(130)] == expected
    assert [published[index].start_sample for index in range(130)] == [index * SAMPLES for index in range(130)]
    assert placement is not None
    assert (placement.start_sample, placement.end_sample) == ((start + 2) * SAMPLES, (end + 2) * SAMPLES)
    assert presentation.ended
    assert presentation.segments[0].sequence > 64  # Real output retirement occurred.


def test_recovery_stall_excludes_elapsed_hold_time_and_reset_restarts_it(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(ad_fixture_server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    (tmp_path / 'source070.ts').write_bytes(b'controlled late source')
    server = ad_fixture_server.make_server(tmp_path, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = 'http://127.0.0.1:%d' % server.server_port

    def read(path):
        with urlopen(origin + path, timeout=3) as response:
            return response.read()

    def recovery():
        return parse_playlist(read('/source/recovery/index.m3u8').decode(), URL)

    try:
        initial = recovery()
        assert initial.cues == ()
        hold = ad_fixture_server.RECOVERY_HOLD_SECONDS
        for elapsed in (5, hold - .001, hold):
            now[0] = 1000 + elapsed
            assert recovery() == initial
        for window in range(1, 6):
            now[0] = 1000 + hold + window * SAMPLES / 48000 + .001
            current = recovery()
            assert current.segments[0].sequence == window
            assert current.segments[-1].sequence == (5 if window < 3 else window + 5)
        assert [(cue.source, cue.sequence) for cue in current.cues] == [
            ('EXT-X-CUE-OUT', 6), ('EXT-X-CUE-IN', 8)]
        assert read('/ads/source070.ts') == b'controlled late source'
        assert json.loads(read('/stats'))['beacons'] == {}
        read('/reset')
        assert recovery() == initial
        now[0] += hold - 1
        assert recovery() == initial
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_late_break_uses_normal_media_time_without_recovery_hold(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(ad_fixture_server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    server = ad_fixture_server.make_server(tmp_path, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = 'http://127.0.0.1:%d' % server.server_port
    def read(scenario):
        with urlopen(origin + '/source/' + scenario + '/index.m3u8', timeout=3) as response:
            return parse_playlist(response.read().decode(), URL)
    try:
        assert read('late-break').segments[0].sequence == 0
        assert read('extended').segments[0].sequence == 0
        now[0] += 67 * SAMPLES / 48000 + .001
        late = read('late-break')
        assert late.segments[0].sequence == 67
        assert [(cue.source, cue.sequence) for cue in late.cues] == [
            ('EXT-X-CUE-OUT', 70), ('EXT-X-CUE-IN', 72)]
        ordinary = read('extended')
        assert ordinary.segments[0].sequence == 67
        assert ordinary.cues == ()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_positive_recovery_drains_audio_before_default_client_read_timeout():
    # Complete cue-out/cue-in first appear at window 3. Initial output contains
    # two preroll and six programme segments. Drain that audio but publish the
    # next bytes before Kodi's default 20 s Curl low-speed threshold. The previous
    # hold 20 fixture released at 26 s and tested timeout, not successful recovery.
    first_recovery = ad_fixture_server.RECOVERY_HOLD_SECONDS + 3 * SAMPLES / 48000
    assert 8 * SAMPLES / 48000 < first_recovery < 20


def test_recovery_serves_exact_replacement_and_following_audio_after_stalled_reads(
        monkeypatch, tmp_path, ad_assets):
    import relay
    now = [1000.0]
    monkeypatch.setattr(ad_fixture_server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(relay, '_AD_RELOAD_INTERVAL', .01)
    server = ad_fixture_server.make_server(ad_assets, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = 'http://127.0.0.1:%d' % server.server_port
    manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                             cache_root=tmp_path, resource_limit=12)
    manager.ad_config = AdConfig(True, True, True, origin + '/vast/direct', origin)
    output = RelayServer(manager, client_timeout=1)
    output.start()
    client = None

    def expected(names):
        return b''.join(extract_adts((ad_assets / (name + '.ts')).read_bytes())[1]
                        for name in names)

    def wait_until(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        assert predicate()

    def stats():
        with urlopen(origin + '/stats', timeout=2) as response:
            return json.load(response)

    try:
        session = manager.create(Station('fixture-recovery', 'Recovery',
            origin + '/source/recovery/index.m3u8', 'AAC', True))
        assert session.ad._midroll_done.wait(3)
        url = urlsplit(session.continuous_playback_url)
        client = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        client.request('GET', url.path)
        response = client.getresponse()
        assert response.status == 200
        initial = expected(['replacement000', 'replacement001'] +
                           ['source%03d' % number for number in range(6)])
        assert response.read(len(initial)) == initial
        wait_until(lambda: session.continuous_audio_state().next_sample == 8 * SAMPLES)
        before_requests = stats()['windows']['recovery']
        now[0] += ad_fixture_server.RECOVERY_HOLD_SECONDS + 2 * SAMPLES / 48000 + .001
        wait_until(lambda: stats()['windows']['recovery'] >= before_requests + 2)
        assert session.continuous_audio_state().next_sample == 8 * SAMPLES
        assert not session.continuous_audio_state().terminal
        # The next window exposes the complete break; consume through the
        # following programme segment, not merely a generated manifest.
        now[0] += SAMPLES / 48000
        recovered = expected(['replacement000', 'replacement001', 'source008'])
        assert response.read(len(recovered)) == recovered
        wait_until(lambda: session.continuous_audio_state().next_sample == 11 * SAMPLES)
        assert manager.get(session.id) is session
        assert not session.continuous_audio_state().terminal
        observed = stats()
        assert observed['requests']['/ads/source008.ts'] == 1
        assert observed['beacons'] == {}
    finally:
        output.close()
        if client is not None:
            client.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
