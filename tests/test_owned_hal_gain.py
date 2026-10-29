"""Controls phase has no clock authority and never trusts notification payloads."""
import dataclasses
import importlib.util
import json
import os
import threading
import time

import pytest
import owned_clock as clock

spec = importlib.util.find_spec('owned_hal_gain')
hal = None
if spec:
    import owned_hal_gain as hal

BASE = 1_000_000_000


def implementation():
    assert hal is not None, 'HAL controls bridge is not implemented'
    return hal


def binding():
    implementation()
    return clock.OwnedClockBinding('a'*32, 'b'*32, 'c'*64, 'd'*64, clock.URL,
        BASE-100, os.getpid(), player_mode='videoplayer_video_v1', requested_core='VideoPlayer',
        video_queue_tenths=40, video_extradata_hex='00', output_backend='hal_callback_v1',
        hal_contract_version=1, device_uid='BuiltInSpeakerDevice',
        device_format={'rate':48000,'frames':512,'channels':2,'buffers':1,'format':'float32_interleaved'},
        lifecycle_capability='retained_exposed_context_until_process_exit')


class Transport:
    def __init__(self):
        self.binding = binding()
        self.device_generation = 1
        self.commands = []
        self.retired = False
    def write_gain(self, command):
        self.commands.append(command)
    def retire(self, reason):
        self.retired = True


def controller(volume=100, muted=False, width=0):
    implementation()
    now=[BASE]; state={'volume':volume,'muted':muted}; calls=[]; t=Transport()
    def rpc(method, params):
        assert method=='Application.GetProperties' and params=={'properties':['volume','muted']}
        calls.append(now[0]); now[0]+=width
        return dict(state)
    c=hal.HalGainController(rpc,t,lambda:now[0])
    return c,t,now,state,calls


def test_missing_bridge_is_explicit():
    implementation()


def test_exact_curve_and_rounded_zero():
    h=implementation()
    for volume,expected in ((0,0),(1,10**(-59.4/20)),(75,10**(-15/20)),(90,10**(-6/20)),(100,1)):
        assert h.kodi_gain(volume,False)==pytest.approx(expected)
        assert h.kodi_gain(volume,True)==0
    for invalid in (True,-1,101,0.5,'90'):
        with pytest.raises(ValueError):h.kodi_gain(invalid,False)


def test_authoritative_query_bracket_refresh_revision_and_effective_epoch():
    c,t,now,state,calls=controller(width=50_000_000)
    first=c.reconcile()
    assert first.request_start_ns==BASE and first.request_end_ns==BASE+50_000_000
    assert first.expiry_ns==BASE+500_000_000 and first.issue_ns==now[0]
    assert first.revision==first.effective_epoch==1 and first.volume==100
    now[0]=BASE+100_000_000
    second=c.reconcile()
    assert second.revision==2 and second.effective_epoch==1
    assert second.expiry_ns==BASE+600_000_000
    state['volume']=75; now[0]=BASE+200_000_000
    third=c.reconcile()
    assert third.revision==3 and third.effective_epoch==2
    assert t.commands==[first,second,third]


@pytest.mark.parametrize('width',[50_000_001,100_000_000,500_000_000])
def test_late_queries_retire_without_publication(width):
    c,t,now,state,calls=controller(width=width)
    assert c.reconcile() is None and c.retired and t.retired and not t.commands
    now[0]+=1
    assert c.reconcile() is None and len(calls)==1


@pytest.mark.parametrize('value',[dict(volume=True,muted=False),dict(volume=90,muted=1),dict(volume=101,muted=False),dict(volume=75.5,muted=False),dict(volume=90),None])
def test_bad_readback_is_terminal(value):
    h=implementation(); t=Transport()
    c=h.HalGainController(lambda *args:value,t,lambda:BASE)
    assert c.reconcile() is None and c.retired and not t.commands


