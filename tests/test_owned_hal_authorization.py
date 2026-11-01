import importlib.util
import pytest
import threading
import time

spec=importlib.util.find_spec('owned_hal_authorization')
if spec:import owned_hal_authorization as auth
else:auth=None


def wait_for_pending(slot, expected):
    until = time.monotonic() + 1
    while slot.pending != expected:
        assert time.monotonic() < until, f'authorization waiters did not reach {expected}'
        time.sleep(.0001)


def test_native_probe_is_required_not_cached_snapshot():
    assert auth is not None,'fresh native authorization module absent'
    r=dict(request_start_ns=100,deadline_ns=20_000_100,query_host_ns=100,
        check_start_ns=101,check_end_ns=102,callback_entry_ns=100,lease_expiry_ns=200_000_000,
        source_end_ns=0,authorized=True)
    assert auth.valid_times(r,103)
    assert not auth.valid_times(r,5_000_103)
    assert not auth.valid_times(dict(r,check_end_ns=5_000_102),5_000_103)
    assert not auth.valid_times(dict(r,lease_expiry_ns=103),103)
    assert not auth.valid_times(dict(r,check_start_ns=99),103)
    assert not auth.valid_times(dict(r,source_end_ns=103),103)


def test_fair_slot_serves_waiters_in_fifo_order():
    slot=auth.FairAuthorizationSlot();cancel=threading.Event();now=time.monotonic_ns
    assert slot.acquire(now()+1_000_000_000,now,cancel)
    entered=[threading.Event(),threading.Event()];order=[]
    def waiter(number):
        entered[number].set()
        if slot.acquire(now()+1_000_000_000,now,cancel):
            order.append(number);slot.release()
    first=threading.Thread(target=waiter,args=(0,));first.start();assert entered[0].wait(1)
    wait_for_pending(slot, 1)
    second=threading.Thread(target=waiter,args=(1,));second.start();assert entered[1].wait(1)
    wait_for_pending(slot, 2)
    slot.release();first.join(1);second.join(1)
    assert order==[0,1] and slot.pending==0


def test_invalid_authorization_directory_permissions_close_open_descriptor(tmp_path,monkeypatch):
    real_close=auth.os.close;real_open_directory=auth._open_directory;opened=[];closed=[]
    def open_directory(path):
        descriptor=real_open_directory(path);opened.append(descriptor);return descriptor
    def close(descriptor):
        if descriptor in opened:closed.append(descriptor)
        return real_close(descriptor)
    monkeypatch.setattr(auth,'_open_directory',open_directory)
    monkeypatch.setattr(auth,'_private',lambda *_args:False)
    monkeypatch.setattr(auth.os,'close',close)
    with pytest.raises(ValueError,match='authorization_permissions'):
        auth.HalAuthorizationTransport(tmp_path,time.monotonic_ns)
    assert len(opened)==1 and closed==opened
    with pytest.raises(OSError):
        auth.os.fstat(opened[0])


def test_fair_slot_removes_timeout_and_cancelled_waiters():
    slot=auth.FairAuthorizationSlot();clock=[time.monotonic_ns()];now=lambda:clock[0]
    holder_cancel=threading.Event()
    assert slot.acquire(now()+1_000_000_000,now,holder_cancel)
    timeout=[];cancelled=[];stop=threading.Event()
    timed_deadline=now()+2_000_000
    timed=threading.Thread(target=lambda:timeout.append(slot.acquire(timed_deadline,now,threading.Event())))
    blocked=threading.Thread(target=lambda:cancelled.append(slot.acquire(now()+1_000_000_000,now,stop)))
    timed.start();blocked.start()
    wait_for_pending(slot, 2)
    clock[0]=timed_deadline+1
    slot.wake()
    timed.join(1)
    stop.set();slot.wake();timed.join(1);blocked.join(1)
    assert not timed.is_alive() and timeout==[False] and cancelled==[False] and slot.pending==0
    slot.release()


