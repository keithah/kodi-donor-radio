#!/usr/bin/env python3
"""Local development evidence only: arm capture, then sample isolated Kodi."""
import argparse
import ctypes
import json
import math
from pathlib import Path
import sys
import time
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[3]
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

def expected_player(target_file, port, expected_type="audio"):
    players=rpc('Player.GetActivePlayers',port=port)
    if not players:return None
    if len(players)!=1 or type(players[0].get('playerid')) is not int or players[0].get('type')!=expected_type:
        raise RuntimeError('Expected one player of the explicitly selected type')
    player=players[0]
    item=rpc('Player.GetItem',{'playerid':player['playerid'],'properties':['file','dynpath']},port=port).get('item',{})
    if item.get('dynpath')!=target_file:raise RuntimeError('Playing item mismatch')
    return dict(player,file=item.get('file'),dynpath=item['dynpath']) # ID/type do not attest the core name

def save_observation(capture, report, port):
    # Preserve the completed timeline even if Kodi or the fixture has exited.
    output = capture / 'kodi-clock-observation.json'
    output.write_text(json.dumps(report, indent=2)+'\n')
    try:
        active = expected_player(report.get('target_file','http://127.0.0.1:18789/live/index.m3u8'),port,
            'video' if report.get('player_mode')=='videoplayer_video_v1' else 'audio')
        report['observer_forced_stop']=bool(active)
        if active:
            rpc('Player.Stop', {'playerid':active['playerid']}, port=port)
    except (OSError, RuntimeError) as error:
        report['stop_error'] = type(error).__name__
    try:
        with urlopen('http://127.0.0.1:18789/stats', timeout=3) as response:
            report['fixture'] = json.load(response)
    except (OSError, ValueError) as error:
        report['fixture'] = {'error': type(error).__name__}
    output.write_text(json.dumps(report, indent=2)+'\n')

