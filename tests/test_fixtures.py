# SPDX-License-Identifier: GPL-2.0-or-later
import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
from urllib.request import urlopen

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
    server = module.make_server(Path('unused'), port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = 'http://127.0.0.1:%s' % server.server_port
        with urlopen(base + '/icy/tildeats.mp3', timeout=3) as response:
            assert response.headers['icy-metaint'] == str(module.ICY_METAINT)
            assert response.read() == module.icy_stream_bytes('tildeats')
        with urlopen(base + '/expected/icy/tildeats.json', timeout=3) as response:
            assert json.loads(response.read()) == [
                {'kind': kind, 'classification': classification}
                for kind, classification in module.expected_icy('tildeats')]
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