def test_close_claims_slot_after_cancelling_queued_waiters(tmp_path):
    p,now,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0])
    # A waiter cannot normally remain queued without a holder, but this is the
    # close race between a waiter enqueue and its first condition recheck.
    with transport.slot._condition:
        transport.slot._waiters.append(object())
    transport.close()
    assert transport.fd is None


def test_slot_timeout_before_request_publication_does_not_retire_transport(tmp_path,monkeypatch):
    import json
    p,now,ev,_=prepared()
    transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0])
    real_acquire=transport.slot.acquire
    monkeypatch.setattr(transport.slot,'acquire',lambda *_args:False)

    assert transport.probe(p.binding,ev,p.generation,'tracker',
        lease_expiry_ns=now[0]+500_000_000) is None
    assert transport.failed is False
    assert transport.count==0
    assert not list(transport.path.glob('request-*.json'))

    monkeypatch.setattr(transport.slot,'acquire',real_acquire)
    def responder():
        path=transport.path/'request-000001.json';until=time.monotonic()+1
        while not path.exists() and time.monotonic()<until:time.sleep(.0005)
        raw=path.read_bytes();request=json.loads(raw)
        response=response_for(request,request_sha256=auth.sha(raw))
        output=transport.path/'response-000001.json'
        temporary=transport.path/'.response-000001.tmp'
        temporary.write_bytes(auth.canonical(response))
        temporary.chmod(0o600)
        temporary.rename(output)
    server=threading.Thread(target=responder);server.start()
    proof=transport.probe(p.binding,ev,p.generation,'tracker',
        lease_expiry_ns=now[0]+500_000_000)
    server.join(1)
    assert not server.is_alive()
    assert proof is not None
    assert transport.failed is False
    assert transport.count==1
    transport.close()


def test_close_racing_probe_release_closes_authorization_descriptor(tmp_path,monkeypatch):
    import json
    p,now,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0])
    release_entered=threading.Event();allow_release=threading.Event();proofs=[]
    original_release=transport.slot.release
    def paused_release(before=None):
        release_entered.set()
        assert allow_release.wait(1)
        return original_release(before)
    monkeypatch.setattr(transport.slot,'release',paused_release)
    def responder():
        path=transport.path/'request-000001.json';until=time.monotonic()+1
        while not path.exists() and time.monotonic()<until:time.sleep(.0005)
        raw=path.read_bytes();request=json.loads(raw)
        response=response_for(request,request_sha256=auth.sha(raw))
        name=transport.path/'response-000001.json';name.write_bytes(auth.canonical(response));name.chmod(0o600)
    server=threading.Thread(target=responder);server.start()
    probe=threading.Thread(target=lambda:proofs.append(transport.probe(
        p.binding,ev,p.generation,'tracker',decision_id='e'*64,
        lease_expiry_ns=now[0]+500_000_000)))
    probe.start()
    assert release_entered.wait(1)
    transport.close()
    allow_release.set()
    probe.join(1);server.join(1)
    assert not probe.is_alive() and proofs[0] is not None
    assert transport.fd is None


def prepared():
    from test_owned_hal_clock import setup,ready,query,raw
    p,now=setup();s=ready(p,now);ev=p.ingest(raw(query(10,s,qualified=True)))
    request={k:getattr(ev,k) for k in auth.FIELDS}
    request.update(request_id=1,expected_generation=p.generation,stage='tracker',request_start_ns=now[0],
        deadline_ns=now[0]+20_000_000,initial=1)
    response=dict(request,authorized=True,check_start_ns=now[0],check_end_ns=now[0],callback_entry_ns=now[0],
                  lease_expiry_ns=now[0]+500_000_000,source_end_ns=0,actual_gate=ev.native_generation,actual_intent=ev.effective_intent)
    proof=auth.Proof(tuple(request.items()),tuple(response.items()),'e'*64)
    return p,now,ev,proof


def test_proof_consumption_rechecks_generation_and_is_single_use():
    assert auth is not None
    p,now,ev,proof=prepared();called=[]
    assert hasattr(p,'consume_proof'),'fresh proof consumption absent'
    assert p.consume_proof(ev,proof,'tracker',lambda current:called.append(current.authorization_ns) or True)
    assert called==now
    assert not p.consume_proof(ev,proof,'tracker',lambda current:pytest.fail('replayed proof'))
    p,now,ev,proof=prepared();p.control('pause',now[0]+1);now[0]+=2
    assert not p.consume_proof(ev,proof,'tracker',lambda current:pytest.fail('stale generation'))