def test_two_delayed_notification_queues_and_coalesced_opposing_commands():
    c,t,now,state,calls=controller()
    c.reconcile()
    # Announcement and Python queues contain obsolete payloads; they are hints only.
    announcements=[('xbmc','Application.OnVolumeChanged','{"volume":90,"muted":false}'),
                   ('xbmc','Application.OnVolumeChanged','{"volume":75,"muted":false}')]
    python_queue=list(announcements)
    state['volume']=100; now[0]+=20_000_000
    for hint in announcements:c.notify(*hint)
    assert len(t.commands)==1
    assert c.reconcile().volume==100
    state['volume']=75;now[0]+=20_000_000;c.notify(*announcements[1]);latest=c.reconcile()
    assert latest.volume==75
    for hint in python_queue:c.notify(*hint)
    now[0]+=20_000_000
    assert c.reconcile().volume==75
    assert [v.volume for v in t.commands]==[100,100,75,75]


def test_notifications_during_read_schedule_followup_without_parallel_publication():
    h=implementation();t=Transport();now=[BASE];entered=threading.Event();release=threading.Event();calls=[]
    def rpc(*args):
        calls.append(len(calls)); entered.set();release.wait(1)
        return {'volume':100 if len(calls)==1 else 75,'muted':False}
    c=h.HalGainController(rpc,t,lambda:now[0])
    worker=threading.Thread(target=c.reconcile);worker.start();assert entered.wait(1)
    c.notify('xbmc','Application.OnVolumeChanged','{}')
    release.set();worker.join(1);assert not worker.is_alive()
    now[0]+=1
    assert c.reconcile().volume==75
    assert [v.revision for v in t.commands]==[1,2]


def test_nominal_cadence_and_close_cannot_renew():
    c,t,now,state,calls=controller();c.reconcile()
    now[0]+=99_999_999;assert c.reconcile() is None
    now[0]+=1;assert c.reconcile().revision==2
    c.close();now[0]+=100_000_000
    assert c.reconcile() is None and len(t.commands)==2


def test_binding_backend_mismatch_is_rejected_and_ae_provider_cannot_accept_hal():
    h=implementation();b=binding()
    with pytest.raises(ValueError):dataclasses.replace(b,output_backend='ae')
    with pytest.raises(ValueError):dataclasses.replace(b,device_format=dict(b.device_format,frames=1024))
    with pytest.raises(ValueError):clock.OwnedClockProvider(b,lambda:pytest.fail('must not build estimator'))
    session=h.HalControlSession(b,lambda:BASE)
    assert not hasattr(session,'snapshot') and not hasattr(session,'authorize')
    assert not session.bind_hello({'output_backend':'ae'}) and session.retired


def test_transport_gain_namespace_exclusive_sequential_and_bound(tmp_path):
    h=implementation();b=binding();t=clock.OwnedClockTransport.create(tmp_path,b)
    t.binding=b;t.device_generation=1
    c=h.HalGainController(lambda *args:dict(volume=90,muted=False),t,lambda:BASE)
    first=c.reconcile()
    assert not c.retired and first
    raw=(t.directory/'gain-000001.json').read_bytes()
    assert json.loads(raw)['output_backend']=='hal_callback_v1'
    assert json.loads(raw)['revision']==1
    with pytest.raises(clock.OwnedTransportError):t.write_gain(first)
    assert not list(t.directory.glob('command-*'))
    t.close()


def test_monitor_controls_phase_cannot_construct_beacon_sender(tmp_path,monkeypatch):
    import importlib.util
    import pathlib
    import sys
    import types
    import owned_ad_playback
    implementation()
    lab=pathlib.Path(__file__).resolve().parents[1]/'docs/patches/owned-hls-lab'
    sys.path.insert(0,str(lab))
    import fixture_beacons as fixture
    constructed=[]
    class Monitor:
        def __init__(self):constructed.append('monitor')
        def waitForAbort(self,wait):return True
    class Player:pass
    fake=types.SimpleNamespace(Monitor=Monitor,Player=Player)
    monkeypatch.setitem(sys.modules,'xbmc',fake)
    monkeypatch.setattr(owned_ad_playback,'OwnedAdPlayback',lambda *a,**kw:pytest.fail('beacon sender constructed'))
    monkeypatch.setattr(fixture,'http_client',lambda *a,**kw:pytest.fail('HTTP sender constructed'))
    module_spec=importlib.util.spec_from_file_location('hal_monitor',lab/'monitor.py')
    module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
    assert hasattr(module,'run_hal_controls'), 'dedicated HAL controls monitor is missing'
    t=clock.OwnedClockTransport.create(tmp_path,binding())
    module.run_hal_controls(t,binding(),fixture)
    assert constructed==['monitor']
    rows=[json.loads(v) for v in (t.directory/'owned-events.jsonl').read_text().splitlines()]
    assert rows[0]['beacons_enabled'] is False and rows[0]['clock_authority'] is False


