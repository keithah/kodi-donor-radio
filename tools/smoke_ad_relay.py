#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independently decode the controlled ad relay's published TS timeline."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'plugin.audio.kodi_donor_radio/resources/lib'))

from ad_models import AdConfig
from http_client import HttpClient
from models import Station
from relay import RelayServer
from sessions import SessionManager
from url_policy import UrlPolicy
from tools.generate_ad_audio import decoded_tone, probe

CHOICES = (440, 550, 660, 720, 880, 990)
TONE_SECTION = {440: 'programme_a', 550: 'programme_b', 660: 'original_break',
                720: 'source_mutation', 880: 'replacement', 990: 'preroll'}
SUCCESS_EVENTS = {'impression', 'start', 'firstQuartile', 'midpoint',
                  'thirdQuartile', 'complete'}


def check_trace(trace):
    """Return evidence failures, including ordering and event safety failures."""
    failures = []
    actual = trace['sections']; expected = trace['expected_sections']
    if len(actual) != len(expected):
        failures.append('segment count: observed %d, expected %d' % (len(actual), len(expected)))
    for index, (a, e) in enumerate(zip(actual, expected)):
        if a != e:
            failures.append('section %d: observed %s, expected %s' % (index, a, e))
    if trace['samples'] != trace['expected_samples']:
        failures.append('decoded sample boundaries differ')
    seen = set()
    for event in trace['events']:
        key = (event['placement_id'], event['event'])
        if key in seen:
            failures.append('duplicate playback event: %s/%s' % key)
        seen.add(key)
        if event['event'] in SUCCESS_EVENTS:
            # This tool only fetches and decodes media. A trace-supplied flag
            # cannot attest that Kodi presented any of it to the listener.
            failures.append('no playback evidence for success event: %s' % event['event'])
        stopped_at = trace['stopped_at']
        if stopped_at is not None and event['event'] == 'complete' and event['at'] > stopped_at:
            failures.append('completion after stop')
    return failures


def _json(url):
    with urlopen(url, timeout=15) as response:
        return json.load(response)


def _bytes(url):
    with urlopen(url, timeout=15) as response:
        return response.read()


def _routes(manifest):
    return [line for line in manifest.splitlines() if line and not line.startswith('#')]


def observe(origin, scenario, vast, preroll, midroll):
    _bytes(origin + '/reset')
    config = AdConfig(True, preroll, midroll, origin + '/vast/' + vast, origin)
    with tempfile.TemporaryDirectory(prefix='ad-relay-smoke-') as temp:
        manager = SessionManager(HttpClient(UrlPolicy(test_origins={origin})),
                                 ad_config=config, cache_root=Path(temp))
        relay = RelayServer(manager)
        session = None
        try:
            relay.start()
            session = manager.create(Station('fixture-smoke', 'Controlled',
                origin + '/source/' + scenario + '/index.m3u8', 'AAC', True))
            if midroll and session.ad is not None:
                session.ad._midroll_done.wait(5)
            deadline = time.monotonic() + 8
            manifest = ''
            # A request cannot advance the fixture's media clock. The first
            # player-visible window contains source000 through source005.
            expected_count = 8 if preroll else 6
            while time.monotonic() < deadline:
                manifest = _bytes(session.playback_url).decode()
                if len(_routes(manifest)) >= expected_count:
                    break
                time.sleep(.2)
            routes = _routes(manifest)
            base = 'http://127.0.0.1:%d' % relay.descriptor.port
            sections, samples, first_pts = [], [], []
            for index, route in enumerate(routes):
                body = _bytes(base + route)
                path = Path(temp) / ('observed-%03d.ts' % index)
                path.write_bytes(body)
                info = probe(path, 'ffprobe')
                tone = decoded_tone(path, 'ffmpeg', CHOICES)
                sections.append(TONE_SECTION[tone])
                samples.append(info['samples'])
                first_pts.append(info['first_pts'])
            available = vast not in ('no-fill', 'unsupported-media', 'failed-media')
            # VAST's duration declaration is advisory. The validated AAC
            # frame count is authoritative, including wrong-duration VAST.
            ad_section = 'preroll' if vast == 'preroll' else 'replacement'
            normal = ['programme_a'] * 2 + ([ad_section] if midroll and available else ['original_break']) * 2 + ['programme_b'] * 2
            expected = ([ad_section] * 2 if preroll and available else []) + normal
            stats = _json(origin + '/stats')
            events = [{'placement_id': key.split('/')[0], 'event': key.split('/')[1], 'at': 0.0}
                      for key, count in stats['beacons'].items() for _ in range(count)]
            trace = {'scenario': scenario, 'vast': vast, 'sections': sections,
                     'expected_sections': expected, 'samples': samples,
                     'expected_samples': [96256] * len(expected), 'first_pts': first_pts,
                     'events': events, 'stopped_at': None,
                     'manifest_discontinuities': manifest.count('#EXT-X-DISCONTINUITY\n'),
                     'requests': stats['requests'], 'beacons': stats['beacons']}
            trace['failures'] = check_trace(trace)
            return trace
        finally:
            try:
                if session is not None:
                    manager.stop(session.id)
            finally:
                relay.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:8766')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    results = [observe(args.origin, *case) for case in (
        ('normal', 'direct', True, True),
        ('normal', 'preroll', True, False),
        ('normal', 'no-fill', False, True),
        ('normal', 'failed-media', False, True),
        ('normal', 'wrong-duration', False, True),
    )]
    report = {'kind': 'relay_decode', 'results': results}
    serialized = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized)
    print(serialized)
    return 1 if any(item['failures'] for item in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