def test_stage_and_late_action_cannot_reuse_tracker_grant():
    p,now,ev,proof=prepared()
    assert hasattr(p,'consume_proof'),'fresh proof consumption absent'
    assert not p.consume_proof(ev,proof,'queue',lambda current:pytest.fail('wrong stage'))
    p,now,ev,proof=prepared();now[0]+=5_000_001
    assert not p.consume_proof(ev,proof,'tracker',lambda current:pytest.fail('late reply consumption'))


def response_for(request,**changes):
    r=dict(request,request_sha256='',authorized=True,reason='authorized',check_start_ns=request['request_start_ns'],
        check_end_ns=request['request_start_ns'],actual_gate=request['native_generation'],actual_intent=request['effective_intent'],
        applied_sequence=21,applied_sha256='c'*64,callback_entry_ns=request['request_start_ns'],
        lease_expiry_ns=request['request_start_ns']+500_000_000,source_end_ns=0)
    r.update(changes);return r


def test_private_file_exchange_echoes_bytes_and_has_no_directory_scan(tmp_path,monkeypatch):
    import os,threading,time,json
    p,now,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0])
    monkeypatch.setattr(os,'listdir',lambda *a:pytest.fail('authorization history scan'))
    monkeypatch.setattr(os,'scandir',lambda *a:pytest.fail('authorization history scan'))
    result=[]
    def responder():
        path=transport.path/'request-000001.json'
        until=time.monotonic()+1
        while not path.exists() and time.monotonic()<until:time.sleep(.0005)
        raw=path.read_bytes();request=json.loads(raw);response=response_for(request,request_sha256=auth.sha(raw))
        name=transport.path/'response-000001.json';name.write_bytes(auth.canonical(response));name.chmod(0o600)
    worker=threading.Thread(target=responder);worker.start()
    proof=transport.probe(p.binding,ev,p.generation,'tracker',decision_id='e'*64,lease_expiry_ns=now[0]+500_000_000)
    worker.join();assert proof and proof.response['authorized'] and not transport.failed
    assert proof.request['row_sha256']==ev.row_sha256
    assert len(auth.canonical(proof.response))<=4096
    transport.close()


@pytest.mark.parametrize('fault',['symlink','oversize','wrong_hash','wrong_stage','future','late'])
def test_bad_response_retires_without_retry(tmp_path,fault):
    import os,threading,time,json
    p,now,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0])
    def responder():
        path=transport.path/'request-000001.json';until=time.monotonic()+1
        while not path.exists() and time.monotonic()<until:time.sleep(.0005)
        raw=path.read_bytes();request=json.loads(raw);response=response_for(request,request_sha256=auth.sha(raw))
        if fault=='wrong_hash':response['request_sha256']='0'*64
        if fault=='wrong_stage':response['stage']='queue'
        if fault=='future':response['check_start_ns']+=1;response['check_end_ns']+=1
        if fault=='late':now[0]+=20_000_001
        name=transport.path/'response-000001.json';temp=transport.path/'.reply'
        temp.write_bytes(b'x'*4097 if fault=='oversize' else auth.canonical(response));temp.chmod(0o600)
        if fault=='symlink':name.symlink_to(temp)
        else:temp.rename(name)
    worker=threading.Thread(target=responder);worker.start()
    proof=transport.probe(p.binding,ev,p.generation,'tracker',decision_id='e'*64,lease_expiry_ns=now[0]+500_000_000)
    worker.join();assert proof is None and transport.failed
    assert transport.probe(p.binding,ev,p.generation,'tracker',decision_id='e'*64,lease_expiry_ns=now[0]+500_000_000) is None
    assert transport.count==1;transport.close()