def hal_hello(b):
    values=dataclasses.asdict(b)
    return {k:values[k] for k in ('nonce','incarnation','timeline_sha256','first_packet_sha256','native_pid','player_mode','requested_core','video_queue_tenths','video_extradata_hex','output_backend','hal_contract_version','device_uid','device_format','lifecycle_capability')} | dict(protocol=1,stream_generation=1,device_generation=1,native_open_mach_ns=BASE-50,qualification_scope='controlled_fixture')


def gain_ack(command, **changes):
    row=dict(kind='hal_gain_ack',nonce=command.nonce,incarnation=command.incarnation,
        timeline_sha256=command.timeline_sha256,output_backend=command.output_backend,
        hal_contract_version=1,device_generation=1,gain_revision=command.revision,
        gain_epoch=command.effective_epoch,volume=command.volume,muted=command.muted,
        applied_gain=command.multiplier,output_host_ns=command.issue_ns,source_start=0,
        request_start_ns=command.request_start_ns,request_end_ns=command.request_end_ns,
        issue_ns=command.issue_ns,expiry_ns=command.expiry_ns,in_flight_generation=1,
        playback_revision=1,playback_epoch=1,segment_sequence=command.revision,
        publication_sequence=1,applied_intent=2,native_gate=1,authorized_native_epoch=0,
        resume_playback_revision=0,resume_granted=False)
    return row | changes


def terminal_row(**changes):
    b=binding()
    return dict(kind='hal_terminal',output_backend=b.output_backend,nonce=b.nonce,
        incarnation=b.incarnation,timeline_sha256=b.timeline_sha256,reason='natural_eof',
        source_end_ns=BASE,hold_until_ns=BASE+1_000_000_000,observed_mach_ns=BASE,
        native_control_reason='eligible',core_failure=0,failed=False,cleanup_result=-1,
        exposed_context_retained_until_exit=True,quarantined=False,
        clock_authority=False,beacons_enabled=False) | changes


def test_natural_terminal_does_not_cancel_native_one_second_hold():
    h=implementation();b=binding();session=h.HalControlSession(b,lambda:BASE)
    assert session.bind_hello(hal_hello(b))
    raw=json.dumps(terminal_row()).encode()
    result=session.ingest(raw)
    assert result and not session.retired and session.source_ended
    assert result['raw_sha256']==__import__('hashlib').sha256(raw).hexdigest()


def test_gain_ack_newer_ack_cannot_be_followed_by_stale_value():
    c,t,now,state,calls=controller();first=c.reconcile();state['volume']=75;now[0]+=100_000_000;second=c.reconcile()
    assert c.acknowledge(gain_ack(second))
    assert not c.acknowledge(gain_ack(first)) and c.retired


def test_initial_issue_request_start_100ms_boundary():
    h=implementation()
    for age,valid in ((100_000_000,True),(100_000_001,False)):
        readings=iter((BASE,BASE+50_000_000,BASE+age));t=Transport()
        c=h.HalGainController(lambda *a:dict(volume=100,muted=False),t,lambda:next(readings))
        assert bool(c.reconcile()) is valid


