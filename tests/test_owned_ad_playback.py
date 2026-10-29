import dataclasses
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

import pytest

from ad_models import OutputSegment, Placement, Presentation, ResolvedAd, TrackingUrl
from ad_tracking import BeaconWorker
from test_owned_clock import BASE, PACKET, binding, hello, estimator_factory, row
import owned_clock as clock

try:
    import owned_ad_playback as playback
except ImportError:
    playback = None

EVENTS=('impression','start','firstQuartile','midpoint','thirdQuartile','complete')


class Client:
    def __init__(self): self.urls=[]; self.entered=threading.Event(); self.release=threading.Event(); self.block=False
    def open(self,url,*args,**kwargs):
        self.urls.append(url); self.entered.set()
        if self.block: self.release.wait(2)
        return self
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,n): return b''
    status=204


def presentation():
    placements=[]
    for name,start,end in [('preroll',0,96),('A',192,288),('B',384,576),('C',672,768)]:
        ad=ResolvedAd(name, 'http://127.0.0.1:18789/media/'+name,
                      (end-start)*1024/48000,
                      tuple(TrackingUrl(e,'http://127.0.0.1:18789/track/'+name+'/'+e) for e in EVENTS),())
        placements.append(Placement(name,ad,start*1024,end*1024))
    return Presentation((OutputSegment(0,0,'e'*64,0,clock.TOTAL_SAMPLES,None),),tuple(placements),True,False)


def setup():
    assert playback is not None, 'owned playback adapter is not implemented'
    view=presentation(); ticks=[BASE]
    b=binding(timeline_sha256=playback.timeline_sha256(view,PACKET))
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:ticks[0])
    p.bind_hello(hello(timeline_sha256=b.timeline_sha256))
    c=Client(); emitted=[]
    a=playback.OwnedAdPlayback(b,p,view,c,threading.Event(),emitted.append)
    a.control('play',BASE-25)
    waiting=row(-1,revision=1,state='waiting',issued=BASE-100,applied=BASE-40)
    waiting.update(mach_absolute_ns=BASE-10,steady_ns=BASE-10,delay_query_start_steady_ns=BASE-10,delay_query_end_steady_ns=BASE-10)
    waiting['ownership'].update(timeline_sha256=b.timeline_sha256,eligible=False,output_guard_valid=False,output_guard_generation=1,reason='output_guard_unarmed')
    p.ingest(json.dumps(waiting).encode())
    return a,p,ticks,c,emitted


def observe(a,p,ticks,i,**kwargs):
    r=row(i,**kwargs); r['ownership']['timeline_sha256']=p.binding.timeline_sha256
    ticks[0]=r['mach_absolute_ns']; ev=p.ingest(json.dumps(r).encode()); a.observe(ev)
    # Let the real worker authorize within the deterministic 100ms clock horizon.
    deadline=time.monotonic()+1
    while a.worker._queue.unfinished_tasks and time.monotonic()<deadline: time.sleep(.001)
    return ev


def events(client,name): return [urlsplit(u).path.split('/')[-1] for u in client.urls if '/'+name+'/' in u]


def test_real_tracker_worker_six_events_once_no_synthetic_zero():
    a,p,t,c,emitted=setup()
    try:
        for i in range(850): observe(a,p,t,i)
        assert events(c,'preroll')==list(EVENTS)
        assert events(c,'A')==list(EVENTS)
        assert isinstance(a.tracker, __import__('ad_tracking').AdTracker)
        auth=[e for e in emitted if e['kind']=='owned_beacon_authorization' and e['authorized']]
        assert len(auth)==24
        assert all(e['query_host_ns']<=e['authorization_ns'] and e['row_sha256'] for e in auth)
        assert all('http' not in json.dumps(e) and 'nonce' not in e for e in emitted)
        assert a.tracker._last_good.monotonic_seconds==pytest.approx((BASE+849*20_000_000)/1e9)
        assert a.tracker._progress['preroll'].started
    finally: a.close()


def test_pause_taints_only_interrupted_ad_later_ad_tracks():
    a,p,t,c,_=setup()
    try:
        for i in range(450): observe(a,p,t,i)
        a.control('pause',BASE+8_990_000_000)
        assert not p.snapshot().valid
        a.control('resume',BASE+9_000_000_000)
        observe(a,p,t,450,revision=3,state='pause',issued=BASE+8_990_000_000,applied=BASE+8_995_000_000)
        for i in range(451,850):
            observe(a,p,t,i,revision=4,state='resume',issued=BASE+9_000_000_000,applied=BASE+9_005_000_000)
        assert events(c,'B')==['impression','start']
        assert events(c,'C')==list(EVENTS)
    finally:a.close()


