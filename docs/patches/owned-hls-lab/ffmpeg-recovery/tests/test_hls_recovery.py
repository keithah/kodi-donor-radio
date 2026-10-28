#!/usr/bin/env python3
"""Real HTTP/HLS regression: append exact next media only after public EOF.

The exhausted-hold check before availability makes recovery fail. Expectations
come from the same fixed media read uninterrupted, including every packet PTS,
payload digest and decoded PCM frame/hash. No reopen, seek or counter reset is
performed by this harness or its public-API reader.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / 'artifacts'
DURATIONS = ['1.002667', '1.002667', '1.002667', '1.002667', '0.021333']

class Fixture:
    def __init__(self, count, ended, http11=False):
        self.count, self.ended = count, ended
        self.requests = []
        self.lock = threading.Lock()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if http11 else "HTTP/1.0"
            def do_GET(self):
                with fixture.lock:
                    count, ended = fixture.count, fixture.ended
                    fixture.requests.append({'path': self.path, 'count': count, 'ended': ended})
                if self.path == '/stream.m3u8':
                    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:1', '#EXT-X-MEDIA-SEQUENCE:0']
                    for index in range(count): lines.extend([f'#EXTINF:{DURATIONS[index]},', f'segment-{index:02d}.ts'])
                    if ended: lines.append('#EXT-X-ENDLIST')
                    data = ('\n'.join(lines)+'\n').encode()
                    mime = 'application/vnd.apple.mpegurl'
                elif self.path in [f'/segment-{i:02d}.ts' for i in range(count)]:
                    data = (MEDIA / self.path[1:]).read_bytes()
                    mime = 'video/mp2t'
                else:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/stream.m3u8'
    def publish(self, count, ended):
        with self.lock: self.count, self.ended = count, ended
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

def run(probe, name, count, ended, expected, transitions=(), seconds=10, hold=3, persistent=1, http11=False):
    fixture = Fixture(count, ended, http11)
    rows, publications = [], []
    started = time.monotonic()
    last_publication_packets = -1
    transitions = list(transitions)
    (ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    stderr_path = ROOT/'logs'/f'{probe.name}-{name}.stderr'
    try:
        with stderr_path.open('w') as stderr:
            process = subprocess.Popen([str(probe), fixture.url, str(expected), str(seconds), str(hold), str(persistent)], stdout=subprocess.PIPE, stderr=stderr, text=True)
            # The probe's interrupt callback bounds every read; wall timeout is
            # an independent safety net, not a simulated EOF or recovery.
            timer = threading.Timer(seconds + 3, process.kill)
            timer.start()
            try:
                for line in process.stdout:
                    row = json.loads(line); rows.append(row)
                    if row['event'] == 'eof' and transitions and row['packets'] > last_publication_packets:
                        next_count, next_ended = transitions.pop(0)
                        fixture.publish(next_count, next_ended)
                        last_publication_packets = row['packets']
                        publications.append({'after_eof': row, 'elapsed_seconds': time.monotonic()-started, 'count': next_count, 'ended': next_ended})
                code = process.wait(timeout=2)
            finally:
                timer.cancel()
                if process.poll() is None: process.kill(); process.wait()
    finally:
        fixture.close()
    report = {'name': name, 'exit_code': code, 'seconds': time.monotonic()-started, 'rows': rows, 'publications': publications, 'requests': fixture.requests}
    (ROOT/'logs'/f'{probe.name}-{name}.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

def packets(run): return [row for row in run['rows'] if row['event']=='packet']
def summary(run): return next((row for row in reversed(run['rows']) if row['event']=='summary'), {})
def compare(run, baseline):
    problems = []
    if run['exit_code'] != 0: problems.append('reader did not recover before its deadline')
    if packets(run) != packets(baseline): problems.append(f"packet/PTS/payload sequence differs: {len(packets(run))} versus {len(packets(baseline))}")
    for key in ('frames', 'pcm_sha256'):
        if summary(run).get(key) != summary(baseline).get(key): problems.append(f'decoded {key} differs')
    if not run['publications']: problems.append('test did not reach EOF before appending')
    return problems

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('probe', type=Path); args=parser.parse_args()
    probe=args.probe.resolve()
    baseline=run(probe,'reference',5,True,0)
    assert baseline['exit_code']==0 and summary(baseline).get('frames',0)>190000, 'Reference media must decode normally'
    # Exactly 189 AAC-LC frames of 1024 samples from this 4-second encoder run.
    assert summary(baseline)['frames']==193536, summary(baseline)
    failures={}
    cases = [('recover-once',[(5,True)],1,False),
             ('recover-twice',[(3,False),(5,True)],1,False),
             ('recover-http11',[(3,False),(5,True)],1,True),
             ('recover-no-persistent',[(5,True)],0,True)]
    for name, transitions, persistent, http11 in cases:
        result=run(probe,name,2,False,summary(baseline)['frames'],transitions,persistent=persistent,http11=http11)
        problems=compare(result,baseline)
        if len(result['publications']) != len(transitions): problems.append('not all separate EOF recovery phases occurred')
        if problems: failures[name]=problems
    cancelled=run(probe,'interrupt-held',2,False,summary(baseline)['frames'],seconds=2,hold=1000)
    if cancelled['exit_code']==0 or cancelled['seconds']>4:
        failures['interrupt-held']=['native interrupted read did not stop within the bounded test deadline']
    report={'probe':str(probe),'reference_packets':len(packets(baseline)),'reference_frames':summary(baseline)['frames'],'reference_pcm_sha256':summary(baseline)['pcm_sha256'],'failures':failures}
    (ROOT/'logs'/f'{probe.name}-result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 1 if failures else 0
if __name__=='__main__': raise SystemExit(main())