def test_hal_session_rejects_coerced_record_numbers_and_flags():
    h=implementation();b=binding()
    integer_fields=('sequence','schema_version','hal_contract_version','device_generation','stream_generation',
        'in_flight_generation','playback_revision','playback_epoch','gain_revision','gain_epoch','source_start','source_count',
        'requested_frames','consumed_frames','copied_frames','zero_frames','output_flags','now_flags','output_ticks',
        'now_ticks','entry_ticks','exit_ticks','timebase_numer','timebase_denom','output_host_ns','source_end_ns','reason','publication_sequence','applied_intent','desired_intent',
        'native_gate','authorized_native_epoch','resume_playback_revision','request_start_ns','request_end_ns',
        'issue_ns','expiry_ns','volume')
    base=dict.fromkeys(integer_fields,0)
    base.update(kind='hal_segment',output_backend=b.output_backend,nonce=b.nonce,incarnation=b.incarnation,
        timeline_sha256=b.timeline_sha256,sequence=1,schema_version=1,hal_contract_version=1,requested_frames=512,
        zero_frames=512,applied_gain=0.0,output_sample_time=0.0,muted=False,playing=False,terminal=False,
        control_acquired=False,clock_authority=False,beacons_enabled=False,requested_playing=False,
        resume_granted=False,started=False,sample_zero_proven=False)
    for field,value in (('device_generation',True),('sequence',1.0),('source_count',False),('muted',0),('playing',1),('applied_gain',True)):
        p=h.HalControlSession(b,lambda:BASE);assert p.bind_hello(hal_hello(b))
        assert p.ingest(json.dumps(dict(base,**{field:value})).encode()) is None and p.retired


