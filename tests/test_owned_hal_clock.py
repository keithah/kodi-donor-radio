"""HAL historical queries retain exact native provenance; no AE estimator."""
import dataclasses
import hashlib
import importlib.util
import json
from types import SimpleNamespace
import pytest
import owned_clock
from test_owned_hal_gain import binding as controls_binding, hal_hello as controls_hello

module = importlib.util.find_spec('owned_hal_clock')
if module:
    import owned_hal_clock as hal
else:
    hal = None


def binding():
    assert hal is not None, 'historical HAL provider is absent'
    return dataclasses.replace(controls_binding(),hal_phase='clock',hal_clock_version=1)


def setup():
    b=binding();now=[1_000_000_000]
    p=hal.OwnedHalClockProvider(b,lambda:now[0]);hello=controls_hello(b)
    hello.update(hal_phase='clock',hal_clock_version=1)
    assert p.bind_hello(hello)
    p.control('play',999_999_990)
    return p,now


def segment(i,source=False,**changes):
    t=1_000_000_000+i*32_000_000//3
    row=dict(kind='hal_segment',output_backend='hal_callback_v1',hal_contract_version=1,
        nonce='a'*32,incarnation='b'*32,timeline_sha256='c'*64,sequence=i+1,schema_version=1,
        device_generation=1,stream_generation=1,in_flight_generation=1,playback_revision=2,playback_epoch=2,
        gain_revision=1,gain_epoch=1,source_start=(i-18)*512 if source else 0,
        source_count=512 if source else 0,requested_frames=512,consumed_frames=512 if source else 0,
        copied_frames=512 if source else 0,zero_frames=0 if source else 512,applied_gain=1.,muted=False,
        playing=True,terminal=False,control_acquired=True,output_sample_time=i*512,output_flags=3,now_flags=3,
        output_ticks=t,now_ticks=t-1_000_000,entry_ticks=t-1_000_000,exit_ticks=t-1_000_000,
        timebase_numer=1,timebase_denom=1,output_host_ns=t,source_end_ns=t+10_666_667 if source else t,
        reason=0,publication_sequence=2,applied_intent=2,desired_intent=2,native_gate=1,authorized_native_epoch=1,
        resume_playback_revision=2,requested_playing=True,resume_granted=True,request_start_ns=t-10_000_000,
        request_end_ns=t-10_000_000,issue_ns=t-10_000_000,expiry_ns=t+490_000_000,volume=100,started=True,
        sample_zero_proven=True,clock_authority=False,beacons_enabled=False)
    row.update(changes);return row


def raw(row):return json.dumps(row,separators=(',',':'),sort_keys=True).encode()
def digest(row):return hashlib.sha256(raw(row)).hexdigest()


def query(seq,s,qualified=False,ready=False,**changes):
    t=s['output_ticks']+30_000
    zero=segment(18,True) if s['copied_frames'] else None
    row=dict(kind='hal_query',output_backend='hal_callback_v1',hal_contract_version=1,hal_phase='clock',hal_clock_version=1,
        nonce='a'*32,incarnation='b'*32,timeline_sha256='c'*64,sequence=seq,query_start_ns=t,query_end_ns=t,
        query_host_ns=t,qualified=qualified,readiness=ready,initial_failed=False,
        reason='qualified' if qualified else ('initial_capture_guard' if zero else 'startup_readiness'),
        source_frame=s['source_start']+1 if zero else 0,segment_sequence=s['sequence'] if zero else 0,
        segment_sha256=digest(s) if zero else '',applied_sequence=s['sequence'],applied_sha256=digest(s),
        source_zero_sequence=19 if zero else 0,source_zero_sha256=digest(zero) if zero else '',
        source_zero_ticks=zero['output_ticks'] if zero else 0,timebase_numer=1 if zero else 0,timebase_denom=1 if zero else 0,
        initial_deadline_ns=zero['output_ticks']+120_000_000 if zero else 0,native_gate=1,effective_intent=2,
        device_generation=1,stream_generation=1,playback_epoch=2,gain_epoch=1,readiness_count=9 if zero else seq,
        consistent_count=9 if qualified else 0,
        **{'applied_'+k:s[k] for k in ('request_start_ns','request_end_ns','issue_ns','expiry_ns')})
    row.update(changes);return row


