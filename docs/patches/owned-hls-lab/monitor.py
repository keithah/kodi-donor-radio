# Isolated loopback experiment only. Normal Kodi callback controls are preserved.
import importlib.util
import json
import os
from pathlib import Path
import queue
import threading
import time
import xbmc

def run_owned(directory,library):
    """One monitor claims a newly created session; restart/reconnect is forbidden."""
    from dataclasses import asdict
    import sys
    sys.path[:0]=[str(Path(__file__).resolve().parent),library]
    import fixture_beacons as fixture
    import runtime_clock
    from owned_clock import OwnedClockBinding,OwnedClockProvider,OwnedClockTransport,OwnedTransportError,SESSION_ROOT,mach_host_ns
    from owned_ad_playback import OwnedAdPlayback
    directory=Path(directory)
    if directory.parent!=SESSION_ROOT:raise ValueError('session_root_mismatch')
    transport=OwnedClockTransport(directory)
    # Exclusive claim is deliberately never removed: a stopped monitor cannot rejoin.
    claim=os.open('monitor-claimed',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=transport.fd)
    os.close(claim)
    launch=json.loads(transport._read_immutable('launch.json',128*1024)[0])
    timeline=transport._read_immutable('timeline.json',128*1024)[0]
    binding=OwnedClockBinding(**launch['binding'])
    if (binding.nonce!=directory.name or binding.native_pid!=os.getpid() or binding.incarnation is not None
            or (directory/'hello.json').exists() or (directory/'native.jsonl').exists()
            or fixture.sha(timeline)!=binding.timeline_sha256 or launch['estimator_sha256']!=runtime_clock.SHA256):
        raise ValueError('monitor_fresh_launch_binding')
    fixture.validate_profile(launch['settings'])
    presentation=fixture.presentation_from_manifest(json.loads(timeline))
    module=runtime_clock.frozen_module()
    pending=queue.Queue(maxsize=80000);cancel=threading.Event()
    def enqueue(row):
        try:pending.put_nowait(row)
        except queue.Full:provider.retire('owned_output_queue_bound')
    class RecordingProvider(OwnedClockProvider):
        def ingest(self,raw):
            received=mach_host_ns()
            snapshot=super().ingest(raw)
            original=json.loads(raw)
            enqueue(dict(kind='owned_clock_input',row_sha256=fixture.sha(raw),
                         row_sequence=original.get('ownership',{}).get('observation_sequence'),
                         received_mach_ns=received,snapshot=asdict(snapshot)))
            return snapshot
    provider=RecordingProvider(binding,module.Estimator,now_ns=mach_host_ns)
    adapter=OwnedAdPlayback(binding,provider,presentation,fixture.http_client(),cancel,enqueue)
    callback_lock=threading.Lock();stopped=threading.Event();active=threading.Event()
    def control(value,callback):
        with callback_lock:
            issued=mach_host_ns()
            command=adapter.control(value,issued_ns=issued)
            enqueue(dict(kind='owned_callback',event=value,callback=callback,issued_mach_ns=issued,
                         revision=command.revision if command else None,generation=provider.generation))
            if value=='play':active.set()
            if value=='stop':stopped.set()
    class OwnedPlayer(xbmc.Player):
        def onAVStarted(self):
            try:adapter.route(self.getPlayingFile())
            except Exception:adapter.retire('route_unreadable')
            control('play','onAVStarted')
        def onPlayBackPaused(self):control('pause','onPlayBackPaused')
        def onPlayBackResumed(self):control('resume','onPlayBackResumed')
        def onPlayBackStopped(self):control('stop','onPlayBackStopped')
        def onPlayBackEnded(self):control('stop','onPlayBackEnded')
        def onPlayBackError(self):adapter.retire('playback_error');enqueue(dict(kind='owned_error',reason='playback_error',issued_mach_ns=mach_host_ns()))
        def onPlayBackSeek(self,*args):adapter.retire('seek');enqueue(dict(kind='owned_error',reason='seek',issued_mach_ns=mach_host_ns()))
        def onPlayBackSpeedChanged(self,speed):
            enqueue(dict(kind='owned_speed_callback',speed=speed,issued_mach_ns=mach_host_ns()))
            if speed not in (0,1):adapter.retire('non_unit_speed')
    player=OwnedPlayer();monitor=xbmc.Monitor();rows=0;bound=False
    descriptor=os.open('owned-events.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=transport.fd)
    output=os.fdopen(descriptor,'w')
    def flush():
        nonlocal rows
        while not pending.empty():
            row=pending.get_nowait();rows+=1
            if rows>80000:adapter.retire('owned_output_bound');raise RuntimeError('owned_output_bound')
            output.write(module.strict_json(row)+'\n')
        output.flush()
    enqueue(dict(kind='owned_launch',session_id=adapter.session_id,launch_mach_ns=binding.launch_mach_ns,
                 native_pid=binding.native_pid,player_mode=binding.player_mode,requested_core=binding.requested_core,video_queue_tenths=binding.video_queue_tenths,timeline_sha256=binding.timeline_sha256,
                 first_packet_sha256=binding.first_packet_sha256,selected_url=binding.selected_url,
                 estimator_sha256=runtime_clock.SHA256,proof_sha256=fixture.sha(fixture.canonical(launch['proof'])),
                 production_eligible=False,settings=launch['settings']))
    fixture.publish_ready(transport.fd,asdict(binding))
    flush();stop_seen=None
    try:
        for _ in range(30000):
            if monitor.waitForAbort(.02) or (directory/'monitor-stop').exists():break
            if active.is_set() and not stopped.is_set():
                try:adapter.route(player.getPlayingFile())
                except Exception:adapter.retire('route_unreadable')
            try:
                snapshots=transport.poll(provider)
                transport.flush_commands(provider)
                if provider.binding.incarnation is not None and not bound:
                    enqueue(dict(kind='owned_native_ready',session_id=adapter.session_id,
                                 incarnation_sha256=fixture.sha(provider.binding.incarnation.encode()),
                                 checked_mach_ns=mach_host_ns()));bound=True
                for snapshot in snapshots:
                    accepted=adapter.observe(snapshot)
                    enqueue(dict(kind='owned_clock_snapshot',observed=accepted,**asdict(snapshot)))
            except OwnedTransportError:
                adapter.retire('transport_invalid')
                enqueue(dict(kind='owned_error',reason='transport_invalid',issued_mach_ns=mach_host_ns()))
            flush()
            if stopped.is_set():
                if stop_seen is None:stop_seen=mach_host_ns()
                if mach_host_ns()-stop_seen>500_000_000:break
            if not active.is_set() and mach_host_ns()-binding.launch_mach_ns>30_000_000_000:
                adapter.retire('startup_timeout');break
    finally:
        adapter.close('monitor_exit')
        enqueue(dict(kind='owned_monitor_exit',retired=provider.retired,reason=provider.reason,
                     checked_mach_ns=mach_host_ns()))
        try:flush()
        finally:output.close();transport.close()


import sys
if len(sys.argv)>1 and sys.argv[1]=='--owned-beacons':
    if len(sys.argv)!=4:raise ValueError('owned_monitor_arguments')
    run_owned(sys.argv[2],sys.argv[3])
    raise SystemExit

ROOT = Path('/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer')
# Kodi executes this script without guaranteeing its directory is on sys.path.
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('owned_hls_runtime_clock', str(HERE/'runtime_clock.py'))
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)
launch_ns = clock.mach_host_ns()
session = clock.RuntimeClockSession(clock.URL, launch_ns)
reader = clock.LedgerReader(ROOT/'hls-clock-ledger.jsonl')
write_lock = threading.RLock()
output_rows = 0
MAX_OUTPUT_ROWS = 80000

