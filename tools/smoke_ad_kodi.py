#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Drive unpatched Kodi over JSON-RPC and record observable playback clocks.

This does not infer audible output or install an ad-time mapping. Reports omit
opaque relay URLs, fixture origins, and addon secrets.
"""
import argparse
import codecs
import json
import math
from pathlib import Path
import platform
import re
import socket
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def rpc(method, params=None, port=9090, timeout=12):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        request['params'] = params
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(request) + '\n').encode())
        decoder = codecs.getincrementaldecoder('utf-8')()
        messages = json.JSONDecoder()
        buffer = ''
        while True:
            part = sock.recv(65536)
            if not part:
                raise OSError('Kodi RPC closed before response')
            buffer += decoder.decode(part)
            while buffer.strip():
                buffer = buffer.lstrip()
                try:
                    response, end = messages.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                buffer = buffer[end:]
                if response.get('id') != request['id']:
                    continue  # Kodi can send asynchronous player notifications.
                if 'error' in response:
                    raise RuntimeError('%s: %s' % (method, response['error'].get('message', 'error')))
                if 'result' not in response:
                    raise OSError('Kodi RPC response omitted result')
                return response['result']


def _time(value):
    if not isinstance(value, dict):
        return None
    return (value.get('hours', 0) * 3600 + value.get('minutes', 0) * 60 +
            value.get('seconds', 0) + value.get('milliseconds', 0) / 1000)


def _seek_effect_observed(evidence):
    """Require an observed jump to the requested target, beyond natural progress."""
    if not isinstance(evidence, dict):
        return False
    before = evidence.get('before_position')
    after = evidence.get('after_position')
    total = evidence.get('total_seconds')
    percentage = evidence.get('target_percentage')
    max_abs_speed = evidence.get('max_abs_speed')
    elapsed = evidence.get('elapsed_seconds')
    values = (before, after, total, percentage, max_abs_speed, elapsed)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or
           not math.isfinite(value) for value in values):
        return False
    if total <= 0 or max_abs_speed < 0 or not 0 <= percentage <= 100:
        return False
    target = total * percentage / 100
    # Require arrival at target and movement beyond the fastest observed normal
    # playback during the seek poll window. Kodi can be in fast-forward before
    # a test action, so elapsed wall time alone is not a sufficient baseline.
    natural_advance = elapsed * max(1.0, max_abs_speed)
    return abs(after - target) <= 1 and after - before > natural_advance + .5


_RELAY_MEDIA_PATH = re.compile(r'/media/[0-9a-f]{48}/[0-9a-f]{48}(?:\.[a-z0-9]+)?')


def _item_matches_expected(item_file, expected_file, previous_file=None):
    if item_file == expected_file:
        return item_file != previous_file
    parts = urlsplit(item_file or '')
    try:
        port = parts.port
    except ValueError:
        return False
    return (item_file != previous_file and parts.scheme == 'http' and parts.hostname == '127.0.0.1'
            and parts.username is None and port is not None
            and _RELAY_MEDIA_PATH.fullmatch(parts.path) is not None)


def sample(port, expected_file, previous_file=None):
    active = rpc('Player.GetActivePlayers', port=port)
    if not active:
        return {'playing': False}
    playerid = active[0]['playerid']
    props = rpc('Player.GetProperties', {'playerid': playerid,
        'properties': ['time', 'totaltime', 'speed', 'cachepercentage', 'live', 'canseek']}, port=port)
    item = rpc('Player.GetItem', {'playerid': playerid, 'properties': ['file']}, port=port)['item']
    conditions = rpc('XBMC.GetInfoBooleans',
                     {'booleans': ['Player.Caching', 'Player.Paused']}, port=port)
    return {'playing': True, 'playerid': playerid, 'time_seconds': _time(props.get('time')),
            'item_matches_expected': _item_matches_expected(item.get('file'), expected_file,
                                                             previous_file),
            'total_seconds': _time(props.get('totaltime')), 'speed': props.get('speed'),
            'cachepercentage': props.get('cachepercentage'), 'live': props.get('live'),
            'canseek': props.get('canseek'), 'player_caching': conditions.get('Player.Caching'),
            'player_paused': conditions.get('Player.Paused')}


def _stats(origin):
    with urlopen(origin + '/stats', timeout=3) as response:
        result = json.load(response)
    # The fixture records only paths. Never include loopback host/port or
    # opaque relay resource URLs in the report.
    return result


_SOURCE_FIXTURE_IDS = {
    'normal': 'fixture-30045',
    'mutation': 'fixture-30046',
    'expired-window': 'fixture-30047',
    'stall': 'fixture-30048',
    'finite': 'fixture-30049',
    'extended': 'fixture-30172',
    'late-break': 'fixture-30174',
    'recovery': 'fixture-30175',
}
_SOURCE_FIXTURE_PATHS = {
    'normal': '/source/normal/index.m3u8',
    'mutation': '/source/mutation/index.m3u8',
    'expired-window': '/source/expired-window/index.m3u8',
    'stall': '/source/stall/index.m3u8',
    'finite': '/source/finite/index.m3u8',
    'extended': '/source/extended/index.m3u8',
    'late-break': '/source/late-break/index.m3u8',
    'recovery': '/source/recovery/index.m3u8',
}


def _menu_file(port, source):
    try:
        expected_id = _SOURCE_FIXTURE_IDS[source]
    except KeyError as error:
        raise ValueError('Unknown controlled source: ' + str(source)) from error
    listing = rpc('Files.GetDirectory', {'directory':
        'plugin://plugin.audio.kodi_donor_radio/?mode=tests', 'media': 'music'}, port=port)
    for item in listing.get('files', []):
        query = parse_qs(urlsplit(item.get('file', '')).query)
        if query.get('mode') == ['play_fixture'] and query.get('id') == [expected_id]:
            return item['file'], item.get('label')
    raise RuntimeError('Controlled source not listed in Kodi test menu: ' + source)


def validate_observation(result):
    """Require the requested Kodi item and locally advancing player time."""
    active = [sample for sample in result['timeline'] if sample.get('playing')]
    failures = []
    requested_actions = result.get('requested_actions')
    if (isinstance(requested_actions, list) and
            [action.get('action') for action in result.get('actions', [])] != requested_actions):
        failures.append('Kodi did not attempt every requested action in order')
    for action in result.get('actions', []):
        if action.get('accepted') is False:
            failures.append('Kodi rejected requested %s action' % action.get('action', 'unknown'))
        if action.get('state_observed') is False:
            failures.append('Kodi accepted %s without the expected playback effect' %
                            action.get('action', 'unknown'))
        if (action.get('action') == 'seek' and action.get('accepted') is True and
                not _seek_effect_observed(action.get('seek_evidence'))):
            failures.append('Kodi accepted seek without the expected playback effect')
    actions = result.get('actions', [])
    for index, action in enumerate(actions):
        if action.get('action') != 'pause' or action.get('accepted') is not True:
            continue
        try:
            pause_at = action['at']
            resume = next(candidate for candidate in actions[index + 1:]
                          if candidate.get('action') == 'resume')
            resume_at = resume['at']
        except (KeyError, StopIteration, TypeError):
            failures.append('Kodi did not observe a complete pause/resume interval')
            continue
        interval = [sample for sample in result['timeline']
                    if sample.get('playing') is True and
                    sample.get('item_matches_expected') is True and
                    isinstance(sample.get('at'), (int, float)) and
                    pause_at < sample['at'] < resume_at]
        positions = [sample.get('time_seconds') for sample in interval
                     if isinstance(sample.get('time_seconds'), (int, float)) and
                     not isinstance(sample.get('time_seconds'), bool) and
                     math.isfinite(sample['time_seconds']) and sample['time_seconds'] >= 0]
        if (len(positions) < 2 or any(sample.get('speed') != 0 for sample in interval) or
                max(positions, default=0) - min(positions, default=0) > .5):
            failures.append('Kodi player clock advanced while paused')
    first_expected = next((index for index, sample in enumerate(active)
                           if sample.get('item_matches_expected') is True), None)
    if first_expected is None or any(
            sample.get('item_matches_expected') is not True for sample in active[first_expected:]):
        failures.append('active Kodi item did not consistently match requested fixture')
    fixture = result.get('fixture')
    expected_beacons = result.get('expected_beacons')
    if isinstance(expected_beacons, dict):
        actual_beacons = fixture.get('beacons') if isinstance(fixture, dict) else None
        if actual_beacons != expected_beacons:
            failures.append('fixture beacon counts did not match the scenario expectation')
    if isinstance(fixture, dict) and isinstance(fixture.get('requests'), dict):
        expected_path = _SOURCE_FIXTURE_PATHS.get(result.get('source'))
        if expected_path and fixture['requests'].get(expected_path, 0) < 1:
            failures.append('controlled fixture origin did not receive the expected fixture source request')
    matched = []
    for index, sample in enumerate(active):
        position = sample.get('time_seconds')
        if (sample.get('item_matches_expected') is not True
                or not isinstance(position, (int, float))
                or isinstance(position, bool) or not math.isfinite(position)
                or position < 0):
            # Kodi can expose a short negative millisecond component while the
            # player starts. It is not a valid media-clock anchor.
            continue
        observed_at = sample.get('at')
        if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)
                or not math.isfinite(observed_at)):
            observed_at = index * .25
        matched.append((observed_at, position))
    bounded_progress = 0
    last_progress = None
    for observed_at, position in matched:
        if last_progress is None:
            last_progress = (observed_at, position)
            continue
        prior_at, prior_position = last_progress
        elapsed = max(0, observed_at - prior_at)
        delta = position - prior_position
        if delta > 0:
            if delta <= elapsed + .5:
                bounded_progress += delta
            # Even an oversized jump becomes the next anchor, but is not
            # credited. Later smooth progress can still be observed.
            last_progress = (observed_at, position)
        elif delta < 0:
            # A backward clock change starts a new observation epoch. Equal
            # positions deliberately keep the old wall anchor so coarse Kodi
            # clock updates can accrue elapsed time across repeated polls.
            last_progress = (observed_at, position)
    stopped_as_requested = any(action.get('action') == 'stop' and action.get('accepted') is True
                               and action.get('state_observed') is True
                               for action in result.get('actions', []))
    if bounded_progress < .5 and not stopped_as_requested:
        failures.append('requested Kodi item did not show 0.5s of bounded clock advance')
    return failures


def observe(origin, port, source, duration, actions, expected_beacons=None):
    with urlopen(origin + '/reset', timeout=3):
        pass
    file, label = _menu_file(port, source)
    previous_file = None
    active = rpc('Player.GetActivePlayers', port=port)
    if active:
        previous_file = rpc('Player.GetItem', {'playerid': active[0]['playerid'],
            'properties': ['file']}, port=port)['item'].get('file')
    rpc('Player.Open', {'item': {'file': file}}, port=port)
    start = time.monotonic()
    timeline, action_results = [], []
    pending = list(actions)
    while time.monotonic() - start < duration:
        elapsed = time.monotonic() - start
        if pending and elapsed >= pending[0][0]:
            _, action = pending.pop(0)
            before = None
            seek_evidence = None
            seek_started_at = None
            seek_max_abs_speed = None
            try:
                active = rpc('Player.GetActivePlayers', port=port)
                playerid = active[0]['playerid'] if active else 0
                before = sample(port, file, previous_file)
                if action == 'pause' or action == 'resume':
                    result = rpc('Player.PlayPause', {'playerid': playerid}, port=port)
                elif action == 'seek':
                    seek_started_at = time.monotonic()
                    speed = before.get('speed') if before else None
                    if (isinstance(speed, (int, float)) and not isinstance(speed, bool)
                            and math.isfinite(speed)):
                        seek_max_abs_speed = abs(speed)
                    result = rpc('Player.Seek', {'playerid': playerid, 'value': {'percentage': 80}}, port=port)
                elif action == 'stop':
                    result = rpc('Player.Stop', {'playerid': playerid}, port=port)
                else:
                    raise ValueError(action)
                after = sample(port, file, previous_file)
                if action in ('pause', 'resume'):
                    expected_speed = 0 if action == 'pause' else 1
                    state_observed = (result.get('speed') == expected_speed and
                                      after.get('speed') == expected_speed)
                elif action == 'seek':
                    prior_position = before.get('time_seconds') if before else None
                    total = before.get('total_seconds') if before else None
                    seek_deadline = time.monotonic() + 2.5
                    state_observed = False
                    while before and before.get('canseek') is True:
                        current_position = after.get('time_seconds')
                        speed = after.get('speed')
                        if (isinstance(speed, (int, float)) and not isinstance(speed, bool)
                                and math.isfinite(speed)):
                            speed = abs(speed)
                            seek_max_abs_speed = (speed if seek_max_abs_speed is None else
                                                  max(seek_max_abs_speed, speed))
                        seek_evidence = {'before_position': prior_position,
                                         'after_position': current_position,
                                         'total_seconds': total,
                                         'target_percentage': 80,
                                         'max_abs_speed': seek_max_abs_speed,
                                         'elapsed_seconds': max(0, time.monotonic() - seek_started_at)}
                        if _seek_effect_observed(seek_evidence):
                            state_observed = True
                            break
                        if not after.get('playing') or time.monotonic() >= seek_deadline:
                            break
                        time.sleep(.2)
                        after = sample(port, file, previous_file)
                elif action == 'stop':
                    stop_deadline = time.monotonic() + 2
                    while (after.get('playing') is True and
                           after.get('item_matches_expected') is True and
                           time.monotonic() < stop_deadline):
                        time.sleep(.1)
                        after = sample(port, file, previous_file)
                    state_observed = (after.get('playing') is not True or
                                      after.get('item_matches_expected') is False)
                else:
                    state_observed = True
                action_result = {'at': round(elapsed, 3), 'action': action, 'accepted': True,
                                 'state_observed': bool(state_observed),
                                 'result': str(result)[:120]}
                if action == 'seek':
                    action_result['seek_evidence'] = seek_evidence
                action_results.append(action_result)
            except (OSError, RuntimeError) as error:
                action_results.append({'at': round(elapsed, 3), 'action': action,
                                       'accepted': False, 'state_observed': False,
                                       'error': type(error).__name__})
        try:
            observation = sample(port, file, previous_file)
        except (OSError, RuntimeError) as error:
            observation = {'rpc_error': type(error).__name__}
        observation['at'] = round(time.monotonic() - start, 3)
        timeline.append(observation)
        time.sleep(.25)
    try:
        active = rpc('Player.GetActivePlayers', port=port)
        if active:
            rpc('Player.Stop', {'playerid': active[0]['playerid']}, port=port)
    except (OSError, RuntimeError):
        pass
    time.sleep(.5)
    result = {'source': source, 'label': label, 'timeline': timeline,
            'actions': action_results, 'fixture': _stats(origin),
            'requested_actions': [action for _, action in actions],
            'audible_output_verified': False, 'mapping_proven': False}
    if expected_beacons is not None:
        result['expected_beacons'] = expected_beacons
    result['validation_failures'] = validate_observation(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:8766')
    parser.add_argument('--rpc-port', type=int, default=9090)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=17)
    args = parser.parse_args()
    scenarios = [
        # Moving live-window behavior is recorded, but not used as a beacon
        # count gate until its native-clock mapping is calibrated.
        ('normal', [], None),
        ('finite', [], {'placement-1/impression': 1, 'placement-1/start': 1,
                        'placement-1/firstQuartile': 1, 'placement-1/midpoint': 1,
                        'placement-1/thirdQuartile': 1, 'placement-1/complete': 1}),
        ('finite', [(3, 'pause'), (5, 'resume')],
         {'placement-1/impression': 1, 'placement-1/start': 1,
          'placement-1/firstQuartile': 1, 'placement-1/midpoint': 1,
          'placement-1/thirdQuartile': 1, 'placement-1/complete': 1}),
        # The seek may happen before or after VAST impression/start depending
        # on Kodi's HLS prefetch; record those beacon totals without gating.
        ('finite', [(9, 'seek')], None),
        ('normal', [(3, 'stop')], {}),
        ('expired-window', [], {}),
        ('mutation', [], None),
        ('stall', [], None),
    ]
    report = {'kind': 'kodi_player_observation', 'platform': platform.platform(),
              'jsonrpc_version': rpc('JSONRPC.Version', port=args.rpc_port),
              'scenarios': []}
    for source, actions, expected_beacons in scenarios:
        report['scenarios'].append(observe(args.origin, args.rpc_port, source,
                                           6 if actions == [(3, 'stop')] else
                                           max(args.duration, 18) if source == 'finite' else args.duration,
                                           actions, expected_beacons))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    for result in report['scenarios']:
        clocks = [s['time_seconds'] for s in result['timeline'] if s.get('time_seconds') is not None]
        print(result['source'], 'clock samples', len(clocks), 'range',
              (min(clocks), max(clocks)) if clocks else None, 'actions', result['actions'],
              'beacons', result['fixture']['beacons'], 'failures', result['validation_failures'])
    return 1 if any(result['validation_failures'] for result in report['scenarios']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
