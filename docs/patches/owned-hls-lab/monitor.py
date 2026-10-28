# Isolated loopback experiment only. Normal Kodi callback controls are preserved.
import importlib.util
import json
import os
from pathlib import Path
import queue
import threading
import time
import xbmc

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