def emit(row):
    global output_rows
    with write_lock:
        if output_rows >= MAX_OUTPUT_ROWS:
            session.retire('runtime_output_bound')
            raise RuntimeError('bounded runtime ledger exhausted')
        if row.get('kind') in ('runtime_clock_input','runtime_clock_snapshot'):
            row = session.for_publication(row)
        with (ROOT/'runtime-clock.jsonl').open('a') as f:
            f.write(session.module.strict_json(row)+'\n')
        output_rows += 1

pending = queue.Queue(maxsize=MAX_OUTPUT_ROWS)
control_lock = threading.Lock()

def enqueue(row):
    try:
        pending.put_nowait(row)
    except queue.Full:
        session.retire('runtime_queue_bound')

def state(value):
    # No telemetry read, parse or output IO can delay callback revocation.
    # This independent lock preserves native control-file callback ordering.
    now = clock.mach_host_ns()
    with control_lock:
        session.control(value, now)
        tmp = ROOT/'control.tmp'
        tmp.write_text(value)
        os.replace(str(tmp), str(ROOT/'control'))
        enqueue(dict(kind='runtime_callback', event=value, monotonic_ns=now))
        enqueue(session.snapshot())

def flush_pending():
    while True:
        try:
            row = pending.get_nowait()
        except queue.Empty:
            break
        if row['kind'] == 'runtime_callback':
            with (ROOT/'callbacks.jsonl').open('a') as f:
                f.write(json.dumps({'event':row['event'],
                                    'monotonic_ns':row['monotonic_ns']})+'\n')
        else:
            emit(row)