def test_cancel_wait_does_not_block_control_or_close_live_descriptor(tmp_path):
    import threading,time
    p,now,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,lambda:now[0]);p.authorization=transport
    result=[]
    worker=threading.Thread(target=lambda:result.append(p.probe(ev,'tracker',decision_id='e'*64)))
    worker.start();until=time.monotonic()+1
    while transport.count==0 and time.monotonic()<until:time.sleep(.0005)
    assert transport.count==1 and worker.is_alive()
    control_done=threading.Event();control_errors=[]
    def control():
        try:p.control('pause',now[0]+1)
        except BaseException as exc:control_errors.append(exc)
        finally:control_done.set()
    controller=threading.Thread(target=control)
    controller.start()
    try:
        assert control_done.wait(1),'control blocked on the pending authorization probe'
        controller.join()
        assert not control_errors
        assert worker.is_alive()
    finally:
        transport.close();controller.join(1);worker.join(1)
    assert not controller.is_alive() and not worker.is_alive() and result==[None] and transport.fd is None


@pytest.mark.parametrize('mode',['stages','pause','route','aba','intent'])
def test_actual_native_file_protocol_rechecks_unseen_fences(tmp_path,mode):
    import dataclasses,os,subprocess,time
    from owned_clock import mach_host_ns
    from test_owned_hal_clock import segment,query,raw,digest
    executable=os.environ.get('HAL_AUTH_HARNESS')
    if not executable:pytest.skip('set HAL_AUTH_HARNESS; see docs/patches/owned-hls-lab/README.md (Owned HAL component harness)')
    p,_,ev,_=prepared();transport=auth.HalAuthorizationTransport(tmp_path,mach_host_ns)
    native=subprocess.Popen([executable,str(tmp_path),mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    until=time.monotonic()+1
    while not (tmp_path/'started').exists() and native.poll() is None and time.monotonic()<until:time.sleep(.001)
    assert (tmp_path/'started').exists()
    q=query(10,segment(20,True),qualified=True);delta=mach_host_ns()-q['query_host_ns']
    for key in ('query_start_ns','query_end_ns','query_host_ns','source_zero_ticks','initial_deadline_ns',
                'applied_request_start_ns','applied_request_end_ns','applied_issue_ns','applied_expiry_ns'):q[key]+=delta
    wire=raw(q);(tmp_path/'.query.tmp').write_bytes(wire);(tmp_path/'.query.tmp').rename(tmp_path/'query.json')
    ev=dataclasses.replace(ev,query_start_ns=q['query_start_ns'],query_end_ns=q['query_end_ns'],query_host_ns=q['query_host_ns'],
        row_sha256=auth.sha(wire),source_zero_ticks=q['source_zero_ticks'],initial_deadline_ns=q['initial_deadline_ns'])
    try:
        until=time.monotonic()+1
        while not (tmp_path/'ready').exists() and native.poll() is None and time.monotonic()<until:time.sleep(.001)
        assert (tmp_path/'ready').exists(),native.communicate(timeout=1)
        first=transport.probe(p.binding,ev,p.generation,'tracker',initial=True,decision_id='e'*64,lease_expiry_ns=ev.query_host_ns+500_000_000)
        assert first is not None, (mode,transport.failed)
        assert first.response['authorized']==(mode=='stages')
        if mode=='stages':
            decision=mach_host_ns();assert auth.valid_times(first.response,decision)
            second=transport.probe(p.binding,ev,p.generation,'queue',initial=True,decision_id='e'*64,event_digest='f'*64,
                parent=first,decision_ns=decision,lease_expiry_ns=ev.query_host_ns+500_000_000)
            assert second and second.response['authorized']
            queued=mach_host_ns();assert auth.valid_times(second.response,queued)
            third=transport.probe(p.binding,ev,p.generation,'dispatch',initial=True,decision_id='e'*64,event_digest='f'*64,
                parent=second,decision_ns=decision,queue_ns=queued,lease_expiry_ns=ev.query_host_ns+500_000_000)
            assert third and third.response['authorized']
        stdout,stderr=native.communicate(timeout=2);assert native.returncode==0,(native.returncode,stdout,stderr)
    finally:
        transport.close()
        if native.poll() is None:native.terminate();native.wait(timeout=2)