def poll_interval(value):
    try:
        interval = float(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError('poll interval must be a finite number from 0.1 to 5 seconds') from exc
    if not math.isfinite(interval) or not .1 <= interval <= 5:
        raise argparse.ArgumentTypeError('poll interval must be a finite number from 0.1 to 5 seconds')
    return interval

def observe(report, args, target_file):
    start = time.monotonic()
    pauses = [(8, 1), (45, 3), (90, 20), (160, 3)]
    pause_index = 0
    report['pause_schedule'] = pauses
    pending = [(pauses[0][0], 'pause')] if args.action == 'pause' else [(9, 'seek')] if args.action == 'seek' else []
    next_observation = start
    while time.monotonic() - start < args.seconds:
        if pending and time.monotonic() - start >= pending[0][0]:
            _, action = pending[0]
            event = {'action':action, 'request_host_ns':host_ns()}
            gate_ready = True
            try:
                actual = expected_player(target_file,args.port,
                    "video" if getattr(args,"player_mode",None)=="videoplayer_video_v1" else "audio")
                if not actual and action == 'pause':
                    gate_ready = False
                elif not actual:
                    raise RuntimeError('No active player for action')
                else:
                    playerid = actual['playerid']
                    event['actual_player']=actual
                    event['before'] = rpc('Player.GetProperties', {'playerid':playerid, 'properties':['time','totaltime','speed']}, port=args.port)
                    if action in ('pause','resume'):
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
                        pending.append((pauses[pause_index][0], 'pause'))
                report['actions'].append(event)
        if time.monotonic() >= next_observation:
            next_observation = time.monotonic() + args.poll_interval
            try:
                active = expected_player(target_file,args.port,
                    "video" if getattr(args,"player_mode",None)=="videoplayer_video_v1" else "audio")
                t0 = host_ns()
                if active:
                    props = rpc('Player.GetProperties', {'playerid':active['playerid'],
                        'properties':['time','totaltime','speed','live','canseek']}, port=args.port)
                    t1 = host_ns()
                    item = {'file':target_file}
                    sample = {'item_identity_unverified':False,'actual_player':active,'playing':True,'player_time_seconds':_time(props['time']),
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

GAIN_CONTROLS = [dict(at=10,volume=90),dict(at=11,volume=75),dict(at=12,muted=True),
    dict(at=13,muted=False),dict(at=14,volume=0),dict(at=15,volume=100),
    dict(at=16,volume=90),dict(at=16.05,volume=75),dict(at=16.1,volume=100),
    dict(at=18,muted=True),dict(at=18.05,muted=False),dict(at=18.1,muted=True),dict(at=19,muted=False)]


def gain_controls_trial(report, args, target_file, open_action, capture):
    if (args.output_backend!='hal_callback_v1' or args.hal_phase!='controls' or args.action!='none'
            or args.port!=19092 or args.player_mode!='videoplayer_video_v1' or args.seconds!=50):
        raise ValueError('gain_trial_requires_isolated_hal_controls_50seconds')
    def persist():
        (capture/'kodi-clock-observation.json').write_text(json.dumps(report,indent=2)+'\n')
    def application(method, params):
        event=dict(method=method,params=params,request_host_ns=host_ns())
        try:
            event['result']=rpc(method,params,port=args.port)
            return event['result']
        except (OSError,RuntimeError) as exc:
            event['error']=type(exc).__name__
            raise
        finally:
            event['response_host_ns']=host_ns()
            report.setdefault('gain_rpc',[]).append(event)
            persist()
    original=application('Application.GetProperties',{'properties':['volume','muted']})
    if (not isinstance(original,dict) or type(original.get('volume')) is not int or not 0<=original['volume']<=100
            or type(original.get('muted')) is not bool):raise ValueError('gain_original_state_invalid')
    report['original_gain']=original
    report['gain_schedule']=GAIN_CONTROLS
    try:
        application('Application.SetVolume',{'volume':100})
        application('Application.SetMute',{'mute':False})
        open_action()
        persist() # open bracket must be visible while the trial is running
        start=time.monotonic(); pending=list(GAIN_CONTROLS)+[dict(at=40,stop=True)]
        while time.monotonic()-start < args.seconds:
            elapsed=time.monotonic()-start
            if pending and elapsed>=pending[0]['at']:
                due=pending.pop(0) # never retry a dispatch of uncertain outcome
                actual=expected_player(target_file,args.port,'video')
                if actual is None:raise RuntimeError('No active player for gain action')
                event=dict(declared=due,actual_player=actual,request_host_ns=host_ns())
                report['actions'].append(event)
                try:
                    if 'stop' in due:
                        event['result']=rpc('Player.Stop',{'playerid':actual['playerid']},port=args.port)
                    else:
                        method='Application.SetMute' if 'muted' in due else 'Application.SetVolume'
                        params={'mute':due['muted']} if 'muted' in due else {'volume':due['volume']}
                        event['result']=application(method,params)
                finally:
                    event['response_host_ns']=host_ns();persist()
                continue
            delay=min(.1,args.seconds-elapsed)
            if pending:delay=min(delay,max(0,pending[0]['at']-elapsed))
            if delay>0:time.sleep(delay)
    except (OSError,RuntimeError,ValueError) as exc:
        report['gain_trial_error']=type(exc).__name__
        raise
    finally:
        report['gain_restore']=dict(restored=False)
        failures=[]
        for method,params in (('Application.SetVolume',{'volume':original['volume']}),
                              ('Application.SetMute',{'mute':original['muted']})):
            try:application(method,params)
            except (OSError,RuntimeError) as exc:failures.append(type(exc).__name__)
        report['gain_restore']=dict(restored=not failures,errors=failures)
        persist()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--source', default='extended')
    p.add_argument('--owned-fixture',type=Path)
    p.add_argument('--player-mode',choices=['paplayer_pcm_v1','videoplayer_pcm_v1','videoplayer_video_v1'],default='paplayer_pcm_v1')
    p.add_argument('--output-backend',choices=['ae','hal_callback_v1'],default='ae')
    p.add_argument('--hal-phase',choices=['controls','clock'])
    p.add_argument('--gain-controls-trial',action='store_true')
    p.add_argument('--port', type=int, default=19093)
    p.add_argument('--pause-at', type=float, default=3)
    p.add_argument('--pause-duration', type=float, default=3)
    p.add_argument('--pause-clock-at', type=float, default=2.5)
    p.add_argument('--seconds', type=float, default=22)
    p.add_argument('--poll-interval', type=poll_interval, default=.1)
    p.add_argument('--action', choices=['none','pause','seek'], default='none')
    p.add_argument('--ready-timeout', type=float, default=180)
    args = p.parse_args()
    if (args.output_backend=='hal_callback_v1' and (args.hal_phase not in ('controls','clock') or args.player_mode!='videoplayer_video_v1' or not args.owned_fixture)) or (args.output_backend=='ae' and args.hal_phase):p.error('HAL requires explicit video fixture controls phase')
    if args.gain_controls_trial and (args.output_backend!='hal_callback_v1' or args.hal_phase!='controls' or args.action!='none' or args.port!=19092 or args.seconds!=50):p.error('gain trial requires isolated HAL controls, action none and 50 seconds')
    if args.player_mode!='paplayer_pcm_v1' and not args.owned_fixture:p.error('VideoPlayer mode requires --owned-fixture')
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
              'poll_interval_s':args.poll_interval, 'mapping_proven':False, 'target_file':target_file,'player_mode':args.player_mode, 'timeline':[], 'actions':[], 'requested_action':args.action,'output_backend':args.output_backend,'hal_phase':args.hal_phase}
    report['pause_gate'] = {'kind':'wall time after open response, independent of tested player clock'}
    from urllib.parse import urlencode
    query={'owned_beacons':'1','fixture':str(args.owned_fixture),'player_mode':args.player_mode} if args.owned_fixture else {'diagnostic_owned_hls':'1'}
    if args.output_backend=='hal_callback_v1':query.update(output_backend=args.output_backend,hal_phase=args.hal_phase)
    def open_action():
        report['open_request_host_ns']=host_ns()
        rpc('Files.GetDirectory', {'directory':'plugin://plugin.audio.kodi_donor_radio/?'+urlencode(query),'media':'music'},port=args.port)
        report['open_response_host_ns']=host_ns()
        (capture/'kodi-clock-observation.json').write_text(json.dumps(report,indent=2)+'\n')
    try:
        if args.gain_controls_trial:gain_controls_trial(report,args,target_file,open_action,capture)
        else:open_action();observe(report,args,target_file)
    finally:save_observation(capture,report,args.port)
    print(json.dumps({'capture':str(capture), 'samples':len(report['timeline']),
                      'beacons':report['fixture'].get('beacons', {})}))

if __name__ == '__main__':
    main()