class Player(xbmc.Player):
    def onAVStarted(self):
        session.route(self.getPlayingFile())
        state('play')
    def onPlayBackPaused(self): state('pause')
    def onPlayBackResumed(self): state('resume')
    def onPlayBackStopped(self): state('stop')
    def onPlayBackEnded(self): state('stop')
    def onPlayBackError(self): session.retire('playback_error')
    def onPlayBackSeek(self, *args): session.retire('seek')
    def onPlayBackSpeedChanged(self, speed):
        if speed not in (0, 1): session.retire('non_unit_speed')

(ROOT/'monitor-stop').unlink(missing_ok=True)
for name in ('callbacks.jsonl', 'runtime-clock.jsonl'):
    previous = ROOT/name
    if previous.exists():
        previous.rename(ROOT/('%s-previous-%d.jsonl'%(previous.stem,time.time_ns())))
(ROOT/'monitor-ready').unlink(missing_ok=True)
p = Player()
m = xbmc.Monitor()
state('waiting')
emit(dict(kind='runtime_clock_launch', launch_id=session.launch_id,
          launch_ns=launch_ns, selected_url=clock.URL, estimator_sha256=clock.SHA256,
          clock_domain='explicit mach_absolute_time times mach_timebase_info nanoseconds; legacy callback monotonic_ns uses this domain',
          production_eligible=False))
(ROOT/'monitor-ready').write_text(str(launch_ns))
last_snapshot = None
try:
    for _ in range(30000):
        if m.waitForAbort(.02) or (ROOT/'monitor-stop').exists(): break
        if session.control_state in ('play','resume','pause'):
            try:
                session.route(p.getPlayingFile())
            except Exception:
                session.retire('route_unreadable')
        try:
            rows = reader.read()
            for item in rows:
                row = item.pop('row')
                if row.get('kind') == 'clock_sample':
                    enqueue(session.ingest(row, identity=item))
        except clock.LedgerError as exc:
            session.retire('ledger_error:'+str(exc))
        snapshot = session.snapshot()
        key = (snapshot['valid'], tuple(snapshot['reasons']), snapshot['generation'])
        if key != last_snapshot:
            enqueue(snapshot)
            last_snapshot = key
        flush_pending()
finally:
    session.retire('monitor_exit')
    try:
        flush_pending()
        emit(session.snapshot())
    finally:
        reader.close()
        (ROOT/'monitor-ready').unlink(missing_ok=True)
