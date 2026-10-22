#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Drive unpatched Kodi over JSON-RPC and record observable playback clocks.

This does not infer audible output or install an ad-time mapping. Reports omit
opaque relay URLs, fixture origins, and addon secrets.
"""
import argparse
import codecs
import json
from pathlib import Path
import platform
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


def sample(port):
    active = rpc('Player.GetActivePlayers', port=port)
    if not active:
        return {'playing': False}
    playerid = active[0]['playerid']
    props = rpc('Player.GetProperties', {'playerid': playerid,
        'properties': ['time', 'totaltime', 'speed', 'cachepercentage', 'live', 'canseek']}, port=port)
    conditions = rpc('XBMC.GetInfoBooleans',
                     {'booleans': ['Player.Caching', 'Player.Paused']}, port=port)
    return {'playing': True, 'playerid': playerid, 'time_seconds': _time(props.get('time')),
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


def _menu_file(port, source):
    listing = rpc('Files.GetDirectory', {'directory':
        'plugin://plugin.audio.kodi_donor_radio/?action=tests', 'media': 'music'}, port=port)
    for item in listing.get('files', []):
        station = parse_qs(urlsplit(item['file']).query).get('station', [])
        if station and json.loads(station[0]).get('url', '').endswith('/source/' + source + '/index.m3u8'):
            return item['file'], item.get('label')
    raise RuntimeError('Controlled source not listed in Kodi test menu: ' + source)


def observe(origin, port, source, duration, actions):
    with urlopen(origin + '/reset', timeout=3):
        pass
    file, label = _menu_file(port, source)
    rpc('Player.Open', {'item': {'file': file}}, port=port)
    start = time.monotonic()
    timeline, action_results = [], []
    pending = list(actions)
    while time.monotonic() - start < duration:
        elapsed = time.monotonic() - start
        if pending and elapsed >= pending[0][0]:
            _, action = pending.pop(0)
            try:
                active = rpc('Player.GetActivePlayers', port=port)
                playerid = active[0]['playerid'] if active else 0
                if action == 'pause' or action == 'resume':
                    result = rpc('Player.PlayPause', {'playerid': playerid}, port=port)
                elif action == 'seek':
                    result = rpc('Player.Seek', {'playerid': playerid, 'value': {'percentage': 80}}, port=port)
                elif action == 'stop':
                    result = rpc('Player.Stop', {'playerid': playerid}, port=port)
                else:
                    raise ValueError(action)
                action_results.append({'at': round(elapsed, 3), 'action': action, 'accepted': True,
                                       'result': str(result)[:120]})
            except (OSError, RuntimeError) as error:
                action_results.append({'at': round(elapsed, 3), 'action': action,
                                       'accepted': False, 'error': type(error).__name__})
        try:
            observation = sample(port)
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
    return {'source': source, 'label': label, 'timeline': timeline,
            'actions': action_results, 'fixture': _stats(origin),
            'audible_output_verified': False, 'mapping_proven': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:58416')
    parser.add_argument('--rpc-port', type=int, default=9090)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=17)
    args = parser.parse_args()
    scenarios = [
        ('normal', [(3, 'pause'), (5, 'resume'), (9, 'seek')]),
        ('normal', [(3, 'stop')]),
        ('expired-window', []),
        ('mutation', []),
        ('stall', []),
    ]
    report = {'kind': 'kodi_player_observation', 'platform': platform.platform(),
              'jsonrpc_version': rpc('JSONRPC.Version', port=args.rpc_port),
              'scenarios': []}
    for source, actions in scenarios:
        report['scenarios'].append(observe(args.origin, args.rpc_port, source,
                                           6 if actions == [(3, 'stop')] else args.duration,
                                           actions))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    for result in report['scenarios']:
        clocks = [s['time_seconds'] for s in result['timeline'] if s.get('time_seconds') is not None]
        print(result['source'], 'clock samples', len(clocks), 'range',
              (min(clocks), max(clocks)) if clocks else None, 'actions', result['actions'],
              'beacons', result['fixture']['beacons'])
    return 0 if all(any(s.get('time_seconds') is not None for s in result['timeline'])
                    for result in report['scenarios']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