def ready(p,now):
    for i in range(18):
        s=segment(i);now[0]=s['output_ticks']+30_000;p.ingest(raw(s))
        if i%2:
            ev=p.ingest(raw(query((i+1)//2,s,ready=i==17)))
            assert not ev.valid
    for i in range(18,21):
        s=segment(i,True);now[0]=s['output_ticks']+30_000;p.ingest(raw(s))
    return s


def test_historical_q_and_hash_survive_delayed_python_read():
    p,now=setup();s=ready(p,now);q=query(10,s,qualified=True)
    now[0]+=25_000_000;ev=p.ingest(raw(q));assert ev.valid,ev.reasons
    assert ev.query_host_ns==q['query_host_ns'] and ev.seconds==1025/48000
    assert ev.segment_sha256==digest(s) and ev.row_sha256==digest(q)
    assert ev.query_start_ns==ev.query_end_ns==q['query_host_ns']
    assert ev.source_zero_sha256==digest(segment(18,True))
    now[0]+=10_000_000;assert p.snapshot().seconds==ev.seconds


@pytest.mark.parametrize('field,value',[('output_flags',0),('output_sample_time',.5),('output_sample_time',2**53),
    ('applied_gain',10**400),('entry_ticks',2**64),('timebase_numer',0),('copied_frames',513)])
def test_invalid_segment_permanently_retires(field,value):
    p,now=setup();p.ingest(raw(segment(0,**{field:value})));assert p.retired


@pytest.mark.parametrize('change',[{'hal_phase':'controls'},{'hal_clock_version':None},{'hal_clock_version':True}])
def test_explicit_clock_phase_cannot_accept_controls_hello(change):
    p,now=setup();h=controls_hello(p.binding);h.update(hal_phase='clock',hal_clock_version=1);h.update(change)
    assert not p.bind_hello(h) and p.retired


@pytest.mark.parametrize('delay',[100_000_001,120_000_001])
def test_late_initial_qualification_cannot_be_reset_by_resume(delay):
    p,now=setup();s=ready(p,now);q=query(10,s,qualified=True);now[0]=q['query_host_ns']+delay
    ev=p.ingest(raw(q));assert not ev.valid
    p.control('pause',now[0]+1);p.control('resume',now[0]+2)
    assert not p.snapshot().valid


def test_mapped_frame_and_s0_guard_are_both_strict():
    p,now=setup();s=ready(p,now)
    q=query(10,s,qualified=True,source_frame=1024,query_host_ns=s['output_ticks'],query_start_ns=s['output_ticks'],query_end_ns=s['output_ticks'])
    assert not p.ingest(raw(q)).valid


def test_no_ae_provider_can_accept_hal_binding():
    with pytest.raises(ValueError):owned_clock.OwnedClockProvider(binding(),lambda:pytest.fail('AE constructed'))


def test_gain_epoch_cannot_skip_nine_query_reacquisition():
    p,now=setup();s=ready(p,now);assert p.ingest(raw(query(10,s,qualified=True))).valid
    s=segment(21,True,gain_epoch=2,gain_revision=2,applied_intent=3,desired_intent=3,volume=90,applied_gain=10**(-6/20))
    now[0]=s['output_ticks']+30_000;p.ingest(raw(s))
    q=query(11,s,qualified=True,gain_epoch=2,effective_intent=3)
    assert not p.ingest(raw(q)).valid


@pytest.mark.parametrize('transition', ['pause', 'gain'])
def test_historical_query_during_pending_control_or_gain_does_not_retire(transition):
    p,now=setup();s=ready(p,now)
    first=query(10,s,qualified=True)
    assert p.ingest(raw(first)).valid
    if transition=='pause':
        p.control('pause',first['query_host_ns']+1)
    else:
        p.gain_pending(SimpleNamespace(effective_epoch=1))
        p.gain_pending(SimpleNamespace(effective_epoch=2))
    qtime=first['query_host_ns']+2
    now[0]=qtime+100
    historical=query(11,s,qualified=True,query_start_ns=qtime,query_end_ns=qtime,query_host_ns=qtime)
    snapshot=p.ingest(raw(historical))
    assert not snapshot.valid
    assert snapshot.reasons==('control_or_gain_pending',)
    assert not p.retired


def test_ordinary_renewal_keeps_qualified_epoch_and_historical_mapping():
    p,now=setup();s=ready(p,now);assert p.ingest(raw(query(10,s,qualified=True))).valid
    s=segment(21,True,gain_revision=2,publication_sequence=3)
    now[0]=s['output_ticks']+30_000;p.ingest(raw(s))
    ev=p.ingest(raw(query(11,s,qualified=True)));assert ev.valid,ev.reasons
    assert ev.seconds==1537/48000
