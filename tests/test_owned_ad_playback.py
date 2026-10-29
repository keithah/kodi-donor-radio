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
