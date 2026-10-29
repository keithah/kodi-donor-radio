#!/usr/bin/env python3
"""Loopback-only finite rolling HLS origin. The first media GET starts its clock.

One segment is initially published, then the window grows to six. Retired TS
files remain available, while their requests are marked expired in the ledger.
No listener is created on import. /stats observes; /reset starts a fresh run.
"""
import argparse
from bisect import bisect_right
from collections import Counter, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, math, os, re, sys, threading, time, uuid
from pathlib import Path

DEFAULT_REPO=Path(__file__).resolve().parents[3]

class RollingTimeline:
    def __init__(self, metadata):
        self.rate=metadata['sample_rate'];self.segments=metadata['segments'];self.anchor=None
        self.init_segment=metadata.get('init_segment')
        if self.init_segment is not None and self.init_segment!='init.mp4':raise ValueError('Unsupported initialization asset')
        extension='m4s' if self.init_segment else 'ts'
        if any(s['filename']!='segment%03d.%s'%(i,extension) for i,s in enumerate(self.segments)):raise ValueError('Unexpected segment asset')
        self.ends=[s['end_sample'] for s in self.segments]
        if self.rate!=48000 or not 1<=len(self.segments)<=128:raise ValueError('Unsupported bounded fixture')
        previous=0
        for i,s in enumerate(self.segments):
            if s['sequence']!=i or s['start_sample']!=previous or s['end_sample']<=previous:raise ValueError('Nonconsecutive segment map')
            previous=s['end_sample']
    def snapshot(self, host_ns, utc_ns, activate=False, uncertainty_ns=0):
        anchor=self.anchor
        if anchor is None:
            lead=self.ends[0]
            anchor={'first_get_host_ns':host_ns,'first_get_utc_ns':utc_ns,'initial_available_samples':lead,'clock_pair_uncertainty_ns':uncertainty_ns,'source_zero_host_ns_numerator':host_ns*self.rate-lead*1_000_000_000,'source_zero_utc_ns_numerator':utc_ns*self.rate-lead*1_000_000_000,'origin_denominator':self.rate}
            anchor['origin_host_ns']=anchor['source_zero_host_ns_numerator']//self.rate
            anchor['origin_utc_ns']=anchor['source_zero_utc_ns_numerator']//self.rate
            if activate:self.anchor=anchor
        media_samples=anchor['initial_available_samples']+max(0,host_ns-anchor['first_get_host_ns'])*self.rate//1_000_000_000
        count=max(1,min(len(self.segments),bisect_right(self.ends,media_samples)))
        first=max(0,count-6);visible=[]
        for s in self.segments[first:count]:
            visible.append(dict(s,program_date_time_ns=(anchor['source_zero_utc_ns_numerator']+s['start_sample']*1_000_000_000)//self.rate))
        return {'init_segment':self.init_segment,'started':self.anchor is not None,'sample_rate':self.rate,'anchor':anchor if self.anchor else None,'first_sequence':first,'published_count':count,'endlist':count==len(self.segments),'media_elapsed_samples':media_samples,'segments':visible}

def pdt(ns):
    seconds,nanos=divmod(ns,1_000_000_000)
    return datetime.fromtimestamp(seconds,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.')+('%06dZ'%(nanos//1000))

def render_playlist(snapshot):
    rate=snapshot['sample_rate'];segs=snapshot['segments']
    target=max(1,math.ceil(max(s['samples'] for s in segs)/rate))
    lines=['#EXTM3U','#EXT-X-VERSION:%d'%(7 if snapshot.get('init_segment') else 3),'#EXT-X-TARGETDURATION:%d'%target,'#EXT-X-MEDIA-SEQUENCE:%d'%snapshot['first_sequence']]
    if snapshot.get('init_segment'):lines.append('#EXT-X-MAP:URI="%s"'%snapshot['init_segment'])
    for s in segs:
        lines.extend(['#EXT-X-PROGRAM-DATE-TIME:'+pdt(s['program_date_time_ns']),'#EXTINF:%.9f,'%(s['samples']/rate),s['filename']])
    if snapshot['endlist']:lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines)+'\n'

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,default=Path(__file__).parent);ap.add_argument('--repo',type=Path,default=DEFAULT_REPO);ap.add_argument('--port',type=int,default=18789);ap.add_argument('--owned-beacons',action='store_true');args=ap.parse_args()
    root=args.root.resolve();metadata=json.loads((root/'metadata.json').read_text());assets=root/'assets'
    sys.path.insert(0,str(args.repo/'tools'))
    from ad_fixture_server import make_tracking_clock
    now,clock=make_tracking_clock()
    beacons=None
    if args.owned_beacons:
        sys.path.insert(0,str(args.repo/'plugin.audio.kodi_donor_radio/resources/lib'))
        from fixture_beacons import verify_fixture,BeaconEndpoints,sha
        metadata,proof=verify_fixture(root)
        beacons=BeaconEndpoints(root,proof,now)
    lock=threading.RLock();timeline=RollingTimeline(metadata);requests=Counter();ledger=deque(maxlen=2048);run_id=str(uuid.uuid4());asset_map={s['filename']:s for s in metadata['segments']}
    required_assets=list(asset_map)+([timeline.init_segment] if timeline.init_segment else [])
    if not all((assets/name).is_file() for name in required_assets):raise ValueError('Generated fixture assets are missing')
    log_path=root/'requests.jsonl'
    def write_event(row):
        with log_path.open('a') as f:f.write(json.dumps(dict(row,run_id=run_id),sort_keys=True)+'\n')
    def persist_state():
        state={'run_id':run_id,'clock':clock,'anchor':timeline.anchor,'metadata_identity':metadata['metadata_identity'],'source_first_pts':metadata['first_pts'],'pts_timebase':metadata.get('pts_timebase',[1,90000]),'sample_rate':48000}
        temporary=root/'server-state.json.tmp';temporary.write_text(json.dumps(state,indent=2)+'\n');os.replace(temporary,root/'server-state.json')
    def clock_pair():
        before=now();utc=time.time_ns();after=now();return (before+after)//2,utc,(after-before+1)//2
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def do_HEAD(self):self.handle_request(True)
        def do_GET(self):self.handle_request(False)
        def log_message(self,*args):pass
        def handle_request(self,head):
            nonlocal timeline,run_id
            route=self.path.split('?',1)[0];sequence=None
            host,utc,uncertainty=clock_pair();status=200;mime='application/json';body=b'{}';headers={}
            with lock:
                if beacons and route.startswith(('/track/','/vast/','/creative/','/owned-proof')):
                    status,mime,body=beacons.get(self.path,head,receipt_ns=host)
                elif route=='/reset':
                    if not head:
                        run_id=str(uuid.uuid4());timeline=RollingTimeline(metadata);requests.clear();ledger.clear();write_event({'event':'reset','host_ns':host});persist_state()
                    body=json.dumps({'run_id':run_id,'started':False}).encode()
                elif route=='/stats':
                    s=timeline.snapshot(host,utc)
                    body=json.dumps({'run_id':run_id,'requests':dict(requests),'beacons':beacons.stats() if beacons else {},'request_ledger':list(ledger),'clock':clock,'anchor':timeline.anchor,'window':{k:s[k] for k in ['started','first_sequence','published_count','endlist','media_elapsed_samples']},'source_metadata':str(root/'metadata.json')}).encode()
                elif route=='/live/index.m3u8':
                    before=timeline.anchor;s=timeline.snapshot(host,utc,activate=not head,uncertainty_ns=uncertainty)
                    if before is None and timeline.anchor is not None:write_event({'event':'clock_started','host_ns':host,'anchor':timeline.anchor});persist_state()
                    body=render_playlist(s).encode();mime='application/vnd.apple.mpegurl'
                elif route=='/live/init.mp4' and timeline.init_segment:
                    body=(assets/timeline.init_segment).read_bytes();mime='audio/mp4'
                    if beacons and sha(body)!=proof['assets'][timeline.init_segment]:raise ValueError('served_init_changed')
                elif re.fullmatch(r'/live/segment\d{3}\.(?:ts|m4s)',route) and route.rsplit('/',1)[1] in asset_map:
                    name=route.rsplit('/',1)[1];sequence=asset_map[name]['sequence'];body=(assets/name).read_bytes();mime='audio/mp4' if timeline.init_segment else 'video/mp2t'
                    if beacons and sha(body)!=proof['assets'][name]:raise ValueError('served_segment_changed')
                else:status=404;route='other';body=b'Not found\n';mime='text/plain'
                if route not in ['/stats','/reset'] and not (beacons and route.startswith('/track/')):
                    s=timeline.snapshot(host,utc);row={'event':'request','method':'HEAD' if head else 'GET','route':route,'status':status,'host_ns':host,'first_sequence':s['first_sequence'],'published_count':s['published_count'],'endlist':s['endlist'],'sequence':sequence,'expired':sequence is not None and sequence<s['first_sequence']}
            if mime in ('video/mp2t','audio/mp4') and self.headers.get('Range'):
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
                if match:
                    start=int(match.group(1));end=min(int(match.group(2)) if match.group(2) else len(body)-1,len(body)-1)
                    if start<=end:headers['Content-Range']='bytes %d-%d/%d'%(start,end,len(body));body=body[start:end+1];status=206
                    else:headers['Content-Range']='bytes */%d'%len(body);body=b'';status=416
                else:headers['Content-Range']='bytes */%d'%len(body);body=b'';status=416
            if route not in ['/stats','/reset'] and not (beacons and route.startswith('/track/')):
                with lock:
                    row['status']=status;row['response_bytes']=len(body);requests[route]+=1;ledger.append(row);write_event(row)
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers()
            if not head:
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.daemon_threads=True
    with lock:persist_state();write_event({'event':'server_ready','host_ns':now(),'port':args.port,'clock':clock})
    print('Ready: http://127.0.0.1:%d/live/index.m3u8 (clock starts on first GET)'%args.port,flush=True)
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        if beacons:beacons.close()
if __name__=='__main__':main()
