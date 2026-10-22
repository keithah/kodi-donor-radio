# SPDX-License-Identifier: GPL-2.0-or-later
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

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
