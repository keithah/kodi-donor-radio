"""Owned session tests use the byte-frozen estimator, never a fitted clock."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

try:
    import owned_clock as clock
except ImportError:
    clock = None

BASE = 1_000_000_000
NONCE, INC, SHA, PACKET = 'a'*32, 'b'*32, 'c'*64, 'd'*64


def estimator_factory():
    path = Path(__file__).resolve().parents[1] / 'docs/patches/owned-hls-lab/clock-candidate-v2/estimator.py'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87'
    spec = importlib.util.spec_from_file_location('owned_test_frozen', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Estimator()


def binding(**changes):
    assert clock is not None, 'owned session provider is not implemented'
    args = dict(nonce=NONCE, incarnation=None, timeline_sha256=SHA,
                first_packet_sha256=PACKET, selected_url=clock.URL,
                launch_mach_ns=BASE-100, native_pid=os.getpid())
    args.update(changes)
    return clock.OwnedClockBinding(**args)


def hello(**changes):
    args = dict(protocol=1, nonce=NONCE, incarnation=INC, timeline_sha256=SHA,
                first_packet_sha256=PACKET, native_pid=os.getpid(),
                native_open_mach_ns=BASE-50, stream_generation=1,
                qualification_scope='controlled_fixture')
    args.update(changes)
    return args


def row(i, revision=2, state='play', issued=BASE-25, applied=BASE-20, **changes):
    t = BASE+i*20_000_000
    own = dict(protocol=1, nonce=NONCE, incarnation=INC, timeline_sha256=SHA,
               observation_sequence=i+2, control_revision=revision, control_state=state,
               control_issued_mach_ns=issued, control_applied_mach_ns=applied,
               state_generation_before=3, state_generation_after=3, stream_generation=1,
               source_zero_confirmed=True, eligible=True, reason='eligible',
               output_guard_generation=2, output_guard_valid=True,
               output_guard_scope='fixture_default_device_observation')
    args = dict(kind='clock_sample', steady_ns=t, mach_absolute_ns=t,
                delay_query_start_steady_ns=t, delay_query_end_steady_ns=t,
                accepted_adddata_frames=48000+i*960, audioengine_delay_s=.99,
                source_epoch=0, control_state=state, playback_started=True,
                renderer_qos_request_confirmed=True, failed=False, drained=False,
                first_source_pts=0, ownership=own)
    args.update(changes)
    return args


def session(**changes):
    ticks = [BASE]
    b = binding(**changes)
    p = clock.OwnedClockProvider(b, estimator_factory, lambda:ticks[0])
    p.bind_hello(hello())
    p.control('play', BASE-25)
    waiting=row(-1,revision=1,state='waiting',issued=BASE-100,applied=BASE-40)
    waiting.update(mach_absolute_ns=BASE-10,steady_ns=BASE-10,
                   delay_query_start_steady_ns=BASE-10,delay_query_end_steady_ns=BASE-10)
    waiting['ownership'].update(eligible=False,output_guard_valid=False,output_guard_generation=1,reason='output_guard_unarmed')
    p.ingest(json.dumps(waiting).encode())
    return p, ticks


def feed(p, ticks, r):
    ticks[0] = r['mach_absolute_ns']
    raw = json.dumps(r, separators=(',', ':')).encode()
    return p.ingest(raw)


def acquire(p, ticks, start=0, **changes):
    for i in range(start, start+9):
        result = feed(p, ticks, row(i, **changes))
    return result


def test_real_frozen_acquisition_invalid_never_returns_prior_seconds():
    p, t = session()
    for i in range(9):
        result = feed(p, t, row(i))
        assert result.valid == (i == 8)
    assert result.seconds == pytest.approx(.17)
    assert result.row_sha256 == hashlib.sha256(json.dumps(row(8), separators=(',', ':')).encode()).hexdigest()
    t[0] += 100_000_001
    assert not p.snapshot().valid
    assert p.snapshot().seconds is None


@pytest.mark.parametrize('changes', [dict(nonce='e'*32), dict(incarnation='e'*32),
    dict(timeline_sha256='e'*64), dict(native_pid=999999), dict(first_packet_sha256='e'*64),
    dict(native_open_mach_ns=BASE-101), dict(native_open_mach_ns=BASE+1), dict(protocol=True)])
def test_wrong_hello_permanently_retires(changes):
    p = clock.OwnedClockProvider(binding(), estimator_factory, lambda:BASE) if clock else None
    assert p is not None, 'owned session provider is not implemented'
    # A preselected incarnation must match if supplied by an existing bound launch.
    if 'incarnation' in changes:
        p = clock.OwnedClockProvider(binding(incarnation=INC), estimator_factory, lambda:BASE)
    assert not p.bind_hello(hello(**changes))
    assert p.retired
    assert not p.bind_hello(hello())


def test_prehello_controls_preserve_waiting_launch_timestamp():
    b = binding()
    p = clock.OwnedClockProvider(b, estimator_factory, lambda:BASE)
    p.control('play', BASE-25)
    p.control('pause', BASE+1)
    p.control('resume', BASE+2)
    assert p.pending_commands() == ()
    assert p.bind_hello(hello())
    commands = p.pending_commands()
    assert [(c.revision,c.state,c.issued_mach_ns) for c in commands] == [
        (1,'waiting',BASE-100),(2,'play',BASE-25),(3,'pause',BASE+1),(4,'resume',BASE+2)]
    assert all(c.incarnation == INC for c in commands)


@pytest.mark.parametrize('field,value', [('nonce','e'*32),('incarnation','e'*32),
    ('timeline_sha256','e'*64),('observation_sequence',3),('stream_generation',2),
    ('control_revision',9),('control_issued_mach_ns',BASE-26),('reason','output_guard')])
def test_owner_sequence_and_control_poison_never_feed_estimator(field,value):
    p,t = session()
    r = row(0); r['ownership'][field] = value
    out = feed(p,t,r)
    assert not out.valid and out.seconds is None and p.retired
    assert p.estimator.origin_ns is None


def test_stale_future_rows_cannot_prime_acquisition():
    for offset in (-1,100_000_001):
        p,t = session()
        for i in range(9):
            r = row(i); t[0] = r['mach_absolute_ns']+offset
            assert not p.ingest(json.dumps(r).encode()).valid
        assert p.estimator.origin_ns is None
        assert acquire(p,t,9).valid


def test_pause_resume_requires_current_ack_and_full_reacquisition():
    p,t = session(); assert acquire(p,t).valid
    p.control('pause',BASE+170_000_000)
    p.control('resume',BASE+171_000_000)
    assert p.snapshot().seconds is None
    assert not feed(p,t,row(9,revision=3,state='pause',issued=BASE+170_000_000,applied=BASE+175_000_000)).valid
    for i in range(10,19):
        out = feed(p,t,row(i, revision=4,state='resume',issued=BASE+171_000_000,applied=BASE+180_000_000))
        assert out.valid == (i==18)


@pytest.mark.parametrize('change', ['terminal_bad_number','epoch','rewind','output','duplicate','route'])
def test_permanent_retirement(change):
    p,t=session(); assert acquire(p,t).valid
    r=row(9)
    if change=='terminal_bad_number': r.update(drained=True,audioengine_delay_s=None)
    if change=='epoch': r['source_epoch']=1
    if change=='rewind': r['accepted_adddata_frames']=0
    if change=='output': r['ownership'].update(eligible=False,output_guard_valid=False,output_guard_generation=3,reason='output_guard')
    if change=='duplicate': r=row(8)
    if change=='route': p.route('https://example.com/live')
    assert not feed(p,t,r).valid and p.retired
    p.control('resume',BASE+190_000_000)
    assert not acquire(p,t,10).valid


def test_unarmed_is_pending_and_bad_width_cannot_prime():
    p,t=session(); r=row(0)
    r['ownership'].update(eligible=False,output_guard_valid=False,output_guard_generation=1,reason='output_guard_unarmed')
    assert not feed(p,t,r).valid and not p.retired
    r=row(1); r['delay_query_end_steady_ns']+=5_000_001; t[0]=BASE+26_000_000
    assert not p.ingest(json.dumps(r).encode()).valid
    assert p.estimator.origin_ns is None
    assert acquire(p,t,2).valid


def test_transport_atomic_commands_and_immutable_hello(tmp_path):
    b=binding(); tr=clock.OwnedClockTransport.create(tmp_path,b)
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    directory=tmp_path/NONCE
    (directory/'native.jsonl').write_bytes(b''); (directory/'native.jsonl').chmod(0o600)
    (directory/'hello.json').write_text(json.dumps(hello())); (directory/'hello.json').chmod(0o600)
    assert tr.poll(p)==()
    p.control('play',BASE-25); tr.flush_commands(p)
    assert (directory/'command-000001.txt').read_text()==f'v1 {NONCE} {INC} 1 waiting {BASE-100}\n'
    assert (directory/'command-000002.txt').stat().st_mode & 0o777 == 0o600
    old=(directory/'command-000001.txt').read_bytes(); tr.flush_commands(p)
    assert (directory/'command-000001.txt').read_bytes()==old
    (directory/'hello.json').write_text(json.dumps(hello(native_pid=999)))
    with pytest.raises(clock.OwnedTransportError): tr.poll(p)
    assert p.retired
    tr.close()


@pytest.mark.parametrize('damage',['symlink','replace','truncate','backlog','partial_long','directory'])
def test_transport_pins_and_bounds(tmp_path,damage):
    b=binding(); tr=clock.OwnedClockTransport.create(tmp_path,b)
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    d=tmp_path/NONCE; ledger=d/'native.jsonl'; hp=d/'hello.json'
    ledger.write_bytes(b'{"kind":"source_packet"}\n'); ledger.chmod(0o600)
    hp.write_text(json.dumps(hello())); hp.chmod(0o600)
    tr.poll(p)
    if damage=='symlink': ledger.unlink(); ledger.symlink_to(hp)
    if damage=='replace': ledger.unlink(); ledger.write_bytes(b''); ledger.chmod(0o600)
    if damage=='truncate': ledger.write_bytes(b'')
    if damage=='backlog': ledger.write_bytes(b' '*262145)
    if damage=='partial_long': ledger.write_bytes(b'{"kind":"source_packet"}\n'+b'x'*16385)
    if damage=='directory': d.rename(tmp_path/'retained'); d.mkdir(mode=0o700)
    with pytest.raises(clock.OwnedTransportError): tr.poll(p)
    assert p.retired
    tr.close()


def test_acknowledgment_gap_is_permanent():
    p,t=session(); p.control('pause',BASE+1); p.control('resume',BASE+2)
    r=row(0,revision=4,state='resume',issued=BASE+2,applied=BASE+3)
    r['mach_absolute_ns']=BASE+10
    assert not feed(p,t,r).valid
    assert p.retired


def test_published_commands_never_have_two_links(tmp_path, monkeypatch):
    b=binding(); tr=clock.OwnedClockTransport.create(tmp_path,b)
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    p.bind_hello(hello())
    def reject_link(*args,**kwargs):
        pytest.fail('hard-link publication exposes a transient native-invalid link count')
    monkeypatch.setattr(os,'link',reject_link)
    tr.flush_commands(p)
    assert (tmp_path/NONCE/'command-000001.txt').stat().st_nlink==1
    tr.close()


def test_command_publication_collision_never_overwrites(tmp_path):
    b=binding();tr=clock.OwnedClockTransport.create(tmp_path,b)
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE);p.bind_hello(hello())
    target=tmp_path/NONCE/'command-000001.txt';target.write_bytes(b'retained');target.chmod(0o600)
    with pytest.raises(clock.OwnedTransportError):tr.flush_commands(p)
    assert target.read_bytes()==b'retained' and p.retired
    tr.close()


def test_pause_can_be_queued_even_after_mapping_retirement():
    p,t=session();p.retire('source_problem')
    command=p.control('pause',BASE+1)
    assert command.state=='pause'
    assert p.pending_commands()[-1]==command
    assert p.snapshot().seconds is None


def test_evaluation_freshness_crossing_rejects_result():
    p,t=session();acquire(p,t)
    times=iter([BASE+279_000_000,BASE+281_000_000])
    p.now_ns=lambda:next(times)
    result=p.ingest(json.dumps(row(9)).encode())
    assert not result.valid and result.seconds is None
    assert 'evaluation_freshness' in result.reasons


@pytest.mark.parametrize('states',[('pause',),('resume',),('play','play')])
def test_invalid_callback_state_order_never_publishes_bad_command(states):
    b=binding();p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    p.bind_hello(hello())
    command=None
    for i,state in enumerate(states):command=p.control(state,BASE+i)
    assert command is None and p.retired


def test_ledger_replacement_during_read_rejected_before_estimator(tmp_path,monkeypatch):
    b=binding();tr=clock.OwnedClockTransport.create(tmp_path,b)
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    d=tmp_path/NONCE;hp=d/'hello.json';ledger=d/'native.jsonl'
    hp.write_text(json.dumps(hello()));hp.chmod(0o600);ledger.write_bytes(b'');ledger.chmod(0o600)
    tr.poll(p)
    original=os.read
    def replace_after_read(fd,length):
        data=original(fd,length)
        if fd==tr.ledger:
            ledger.rename(d/'retained.jsonl');ledger.write_bytes(b'');ledger.chmod(0o600)
        return data
    monkeypatch.setattr(os,'read',replace_after_read)
    with pytest.raises(clock.OwnedTransportError):tr.poll(p)
    assert p.retired and p.estimator.origin_ns is None
    tr.close()


@pytest.mark.parametrize('value',[False,1,None])
def test_confirmed_nonzero_or_non_numeric_source_origin_retires(value):
    p,t=session();r=row(0,first_source_pts=value)
    assert not feed(p,t,r).valid and p.retired
    assert p.estimator.origin_ns is None


@pytest.mark.parametrize('discontinuity', ['epoch','counter','native_generation','output_generation'])
@pytest.mark.parametrize('damage', ['delay','query','started'])
def test_irreversible_discontinuity_survives_unrelated_invalid_schema(discontinuity,damage):
    p,t=session();assert acquire(p,t).valid
    r=row(9)
    if discontinuity=='epoch':r['source_epoch']=1
    if discontinuity=='counter':r['accepted_adddata_frames']=0
    if discontinuity=='native_generation':r['ownership']['state_generation_after']=2
    if discontinuity=='output_generation':r['ownership']['output_guard_generation']=1
    if damage=='delay':r['audioengine_delay_s']=None
    if damage=='query':r['steady_ns']=None
    if damage=='started':r['playback_started']='invalid'
    result=feed(p,t,r)
    assert p.retired and not result.valid and result.seconds is None
    assert p.reason=='epoch_counter_or_generation_rewind'
    estimator_last=p.estimator.last_t
    assert not acquire(p,t,10).valid
    assert p.retired and p.estimator.last_t==estimator_last


@pytest.mark.parametrize('offset',[-1,100_000_001])
def test_stale_or_future_rows_cannot_establish_discontinuity_baselines(offset):
    p,t=session()
    r=row(0,source_epoch=7,accepted_adddata_frames=clock.TOTAL_SAMPLES)
    t[0]=r['mach_absolute_ns']+offset
    result=p.ingest(json.dumps(r).encode())
    assert not result.valid and not p.retired
    assert p._epoch is None and p._counter is None
    assert p.estimator.origin_ns is None
    assert acquire(p,t,1).valid


@pytest.mark.parametrize('offset',[-1,100_000_001])
def test_stale_or_future_rows_cannot_advance_discontinuity_baselines(offset):
    p,t=session();assert acquire(p,t).valid
    previous=(p._epoch,p._counter)
    r=row(9,accepted_adddata_frames=clock.TOTAL_SAMPLES)
    t[0]=r['mach_absolute_ns']+offset
    assert not p.ingest(json.dumps(r).encode()).valid
    assert (p._epoch,p._counter)==previous
    assert acquire(p,t,10).valid and not p.retired


def test_host_override_pending_uses_existing_qos_pending_semantics():
    p,t=session()
    pending=row(0)
    pending['ownership'].update(eligible=False,reason='qos_unconfirmed')
    pending['host_override']=dict(enabled=True,active=False)
    assert not feed(p,t,pending).valid
    assert not p.retired and p.estimator.origin_ns is None
    # Only subsequent native-qualified rows can start the frozen acquisition.
    assert acquire(p,t,1).valid


def test_explicit_player_mode_binding_and_hello_mismatch():
    b=binding(player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer')
    for change in ({},dict(player_mode='paplayer_pcm_v1',requested_core='PAPlayer'),dict(player_mode='videoplayer_pcm_v1',requested_core='PAPlayer')):
        p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
        assert not p.bind_hello(hello(**change)) and p.retired
    p=clock.OwnedClockProvider(b,estimator_factory,lambda:BASE)
    assert p.bind_hello(hello(player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer'))
    for mode,core in [('unknown','VideoPlayer'),('videoplayer_pcm_v1','PAPlayer'),('paplayer_pcm_v1','VideoPlayer')]:
        with pytest.raises(ValueError):binding(player_mode=mode,requested_core=core)


def video_evidence(r):
    r['ownership'].update(player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer',
        host_override_applicable=False,host_mode_generation_before=4,host_mode_generation_after=4,
        host_mode_armed=True,host_caller_id=71,host_caller_name='VideoPlayer',host_caller_identity_status=0,
        host_caller_name_status=0,host_caller_observed_mach_ns=BASE-40)
    r['host_override']=dict(enabled=False,active=False,failed=False,starts=0,ends=0,outstanding=0)
    return r


def video_session():
    t=[BASE];p=clock.OwnedClockProvider(binding(player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer'),estimator_factory,lambda:t[0])
    assert p.bind_hello(hello(player_mode='videoplayer_pcm_v1',requested_core='VideoPlayer'))
    p.control('play',BASE-25)
    r=video_evidence(row(-1,revision=1,state='waiting',issued=BASE-100,applied=BASE-40))
    r.update(mach_absolute_ns=BASE-10,steady_ns=BASE-10,delay_query_start_steady_ns=BASE-10,delay_query_end_steady_ns=BASE-10)
    feed(p,t,r)
    return p,t


def test_video_rows_acquire_without_override_but_keep_renderer_qos():
    p,t=video_session()
    for i in range(9):result=feed(p,t,video_evidence(row(i)))
    assert result.valid and not p.retired
    r=video_evidence(row(9));r['renderer_qos_request_confirmed']=False
    assert not feed(p,t,r).valid


@pytest.mark.parametrize('damage',['missing_mode','core','caller','changed_id','missing_identity','generation','override','applicable'])
def test_video_wrong_evidence_retires(damage):
    p,t=video_session();r=video_evidence(row(0));own=r['ownership']
    if damage=='missing_mode':own.pop('player_mode')
    if damage=='core':own['requested_core']='PAPlayer'
    if damage=='caller':own['host_caller_name']='PAPlayer'
    if damage=='changed_id':own['host_caller_id']=72
    if damage=='missing_identity':own.pop('host_caller_identity_status')
    if damage=='generation':own['host_mode_generation_after']=2
    if damage=='override':r['host_override']['starts']=1
    if damage=='applicable':own['host_override_applicable']=True
    assert not feed(p,t,r).valid and p.retired


def test_video_generation_change_is_pending_then_reacquires():
    p,t=video_session();r=video_evidence(row(0));r['ownership']['host_mode_generation_before']=3
    assert not feed(p,t,r).valid and not p.retired
    for i in range(1,10):result=feed(p,t,video_evidence(row(i)))
    assert result.valid
