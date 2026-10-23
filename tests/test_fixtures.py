# SPDX-License-Identifier: GPL-2.0-or-later
import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
import pytest

from cues import CueTracker
from hls import parse_playlist


TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def test_fixture_schedules_match_expected_observations():
    spec = importlib.util.spec_from_file_location('fixture_server', TOOLS / 'fixture_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for scenario in module.SCENARIOS:
        events = []
        class Sink:
            def emit(self, event):
                events.append({'kind': event.kind, 'classification': event.details['classification']})
        tracker = CueTracker('session', 'station', Sink(), lambda: datetime.now(timezone.utc), lambda: 0)
        for version in range(3):
            playlist = parse_playlist(module.manifest(scenario, version), 'http://fixture.example/hls/%s/index.m3u8' % scenario)
            tracker.observe(playlist, 'rendition', 0)
        tracker.finish('fixture_complete')
        assert events == module.expected(scenario)


def test_fixture_tool_help_documents_scenarios_and_audio_generator():
    for filename in ('fixture_server.py', 'generate_audio.py'):
        result = subprocess.run([sys.executable, str(TOOLS / filename), '--help'], capture_output=True, text=True)
        assert result.returncode == 0
        assert 'assets' in result.stdout


def test_controlled_icy_scenarios_match_expected_events_and_preserve_audio():
    spec = importlib.util.spec_from_file_location('fixture_server', TOOLS / 'fixture_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from icy import IcyDecoder
    from icy_markers import IcyMarkerTracker

    for scenario in module.ICY_SCENARIOS:
        events = []
        tracker = IcyMarkerTracker('fixture-session', 'fixture-station', events.append,
                                   lambda marker: None)
        decoded_audio = []
        times = iter(module.icy_observation_times(scenario))

        def observe(metadata):
            tracker.observe(metadata, '2026-10-23T00:00:00Z', next(times))

        decoder = IcyDecoder(module.ICY_METAINT, observe)
        audio = module.icy_audio_bytes(scenario)
        framed = module.icy_stream_bytes(scenario, audio)
        for offset in range(0, len(framed), 137):
            decoded_audio.append(decoder.feed(framed[offset:offset + 137]))
        decoder.finish()
        if scenario == 'unresolved':
            tracker.expire(301)

        assert b''.join(decoded_audio) == audio
        assert [(event.kind, event.details.get('classification')) for event in events] == (
            module.expected_icy(scenario))
        assert all(event.protocol == 'icy' and event.source == 'icy_marker'
                   for event in events)
        assert not any('vast' in event.kind.lower() or 'replacement' in event.kind.lower()
                       or 'beacon' in event.kind.lower() for event in events)


def test_controlled_icy_http_routes_publish_paced_metadata_streams():
    spec = importlib.util.spec_from_file_location('fixture_server', TOOLS / 'fixture_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assets = Path(__file__).resolve().parent / 'generated-audio'
    tone_path = assets / 'tone.mp3'
    if not tone_path.is_file():
        pytest.skip('Generate controlled MP3 fixtures before testing ICY HTTP playback')
    tone = module.mp3_frame_payload(tone_path)
    assert tone[:1] == b'\xff' and tone[1] & 0xe0 == 0xe0
    expected_audio = module.icy_audio_bytes('tildeats', tone)
    expected_frames = module.icy_stream_frames('tildeats', expected_audio)
    server = module.make_server(assets, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = 'http://127.0.0.1:%s' % server.server_port
        with urlopen(base + '/icy/tildeats.mp3', timeout=5) as response:
            assert response.headers['icy-metaint'] == str(module.ICY_METAINT)
            frame_times = []
            received = []
            for expected_frame in expected_frames:
                remaining = len(expected_frame)
                frame = bytearray()
                while remaining:
                    block = response.read(remaining)
                    assert block
                    frame.extend(block)
                    remaining -= len(block)
                received.append(bytes(frame))
                frame_times.append(time.monotonic())
            assert b''.join(received) == module.icy_stream_bytes('tildeats', expected_audio)
            assert all(.12 <= later - earlier <= 1.5
                       for earlier, later in zip(frame_times, frame_times[1:]))
        with urlopen(base + '/expected/icy/tildeats.json', timeout=3) as response:
            assert json.loads(response.read()) == [
                {'kind': kind, 'classification': classification}
                for kind, classification in module.expected_icy('tildeats')]
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()


def test_controlled_icy_relay_preserves_generated_audio_and_emits_only_observations(tmp_path):
    spec = importlib.util.spec_from_file_location('fixture_server', TOOLS / 'fixture_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assets = Path(__file__).resolve().parent / 'generated-audio'
    tone_path = assets / 'tone.mp3'
    if not tone_path.is_file():
        pytest.skip('Generate controlled MP3 fixtures before testing ICY relay playback')
    tone = module.mp3_frame_payload(tone_path)
    assert tone[:1] == b'\xff' and tone[1] & 0xe0 == 0xe0
    scenario = 'paired-marker'
    expected_audio = module.icy_audio_bytes(scenario, tone)
    origin_server = module.make_server(assets, port=0)
    origin_worker = threading.Thread(target=origin_server.serve_forever, daemon=True)
    origin_worker.start()

    from ad_models import AdConfig
    from http_client import HttpClient
    from models import Station
    from relay import RelayServer
    from sessions import SessionManager
    from url_policy import UrlPolicy

    origin = 'http://127.0.0.1:%s' % origin_server.server_port
    events = []
    class Sink:
        def emit(self, event):
            events.append(event)

    manager = SessionManager(
        client=HttpClient(UrlPolicy(test_origins={origin}), read_timeout=5), sink=Sink(),
        ad_config=AdConfig(True, True, True, origin + '/vast/direct', origin),
        cache_root=tmp_path)
    relay = RelayServer(manager, client_timeout=5)
    try:
        relay.start()
        session = manager.create(Station(
            'fixture-icy-paired', 'ICY paired marker fixture',
            origin + '/icy/' + scenario + '.mp3', 'MP3', False))
        with urlopen(session.playback_url, timeout=10) as response:
            assert response.status == 200
            assert 'icy-metaint' not in response.headers
            decoded_audio = response.read()
        assert decoded_audio == expected_audio
        marker_events = [event for event in events if event.source == 'icy_marker']
        assert [(event.kind, event.details.get('classification')) for event in marker_events] == (
            module.expected_icy(scenario))
        assert all(event.kind == 'metadata_observed' or event.source == 'icy_marker'
                   for event in events)
        assert session.ad is None and not session.ad_enabled
        assert not any(token in event.kind.lower()
                       for event in events
                       for token in ('vast', 'replacement', 'beacon'))
    finally:
        relay.close()
        origin_server.stopping.set()
        origin_server.shutdown()
        origin_server.server_close()