def test_late_onset_no_preroll_and_no_eof_completion():
    a,p,t,c,_=setup()
    try:
        # Consume native early rows without calling the adapter; first evidence >250 ms.
        for i in range(17):
            r=row(i);r['ownership']['timeline_sha256']=p.binding.timeline_sha256
            t[0]=r['mach_absolute_ns'];p.ingest(json.dumps(r).encode())
        for i in range(17,96):observe(a,p,t,i)
        a.control('stop',t[0]+1)
        assert events(c,'preroll')==[]
    finally:a.close()


def test_old_snapshot_callback_race_cannot_enqueue():
    a,p,t,c,_=setup()
    try:
        for i in range(9):
            r=row(i);r['ownership']['timeline_sha256']=p.binding.timeline_sha256
            t[0]=r['mach_absolute_ns'];ev=p.ingest(json.dumps(r).encode())
        assert ev.valid
        a.control('pause',t[0]+1)
        assert not a.observe(ev)
        assert c.urls==[]
    finally:a.close()


def test_queued_event_authorization_revoked_before_network():
    client=Client(); client.block=True; worker=BeaconWorker(client,threading.Event(),clock.URL)
    allowed=[True]
    try:
        assert worker.enqueue('blocking','start',clock.URL+'/blocking')
        assert client.entered.wait(1)
        assert worker.enqueue('pending','start',clock.URL+'/pending',authorize=lambda:allowed[0])
        allowed[0]=False; client.release.set();worker.close()
        assert client.urls==[clock.URL+'/blocking']
    finally:client.release.set();worker.close()


@pytest.mark.parametrize('damage',['short','gap','hash','oversize','placements','foreign','mutable','origin','identity'])
def test_bounded_immutable_identity_rejected(damage):
    assert playback is not None, 'owned playback adapter is not implemented'
    p=presentation(); b=binding(timeline_sha256=playback.timeline_sha256(p,PACKET))
    if damage=='short':p=dataclasses.replace(p,segments=(dataclasses.replace(p.segments[0],samples=1024),))
    if damage=='gap':p=dataclasses.replace(p,segments=(dataclasses.replace(p.segments[0],start_sample=1024),))
    if damage=='hash':p=dataclasses.replace(p,segments=(dataclasses.replace(p.segments[0],resource_id='filename'),))
    if damage=='oversize':p=dataclasses.replace(p,segments=p.segments*129)
    if damage=='placements':p=dataclasses.replace(p,placements=p.placements*3)
    if damage=='foreign':
        ad=dataclasses.replace(p.placements[0].ad,tracking=(TrackingUrl('start','https://production.example/track'),))
        p=dataclasses.replace(p,placements=(dataclasses.replace(p.placements[0],ad=ad),))
    if damage=='mutable':p=dataclasses.replace(p,segments=list(p.segments))
    if damage=='origin':b=dataclasses.replace(b,selected_url='http://localhost:18789/live/index.m3u8')
    if damage=='identity':b=dataclasses.replace(b,timeline_sha256='f'*64)
    provider=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    with pytest.raises(ValueError):playback.OwnedAdPlayback(b,provider,p,Client(),threading.Event(),lambda e:None)


def test_canonical_identity_binds_tracking_and_segment_bytes():
    assert playback is not None, 'owned playback adapter is not implemented'
    p=presentation(); raw=playback.canonical_timeline(p,PACKET)
    assert hashlib.sha256(raw).hexdigest()==playback.timeline_sha256(p,PACKET)
    value=json.loads(raw); assert value['first_packet_sha256']==PACKET
    changed=dataclasses.replace(p,segments=(dataclasses.replace(p.segments[0],resource_id='f'*64),))
    assert playback.timeline_sha256(changed,PACKET)!=playback.timeline_sha256(p,PACKET)


def test_fixture_origin_override_remains_loopback_only():
    assert playback._local_url('http://127.0.0.1:43210/track/start','http://127.0.0.1:43210')
    assert not playback._local_url('http://example.com:43210/track/start','http://example.com:43210')
    assert not playback._local_url('https://127.0.0.1:43210/track/start','https://127.0.0.1:43210')