def test_monitor_real_callback_ack_and_gain_read_order(tmp_path,monkeypatch):
    import pathlib,sys,types
    lab=pathlib.Path(__file__).resolve().parents[1]/'docs/patches/owned-hls-lab'
    sys.path.insert(0,str(lab))
    import fixture_beacons as fixture
    events=[];players=[];t=clock.OwnedClockTransport.create(tmp_path,binding())
    class Monitor:
        def __init__(self):events.append('monitor_registered');self.waits=0
        def waitForAbort(self,wait):
            self.waits+=1
            if self.waits==1:
                hello=hal_hello(binding());hello['native_open_mach_ns']=BASE-50
                for name,raw in [('hello.json',json.dumps(hello)),('native.jsonl','')]:
                    path=t.directory/name;path.write_text(raw);path.chmod(0o600)
                players[0].onAVStarted()
                return False
            return True
    class Player:
        def __init__(self):players.append(self)
        def getPlayingFile(self):return clock.URL
    def rpc(raw):
        request=json.loads(raw);events.append(request['method'])
        assert request['method']=='Application.GetProperties'
        return json.dumps({'result':{'volume':75,'muted':False}})
    monkeypatch.setitem(sys.modules,'xbmc',types.SimpleNamespace(Monitor=Monitor,Player=Player,executeJSONRPC=rpc))
    spec=importlib.util.spec_from_file_location('hal_monitor_callbacks',lab/'monitor.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.run_hal_controls(t,binding(),fixture)
    assert events==['monitor_registered','Application.GetProperties']
    commands=[p.read_text().split()[4] for p in sorted(t.directory.glob('command-*'))]
    assert commands==['waiting','play','stop']
    assert json.loads((t.directory/'gain-000001.json').read_text())['volume']==75


def test_muted_volume_refresh_does_not_change_effective_gain_epoch():
    c,t,now,state,calls=controller(volume=100,muted=True);first=c.reconcile()
    state['volume']=75;now[0]+=100_000_000;second=c.reconcile()
    assert second.revision==2 and second.effective_epoch==first.effective_epoch
    state['muted']=False;now[0]+=100_000_000;third=c.reconcile()
    assert third.effective_epoch==first.effective_epoch+1


def test_gain_ack_request_brackets_cannot_be_coerced():
    c,t,now,state,calls=controller();command=c.reconcile()
    row=gain_ack(command,request_start_ns=float(command.request_start_ns))
    assert not c.acknowledge(row) and c.retired


def test_monitor_import_has_no_legacy_session_side_effects(tmp_path,monkeypatch):
    import pathlib,sys,types
    original={'callbacks.jsonl':b'original callbacks\n','runtime-clock.jsonl':b'original runtime\n','monitor-stop':b'1'}
    for name,raw in original.items():(tmp_path/name).write_bytes(raw)
    def unexpected(*args,**kwargs):pytest.fail('legacy Kodi object created during import')
    monkeypatch.setitem(sys.modules,'xbmc',types.SimpleNamespace(Player=unexpected,Monitor=unexpected))
    real_path=pathlib.Path
    proxy=types.ModuleType('pathlib')
    proxy.Path=lambda value:tmp_path if str(value)=='/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer' else real_path(value)
    monkeypatch.setitem(sys.modules,'pathlib',proxy)
    path=real_path(__file__).resolve().parents[1]/'docs/patches/owned-hls-lab/monitor.py'
    spec=importlib.util.spec_from_file_location('hal_monitor_import_safety',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()}==original


@pytest.mark.parametrize('field,value',[('buffers',True),('rate',48000.0),('frames',512.0)])
def test_hello_nested_format_types_are_strict(field,value):
    h=implementation();b=binding();hello=hal_hello(b);hello['device_format']=dict(hello['device_format'],**{field:value})
    p=h.HalControlSession(b,lambda:BASE)
    assert not p.bind_hello(hello) and p.retired


# These mutations must fail before either consumer advances its accepted state.
ACK_FIELDS=('kind','nonce','incarnation','timeline_sha256','output_backend','hal_contract_version',
    'device_generation','gain_revision','gain_epoch','volume','muted','applied_gain','output_host_ns',
    'source_start','request_start_ns','request_end_ns','issue_ns','expiry_ns','in_flight_generation',
    'playback_revision','playback_epoch','segment_sequence','publication_sequence','applied_intent',
    'native_gate','authorized_native_epoch','resume_playback_revision','resume_granted')


@pytest.mark.parametrize('field',ACK_FIELDS)
@pytest.mark.parametrize('mutation',['missing','coerced'])
@pytest.mark.parametrize('consumer',['controller','session'])
def test_complete_ack_schema_rejects_each_missing_or_coerced_field(field,mutation,consumer):
    c,t,now,state,calls=controller();command=c.reconcile();row=gain_ack(command)
    if mutation=='missing':del row[field]
    else:
        value=row[field]
        row[field]=float(value) if type(value) is int else (0 if type(value) is bool else None)
    if consumer=='controller':
        assert not c.acknowledge(row) and c.retired and c._ack==0
    else:
        session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
        assert session.ingest(json.dumps(row).encode()) is None and session.retired
        assert session._ack==0 and not session.source_ended


@pytest.mark.parametrize('changes',[
    dict(publication_sequence=0),dict(applied_intent=0),dict(native_gate=0),
    dict(native_gate=True),dict(native_gate=1<<64),dict(native_gate=(1<<63)|1),
    dict(authorized_native_epoch=-1),dict(authorized_native_epoch=1<<62),
    dict(resume_playback_revision=2),dict(resume_granted='false'),
    dict(resume_granted=True),dict(in_flight_generation=2),dict(core_failure=0,clock_authority=True),
    dict(beacons_enabled=True),dict(clock_authority=0),dict(volume=101),
    dict(applied_gain=float('inf')),dict(request_end_ns=BASE+50_000_001),
    dict(issue_ns=BASE-1),dict(expiry_ns=BASE+500_000_001),dict(output_host_ns=BASE-1),
])
def test_ack_invalid_ranges_or_grant_provenance_never_advance(changes):
    c,t,now,state,calls=controller();row=gain_ack(c.reconcile(),**changes)
    assert not c.acknowledge(row) and c.retired and c._ack==0
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    # Nonfinite JSON is independently rejected by the production decoder.
    if changes.get('applied_gain')==float('inf'):
        with pytest.raises(ValueError):session.ingest(json.dumps(row).encode())
    else:assert session.ingest(json.dumps(row).encode()) is None and session.retired


@pytest.mark.parametrize('changes',[
    {}, # bootstrap has no resume grant
    dict(native_gate=(1<<62)|2,in_flight_generation=(1<<62)|2), # pause before playback grant
    dict(authorized_native_epoch=1,resume_playback_revision=1,resume_granted=True),
    dict(native_gate=(1<<62)|2,in_flight_generation=(1<<62)|2,
         authorized_native_epoch=1,resume_playback_revision=1), # native pause override
    dict(native_gate=3,in_flight_generation=3,authorized_native_epoch=1,resume_playback_revision=1),
    dict(native_gate=(1<<62)|2,in_flight_generation=(1<<62)|2,
         authorized_native_epoch=3,resume_playback_revision=1), # deferred next unpause
])
def test_ack_valid_bootstrap_paused_and_native_override_provenance(changes):
    c,t,now,state,calls=controller();row=gain_ack(c.reconcile(),**changes)
    assert c.acknowledge(row) and c._ack==1 and not c.retired
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    assert session.ingest(json.dumps(row).encode()) and not session.retired


@pytest.mark.parametrize('field',tuple(terminal_row()))
@pytest.mark.parametrize('mutation',['missing','coerced'])
def test_terminal_schema_rejects_each_missing_or_coerced_field_before_eof(field,mutation):
    row=terminal_row()
    if mutation=='missing':del row[field]
    else:
        value=row[field];row[field]=float(value) if type(value) is int else (0 if type(value) is bool else None)
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    assert session.ingest(json.dumps(row).encode()) is None and session.retired and not session.source_ended


@pytest.mark.parametrize('changes',[
    dict(reason='other'),dict(source_end_ns=-1),dict(source_end_ns=0),dict(hold_until_ns=BASE),
    dict(observed_mach_ns=BASE-1),dict(core_failure=11),dict(core_failure=1),dict(failed=True),
    dict(cleanup_result=0),dict(exposed_context_retained_until_exit=False),dict(clock_authority=True),
    dict(beacons_enabled=True),dict(native_control_reason=''),dict(observed_mach_ns=1<<64),
    dict(reason='cleanup_clean',cleanup_result=1),dict(reason='cleanup_failed',cleanup_result=0),
    dict(reason='cleanup_failed',cleanup_result=2,failed=False),
])
def test_terminal_invalid_ranges_or_authority_cannot_declare_source_end(changes):
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    assert session.ingest(json.dumps(terminal_row(**changes)).encode()) is None
    assert session.retired and not session.source_ended


@pytest.mark.parametrize('changes',[
    dict(reason='retired',source_end_ns=0,hold_until_ns=0,core_failure=6),
    dict(reason='cleanup_clean',cleanup_result=0,core_failure=8,quarantined=True,
         observed_mach_ns=BASE+1_000_000_000), # cleanup revocation after natural END
    dict(reason='cleanup_clean',source_end_ns=0,hold_until_ns=0,cleanup_result=0,
         core_failure=6,failed=True,quarantined=True),
    dict(reason='cleanup_failed',cleanup_result=1,failed=True,quarantined=True),
    dict(reason='cleanup_failed',cleanup_result=2,failed=True,quarantined=True),
])
def test_terminal_valid_retirement_and_eof_cleanup_are_not_schema_failures(changes):
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    if changes.get('source_end_ns',BASE):
        assert session.ingest(json.dumps(terminal_row()).encode()) and session.source_ended and not session.retired
    assert session.ingest(json.dumps(terminal_row(**changes)).encode())
    assert session.retired and session.reason=='native_terminal'


@pytest.mark.parametrize('field',[
    'publication_sequence','applied_intent','native_gate','authorized_native_epoch','resume_playback_revision',
])
@pytest.mark.parametrize('value',[True,False,-1,None,'bad',1<<64])
def test_new_ack_provenance_requires_exact_unsigned_integer_fields(field,value):
    c,t,now,state,calls=controller();row=gain_ack(c.reconcile(),**{field:value})
    assert not c.acknowledge(row) and c.retired and c._ack==0
    session=hal.HalControlSession(binding(),lambda:BASE);assert session.bind_hello(hal_hello(binding()))
    assert session.ingest(json.dumps(row).encode()) is None and session.retired
