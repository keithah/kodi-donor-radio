#!/usr/bin/env python3
"""Local development evidence only: arm capture, then sample isolated Kodi."""
import argparse
import ctypes
import json
from pathlib import Path
import sys
import time
from urllib.request import urlopen

REPO = Path('/Users/hermes/.codex/worktrees/radio-poc/kodi-donor-radio')
sys.path.insert(0, str(REPO))
from tools.smoke_ad_kodi import rpc, _menu_file, _time, _item_matches_expected

lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
lib.mach_absolute_time.restype = ctypes.c_uint64
class Timebase(ctypes.Structure):
    _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
timebase = Timebase()
assert lib.mach_timebase_info(ctypes.byref(timebase)) == 0

def host_ns():
    return lib.mach_absolute_time() * timebase.numer // timebase.denom

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--source', default='extended')
    p.add_argument('--port', type=int, default=19093)
    p.add_argument('--pause-at', type=float, default=3)
    p.add_argument('--pause-duration', type=float, default=3)
    p.add_argument('--pause-clock-at', type=float, default=2.5)
    p.add_argument('--seconds', type=float, default=22)
    p.add_argument('--action', choices=['none','pause','seek'], default='none')
    p.add_argument('--ready-timeout', type=float, default=180)
    args = p.parse_args()
    deadline = time.monotonic() + args.ready_timeout
    capture = None
    existing = set(args.output_root.glob("*/capture.json"))
    while time.monotonic() < deadline:
        for candidate in sorted(args.output_root.glob('*/capture.json'), reverse=True):
            if candidate in existing:
                continue
            try:
                state = json.loads(candidate.read_text())
            except (OSError, ValueError):
                continue
            if state.get('status') == 'recording':
                capture = candidate.parent
                break
        if capture:
            break
        time.sleep(.1)
    if not capture:
        raise SystemExit('Recorder did not become ready')
    target_file = 'http://127.0.0.1:18789/live/index.m3u8'
    report = {'kind':'captured_kodi_clock_observation', 'source':args.source,
              'clock':'mach_absolute_time', 'timebase':{'numer':timebase.numer,'denom':timebase.denom},
              'mapping_proven':False, 'timeline':[], 'actions':[], 'requested_action':args.action}
    report['pause_gate'] = {'kind':'wall time after open response, independent of tested player clock'}
    report['open_request_host_ns'] = host_ns()
    rpc('Files.GetDirectory', {'directory':'plugin://plugin.audio.kodi_donor_radio/?diagnostic_owned_hls=1','media':'music'},port=args.port)
    report['open_response_host_ns'] = host_ns()
    start = time.monotonic()
    pauses = [(8, 1), (45, 3), (90, 20), (160, 3)]
    pause_index = 0
    report['pause_schedule'] = pauses
    pending = [(0, 'pause')] if args.action == 'pause' else [(9, 'seek')] if args.action == 'seek' else []
    while time.monotonic() - start < args.seconds:
        if pending and time.monotonic() - start >= pending[0][0]:
            _, action = pending[0]
            event = {'action':action, 'request_host_ns':host_ns()}
            gate_ready = True
            try:
                players = rpc('Player.GetActivePlayers', port=args.port)
                if not players and action == 'pause':
                    gate_ready = False
                elif not players:
                    raise RuntimeError('No active player for action')
                else:
                    playerid = players[0]['playerid']
                    event['before'] = rpc('Player.GetProperties', {'playerid':playerid, 'properties':['time','totaltime','speed']}, port=args.port)
                    if action == 'pause' and time.monotonic() - start < pauses[pause_index][0]:
                        gate_ready = False
                    elif action in ('pause','resume'):
                        event['result'] = rpc('Input.ExecuteAction', {'action':'pause'}, port=args.port)
                    else:
                        event['result'] = rpc('Player.Seek', {'playerid':playerid, 'value':{'percentage':80}}, port=args.port)
            except (OSError,RuntimeError) as error:
                event['error'] = type(error).__name__
                # A failed status query did not dispatch a toggle. Keep the
                # scheduled action pending until the player becomes readable.
                # Do not retry an uncertain action dispatch: that could toggle twice.
                if 'before' not in event:
                    gate_ready = False
                    report.setdefault('status_query_retries', []).append(event)
            event['response_host_ns'] = host_ns()
            if gate_ready:
                pending.pop(0)
                if action == 'pause' and 'error' not in event:
                    pending.insert(0, (time.monotonic() - start + pauses[pause_index][1], 'resume'))
                event['cycle'] = pause_index
                if action == 'resume' and 'error' not in event:
                    pause_index += 1
                    if pause_index < len(pauses):
                        pending.append((0, 'pause'))
                report['actions'].append(event)
        try:
            active = rpc('Player.GetActivePlayers', port=args.port)
            t0 = host_ns()
            if active:
                props = rpc('Player.GetProperties', {'playerid':active[0]['playerid'],
                    'properties':['time','totaltime','speed','live','canseek']}, port=args.port)
                t1 = host_ns()
                item = {'file':target_file}
                sample = {'item_identity_unverified':True,'playing':True,'player_time_seconds':_time(props['time']),
                          'total_seconds':_time(props['totaltime']), 'speed':props['speed'],
                          'live':props['live'], 'canseek':props['canseek']}
            else:
                t1 = host_ns()
                sample = {'playing':False}
            sample.update({'request_host_ns':t0,'response_host_ns':t1})
        except (OSError,RuntimeError) as error:
            sample = {'error':type(error).__name__, 'response_host_ns':host_ns()}
        report['timeline'].append(sample)
        time.sleep(.1)
    active = rpc('Player.GetActivePlayers', port=args.port)
    if active:
        rpc('Player.Stop', {'playerid':active[0]['playerid']}, port=args.port)
    report['fixture'] = json.load(urlopen('http://127.0.0.1:18789/stats', timeout=3))
    (capture / 'kodi-clock-observation.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'capture':str(capture), 'samples':len(report['timeline']),
                      'beacons':report['fixture'].get('beacons', {})}))

if __name__ == '__main__':
    main()