def test_stop_callback_never_waits_for_blocked_client_and_defers_join():
    a,p,t,c,_=setup();c.block=True
    try:
        for i in range(9):
            r=row(i);r['ownership']['timeline_sha256']=p.binding.timeline_sha256
            t[0]=r['mach_absolute_ns'];e=p.ingest(json.dumps(r).encode())
            a.observe(e)
        assert c.entered.wait(1)
        stopped=threading.Event()
        thread=threading.Thread(target=lambda:(a.control('stop',t[0]+1),stopped.set()))
        thread.start()
        assert stopped.wait(.1)
        assert a.worker._stop.is_set()
        assert not a.worker.enqueue('late','start',clock.URL+'/late')
        c.release.set();thread.join(1);a.close()
        assert not a.worker._thread.is_alive()
    finally:c.release.set();a.close()


def test_native_terminal_does_not_manufacture_complete_and_stops_worker():
    a,p,t,c,_=setup()
    try:
        for i in range(101):observe(a,p,t,i)
        assert events(c,'preroll')==list(EVENTS[:-1])
        observe(a,p,t,101,drained=True,audioengine_delay_s=None)
        assert events(c,'preroll')==list(EVENTS[:-1])
        assert a.worker._stop.is_set()
    finally:a.close()


def test_presentation_is_complete_even_when_publisher_window_is_trimmed():
    a,p,t,c,_=setup()
    try:
        original=a.presentation
        rolling=dataclasses.replace(original,segments=())
        assert rolling.segments==()
        for i in range(850):observe(a,p,t,i)
        assert a.presentation is original
        assert events(c,'C')==list(EVENTS)
    finally:a.close()


def test_owned_queued_start_is_dropped_after_pause_with_evidence():
    a,p,t,c,emitted=setup();c.block=True
    try:
        for i in range(9):
            r=row(i);r['ownership']['timeline_sha256']=p.binding.timeline_sha256
            t[0]=r['mach_absolute_ns'];a.observe(p.ingest(json.dumps(r).encode()))
        assert c.entered.wait(1)
        a.control('pause',t[0]+1)
        c.release.set()
        deadline=time.monotonic()+1
        while a.worker._queue.unfinished_tasks and time.monotonic()<deadline:time.sleep(.001)
        assert events(c,'preroll')==['impression']
        dropped=[e for e in emitted if e['kind']=='owned_beacon_authorization' and not e['authorized']]
        assert len(dropped)==1 and dropped[0]['event']=='start'
        assert dropped[0]['row_sequence']==10
    finally:c.release.set();a.close()


def test_adapter_rejects_different_declared_player_contract():
    view=presentation();b=binding(timeline_sha256=playback.timeline_sha256(view,PACKET))
    other=dataclasses.replace(b,player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer')
    provider=clock.OwnedClockProvider(other,estimator_factory,lambda:BASE)
    with pytest.raises(ValueError):
        playback.OwnedAdPlayback(b,provider,view,Client(),threading.Event(),lambda row:None)


def test_hal_snapshot_provenance_survives_tracker_detail_projection():
    a,p,t,c,_=setup()
    try:
        ev=clock.OwnedClockSnapshot(valid=True,seconds=.04,query_host_ns=10,row_sequence=7,row_sha256='a'*64,
            backend='hal_callback_v1',query_start_ns=8,query_end_ns=12,segment_sha256='b'*64,
            applied_sequence=9,applied_sha256='c'*64,effective_intent=3,playback_epoch=2,gain_epoch=1,
            device_generation=1,stream_generation=1,source_zero_sequence=4,source_zero_sha256='d'*64,
            source_zero_ticks=1,timebase_numer=1,timebase_denom=1,initial_deadline_ns=120000001)
        details=a._details(ev)
        for key in ('backend','query_start_ns','query_end_ns','segment_sha256','applied_sequence','applied_sha256',
                    'effective_intent','playback_epoch','gain_epoch','device_generation','stream_generation',
                    'source_zero_sequence','source_zero_sha256','source_zero_ticks','timebase_numer','timebase_denom','initial_deadline_ns'):
            assert details[key]==getattr(ev,key)
    finally:a.close()


def test_hal_enqueue_stages_intent_without_native_io_or_worker_queue():
    a,p,t,c,emitted=setup()
    try:
        a._hal=True;a._event_intents=[];a._evidence=clock.OwnedClockSnapshot(valid=True,seconds=.04,query_host_ns=100,row_sequence=1,row_sha256='a'*64,backend='hal_callback_v1')
        a._decision_ns=101;a._decision_id='b'*64;a._decision_proof=None
        p.authorize=lambda *args:pytest.fail('nested native authorization under tracker lock')
        assert a._enqueue('preroll','start','http://127.0.0.1:18789/track/preroll/start')
        assert len(a._event_intents)==1 and not c.urls and a.worker._queue.empty()
    finally:a.close()


def test_hal_initial_tracker_queue_dispatch_real_native_files(tmp_path):
    import os,subprocess
    from owned_hal_clock import OwnedHalClockProvider
    from owned_hal_authorization import HalAuthorizationTransport
    from test_owned_hal_clock import binding as hb,segment as hs,query as hq,raw as hr,digest as hd
    from test_owned_hal_gain import hal_hello
    executable=os.environ.get('HAL_AUTH_HARNESS')
    if not executable:pytest.skip('compile hal_authorization_harness.cpp and set HAL_AUTH_HARNESS')
    transport=HalAuthorizationTransport(tmp_path,clock.mach_host_ns)
    native=subprocess.Popen([executable,str(tmp_path),'bridge'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    deadline=time.monotonic()+1
    while not (tmp_path/'started').exists() and native.poll() is None and time.monotonic()<deadline:time.sleep(.001)
    assert (tmp_path/'started').exists()
    view=presentation();offset=clock.mach_host_ns()-(hs(20,True)['output_ticks']+30000)
    b=dataclasses.replace(hb(),timeline_sha256=playback.timeline_sha256(view,'d'*64),launch_mach_ns=1_000_000_000-100+offset)
    now=[1_000_000_000+offset];p=OwnedHalClockProvider(b,lambda:now[0]);hello=hal_hello(b)
    hello.update(native_open_mach_ns=1_000_000_000-50+offset,hal_phase='clock',hal_clock_version=1)
    assert p.bind_hello(hello);p.control('play',1_000_000_000-10+offset)
    segments={};final=None
    for i in range(21):
        s=hs(i,i>=18);s['timeline_sha256']=b.timeline_sha256
        for key in ('output_ticks','now_ticks','entry_ticks','exit_ticks','output_host_ns','source_end_ns','request_start_ns','request_end_ns','issue_ns','expiry_ns'):s[key]+=offset
        segments[i+1]=s;now[0]=s['output_ticks']+30000;p.ingest(hr(s))
        if i<18 and i%2 or i==20:
            q=hq((i+1)//2 if i<18 else 10,hs(i,i>=18),qualified=i==20,ready=i==17)
            q['timeline_sha256']=b.timeline_sha256
            for key in ('query_start_ns','query_end_ns','query_host_ns','applied_request_start_ns','applied_request_end_ns','applied_issue_ns','applied_expiry_ns'):q[key]+=offset
            for field in ('segment','applied','source_zero'):
                if q[field+'_sequence']:q[field+'_sha256']=hd(segments[q[field+'_sequence']])
            if i>=18:q['source_zero_ticks']+=offset;q['initial_deadline_ns']+=offset
            final=p.ingest(hr(q))
    assert final.valid,final.reasons
    (tmp_path/'.query.tmp').write_bytes(hr(q));(tmp_path/'.query.tmp').rename(tmp_path/'query.json')
    deadline=time.monotonic()+1
    while not (tmp_path/'ready').exists() and native.poll() is None and time.monotonic()<deadline:time.sleep(.0005)
    p.now_ns=clock.mach_host_ns;p.authorization=transport
    c=Client();emitted=[];a=playback.OwnedAdPlayback(b,p,view,c,threading.Event(),emitted.append)
    try:
        assert a.observe(final)
        deadline=time.monotonic()+1
        while a.worker._queue.unfinished_tasks and time.monotonic()<deadline:time.sleep(.001)
        assert events(c,'preroll')==['impression','start'],emitted
        queues=[r for r in emitted if r['kind']=='owned_beacon_queue'];auths=[r for r in emitted if r['kind']=='owned_beacon_authorization']
        assert len(queues)==len(auths)==2 and all(r['queued'] for r in queues) and all(r['authorized'] for r in auths)
        assert all(r['decision_ns']<=r['queue_ns']<=final.initial_deadline_ns for r in auths)
        assert all(r['query_host_ns']==final.query_host_ns and r['segment_sha256']==final.segment_sha256 for r in auths)
        stdout,stderr=native.communicate(timeout=2);assert native.returncode==0,(stdout,stderr)
    finally:
        a.close();transport.close()
        if native.poll() is None:native.terminate();native.wait(timeout=2)
