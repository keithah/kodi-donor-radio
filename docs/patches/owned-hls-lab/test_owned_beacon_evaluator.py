import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import evaluate_owned_beacons as e

class OwnedEvaluatorTests(unittest.TestCase):
    def hal_chain(self):
        rows=[];proofs=[]
        def add(number,stage,decision='d'*64,event='',parent=(0,''),decision_ns=0,queue_ns=0):
            digest=('%064x'%number);request=('%064x'%(number+100))
            response=dict(request_id=number,stage=stage,request_sha256=request,decision_id=decision,event_digest=event,
                          parent_id=parent[0],parent_sha256=parent[1],decision_ns=decision_ns,queue_ns=queue_ns)
            rows.append(dict(kind='owned_hal_probe',request_sha256=request,response_sha256=digest,response=response))
            return number,digest,response
        tracker=add(1,'tracker');queue=add(2,'queue',event='e'*64,parent=tracker[:2],decision_ns=10);dispatch=add(3,'dispatch',event='e'*64,parent=queue[:2],decision_ns=10,queue_ns=20)
        rows.extend([
            dict(kind='owned_hal_tracker_authorization',decision_id='d'*64,proof_id=1,proof_sha256=tracker[1]),
            dict(kind='owned_beacon_queue',decision_id='d'*64,placement_id='p',event='start',event_digest='e'*64,decision_ns=10,proof_id=2,proof_sha256=queue[1]),
            dict(kind='owned_beacon_authorization',authorization_stage='dispatch',decision_id='d'*64,placement_id='p',event='start',event_digest='e'*64,decision_ns=10,queue_ns=20,proof_id=3,proof_sha256=dispatch[1]),
        ])
        return rows

    def test_hal_authorization_chain_requires_each_native_stage_and_parent(self):
        for mutation,expected in (('missing','hal_stage_native_proof'),('replay','hal_native_proof_schema'),('parent','hal_stage_parent_chain'),('reply','hal_native_request_reply_binding'),('authorized','hal_stage_authorization_binding')):
            rows=self.hal_chain()
            if mutation=='missing':rows=[r for r in rows if not (r.get('kind')=='owned_hal_probe' and r['response']['request_id']==2)]
            if mutation=='replay':rows.append(dict(rows[0]))
            if mutation=='parent':next(r for r in rows if r.get('kind')=='owned_hal_probe' and r['response']['request_id']==3)['response']['parent_sha256']='0'*64
            if mutation=='reply':rows[0]['response']['request_sha256']='0'*64
            if mutation=='authorized':next(r for r in rows if r.get('kind')=='owned_hal_tracker_authorization')['authorized']=False
            errors=[];e.audit_hal_authorization_chain(rows,errors)
            with self.subTest(mutation=mutation):self.assertIn(expected,errors)

    def evidence(self):
        nonce='a'*32;sid=hashlib.sha256(nonce.encode()).hexdigest()[:16]
        native=dict(kind='clock_sample',mach_absolute_ns=1000000000,steady_ns=1000000000,
                    delay_query_start_steady_ns=1000000000,delay_query_end_steady_ns=1000000000,
                    ownership=dict(observation_sequence=1,nonce=nonce,incarnation='b'*32,timeline_sha256='c'*64,
                                   eligible=True,source_zero_confirmed=True,state_generation_before=3,state_generation_after=3))
        raw=json.dumps(native).encode();digest=hashlib.sha256(raw).hexdigest()
        snap=dict(kind='owned_clock_snapshot',valid=True,seconds=16.384,query_host_ns=1000000000,
                  row_sequence=1,row_sha256=digest,generation=1,native_generation=3,observed=True,authorization_ns=1005000000)
        launch=dict(kind='owned_launch',session_id=sid,timeline_sha256='c'*64,first_packet_sha256=e.FIRST_PACKET,
                    selected_url=e.URL,estimator_sha256=e.ESTIMATOR_SHA,production_eligible=False,launch_mach_ns=1)
        details=dict(row_sequence=1,row_sha256=digest,generation=1,native_generation=3,query_host_ns=1000000000,
                     mapped_seconds=16.384,decision_ns=1010000000,session_id=sid,placement_id=e.opaque('a'),event='start')
        decision=dict(details,kind='ad_tracking',reason='queued',creative_id=e.opaque('a'))
        auth=dict(details,kind='owned_beacon_authorization',authorized=True,authorization_ns=1011000000,threshold_seconds=16.384)
        receipt=dict(kind='owned_http_receipt',session_id=sid,known_launch=True,creative='a',event='start',accepted=True,
                     duplicate=False,status=204,method='GET',receipt_mach_ns=1012000000)
        record=dict(kind='owned_clock_input',row_sequence=1,row_sha256=digest,received_mach_ns=1002000000,
                    snapshot={k:v for k,v in snap.items() if k not in ('kind','observed')})
        return [raw],[launch,snap,record,decision,auth],[receipt]
    def test_actual_row_identity_and_receipt_join(self):
        raw,rows,receipts=self.evidence()
        result=e.join_evidence(raw,rows,receipts)
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['errors'],[])
    def test_changed_native_hash_rejected(self):
        raw,rows,receipts=self.evidence();rows[1]['row_sha256']='0'*64
        self.assertIn('snapshot_native_identity',e.join_evidence(raw,rows,receipts)['errors'])
    def test_duplicate_unknown_and_missing_receipts_fail(self):
        for alteration in ('duplicate','unknown','missing'):
            raw,rows,receipts=self.evidence()
            if alteration=='duplicate':receipts.append(dict(receipts[0],accepted=False,duplicate=True,status=409))
            if alteration=='unknown':receipts.append(dict(receipts[0],event='unknown',accepted=False,status=400))
            if alteration=='missing':receipts=[]
            self.assertTrue(e.join_evidence(raw,rows,receipts)['errors'])
    def test_wrong_auth_generation_and_future_authorization_fail(self):
        for changed in ({'generation':2},{'authorization_ns':1200000000},{'authorized':False}):
            raw,rows,receipts=self.evidence();rows[-1].update(changed)
            self.assertTrue(e.join_evidence(raw,rows,receipts)['errors'])
    def test_missing_contemporaneous_input_and_stale_publication_fail(self):
        raw,rows,receipts=self.evidence()
        missing=[r for r in rows if r['kind']!='owned_clock_input']
        self.assertIn('snapshot_contemporaneous_input',e.join_evidence(raw,missing,receipts)['errors'])
        rows[2]['received_mach_ns']=1200000000
        self.assertIn('snapshot_freshness',e.join_evidence(raw,rows,receipts)['errors'])
    def test_qualified_input_cannot_be_omitted_from_snapshot_audit(self):
        raw,rows,receipts=self.evidence();rows=[r for r in rows if r['kind']!='owned_clock_snapshot']
        self.assertIn('qualified_input_snapshot_missing',e.join_evidence(raw,rows,receipts)['errors'])
    def append_native(self,raw,rows,sequence,terminal=False,consume=False,query=None):
        row=json.loads(raw[0]);query=query if query is not None else 1000000000+sequence*20000000
        row.update(mach_absolute_ns=query,steady_ns=query,delay_query_start_steady_ns=query,
                   delay_query_end_steady_ns=query,control_state='drained' if terminal else 'play',
                   drained=terminal,failed=False)
        row['ownership'].update(observation_sequence=sequence,eligible=not terminal,
                                reason='drain' if terminal else 'eligible')
        line=json.dumps(row).encode();raw.append(line)
        if consume:
            snapshot=dict(valid=False,reasons=['native_terminal'],seconds=None,query_host_ns=None,
                          row_sequence=None,row_sha256=None,native_generation=None,generation=2,
                          authorization_ns=query+2000000)
            rows.extend([dict(kind='owned_clock_input',row_sequence=sequence,row_sha256=hashlib.sha256(line).hexdigest(),
                              received_mach_ns=query+1000000,snapshot=snapshot),
                         dict(kind='owned_clock_snapshot',observed=False,**snapshot)])
        return line
    def terminal_tail(self):
        raw,rows,receipts=self.evidence()
        self.append_native(raw,rows,2,terminal=True,consume=True,query=1100000000)
        rows.extend([dict(kind='owned_callback',event='stop',issued_mach_ns=1080000000),
                     dict(kind='owned_monitor_exit',retired=True,reason='native_terminal',checked_mach_ns=1700000000)])
        self.append_native(raw,rows,3,terminal=True,query=1750000000)
        return raw,rows,receipts
    def test_missing_eligible_interior_and_tail_fail(self):
        for interior in (False,True):
            raw,rows,receipts=self.evidence();self.append_native(raw,rows,2)
            if interior:self.append_native(raw,rows,3,terminal=True,consume=True)
            self.assertIn('native_input_missing',e.join_evidence(raw,rows,receipts)['errors'])
    def test_terminal_tail_is_explicitly_accounted_with_original_identity(self):
        raw,rows,receipts=self.terminal_tail();result=e.join_evidence(raw,rows,receipts)
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['native_accounting']['consumed_sequences'],[1,2])
        excluded=result['native_accounting']['excluded_terminal_tail']
        self.assertEqual(len(excluded),1);self.assertEqual(excluded[0]['row_sequence'],3)
        self.assertEqual(excluded[0]['row_sha256'],hashlib.sha256(raw[-1]).hexdigest())
        self.assertEqual(excluded[0]['native_reason'],'drain')
        self.assertEqual(excluded[0]['query_host_ns'],1750000000)
    def test_terminal_tail_never_hides_eligible_row(self):
        raw,rows,receipts=self.terminal_tail()
        self.append_native(raw,rows,4,query=1770000000)
        self.append_native(raw,rows,5,terminal=True,query=1790000000)
        result=e.join_evidence(raw,rows,receipts)
        self.assertIn('native_input_missing',result['errors'])
        self.assertEqual(result['native_accounting']['excluded_terminal_tail'],[])
    def test_tail_requires_stop_exit_terminal_prefix_and_both_bounds(self):
        for broken in ('stop','exit','retired','exit_time','prefix','time','count','failed','nonterminal','before_stop'):
            raw,rows,receipts=self.terminal_tail()
            if broken in ('stop','exit'):
                kind='owned_callback' if broken=='stop' else 'owned_monitor_exit'
                rows=[r for r in rows if r['kind']!=kind]
            elif broken=='retired':next(r for r in rows if r['kind']=='owned_monitor_exit')['retired']=False
            elif broken=='exit_time':next(r for r in rows if r['kind']=='owned_monitor_exit')['checked_mach_ns']=5000000000
            elif broken=='prefix':
                # Last consumed row ceases to provide terminal proof; retain its exact identity.
                original=json.loads(raw[1]);original['ownership']['eligible']=True
                raw[1]=json.dumps(original).encode()
                next(r for r in rows if r['kind']=='owned_clock_input' and r['row_sequence']==2)['row_sha256']=hashlib.sha256(raw[1]).hexdigest()
            elif broken=='count':
                for i in range(4,68):self.append_native(raw,rows,i,terminal=True,query=1750000000+i*1000000)
            else:
                original=json.loads(raw[-1])
                if broken=='failed':original['failed']=True
                if broken=='nonterminal':original.update(drained=False,control_state='pause');original['ownership']['reason']='control_inactive'
                if broken in ('time','before_stop'):
                    query=5000000000 if broken=='time' else 1000000000
                    for key in ('mach_absolute_ns','steady_ns','delay_query_start_steady_ns','delay_query_end_steady_ns'):original[key]=query
                raw[-1]=json.dumps(original).encode()
            with self.subTest(broken=broken):
                self.assertIn('native_input_missing',e.join_evidence(raw,rows,receipts)['errors'])
    def test_consumed_invalid_row_still_requires_returned_snapshot(self):
        raw,rows,receipts=self.terminal_tail()
        rows=[r for r in rows if not (r['kind']=='owned_clock_snapshot' and r['valid'] is False)]
        self.assertIn('input_snapshot_accounting',e.join_evidence(raw,rows,receipts)['errors'])
    def canceled_b(self):
        raw,rows,_=self.evidence()
        for row in rows:
            if row['kind']=='owned_clock_snapshot':row['seconds']=32.768
            if row['kind']=='owned_clock_input':row['snapshot']['seconds']=32.768
            if row['kind'] in ('ad_tracking','owned_beacon_authorization'):
                row.update(placement_id=e.opaque('b_paused'),mapped_seconds=32.768)
            if row['kind']=='ad_tracking':row['creative_id']=e.opaque('b_paused')
            if row['kind']=='owned_beacon_authorization':
                row.update(authorized=False,authorization_ns=1200000000,threshold_seconds=32.768)
        rows.append(dict(kind='owned_callback',event='pause',issued_mach_ns=1050000000))
        return raw,rows,[]
    def test_stale_revoked_b_authorization_is_retained_without_delivery_failure(self):
        result=e.join_evidence(*self.canceled_b())
        self.assertEqual(result['errors'],[])
        event=result['events'][0];self.assertFalse(event['delivered'])
        self.assertEqual(event['authorization']['authorization_ns'],1200000000)
        self.assertTrue(event['cancellation']['proof_stale_at_check'])
        self.assertEqual(event['cancellation']['reason'],'delivery_authorization_rejected')
        self.assertEqual(event['cancellation']['intervening_controls'][0]['event'],'pause')
    def test_canceled_b_receipt_is_still_rejected(self):
        raw,rows,receipts=self.canceled_b();auth=next(r for r in rows if r['kind']=='owned_beacon_authorization')
        receipts.append(dict(session_id=auth['session_id'],creative='b_paused',event='start',accepted=True,
                             duplicate=False,status=204,method='GET',receipt_mach_ns=1210000000))
        result=e.join_evidence(raw,rows,receipts)
        self.assertIn('receipt_without_authorization',result['errors'])
        self.assertNotIn('authorization_freshness',result['errors'])
        self.assertEqual(result['http_receipts'],receipts)
    def test_canceled_b_is_not_evaluated_as_an_emitted_event(self):
        joined=e.join_evidence(*self.canceled_b())
        class Matcher:
            def match(self,ns):
                if ns==1010000000:return dict(qualified=False,reason='uncertain_decision')
                return dict(qualified=True,source_seconds=32.768,channels=[{'source_center_sample':32.768*e.RATE}]*2)
        result=e.evaluate(joined,Matcher())
        self.assertFalse(result['events'][0]['delivered'])
        self.assertNotIn('timing',result['events'][0])
        self.assertNotIn('event_capture_uncertain',result['errors'])
        self.assertNotIn('event_decision_timing',result['errors'])
        self.assertTrue(result['events'][0]['cancellation']['proof_stale_at_check'])
    def test_canceled_work_still_requires_timely_decision_and_ordered_check(self):
        for mutation in ('late_decision','early_check'):
            raw,rows,receipts=self.canceled_b()
            if mutation=='late_decision':
                for r in rows:
                    if r['kind'] in ('ad_tracking','owned_beacon_authorization'):r['decision_ns']=1150000000
            else:next(r for r in rows if r['kind']=='owned_beacon_authorization')['authorization_ns']=1000000001
            self.assertTrue(e.join_evidence(raw,rows,receipts)['errors'])
    def test_early_and_late_decisions_reported(self):
        for actual,early,failed in [(16.37,True,False),(16.3,True,True),(16.6,False,True)]:
            result=e.event_timing(16.384,actual,0.01)
            self.assertEqual(result['early'],early);self.assertEqual(result['violation'],failed)
    def test_preroll_never_manufactured_after_late_acquisition(self):
        expected=e.expected_events(.251)
        self.assertFalse(any(x[0]=='preroll' for x in expected))
        self.assertEqual(len(e.expected_events(.25)),30)


class FinalQualificationTests(unittest.TestCase):
    def audit(self,target):
        # Isolate the audit consumer while exercising the real source-order
        # mutation. All required event, pause and threshold evidence starts valid.
        first=32.768;clock_ns=1000000000;pause_ns=3000000000
        sources={clock_ns:first,pause_ns:first};regions={clock_ns:clock_ns,pause_ns:pause_ns}
        events=[];thresholds={}
        for i,(creative,event) in enumerate(sorted(e.expected_events(first))):
            lo,hi=e.PLACEMENTS[creative]
            threshold=(lo+(hi-lo)*e.FRACTIONS[event])*1024/e.RATE
            ns=2000000000+i*1000;threshold_ns=ns-100
            sources[ns]=sources[threshold_ns]=threshold
            regions[ns]=regions[threshold_ns]=ns;thresholds[ns]=threshold_ns
            events.append(dict(creative=creative,event=event,delivered=True,decision=dict(decision_ns=ns),
                authorization=dict(authorization_ns=ns+100),receipt=dict(receipt_mach_ns=ns+100),
                threshold_seconds=threshold))
        dependency={'clock':clock_ns,'pause':pause_ns,'decision':2000000000,'threshold':1999999900}.get(target)
        cancel_ns=1999999950 if target=='threshold' else 4000000000
        if dependency is not None:
            sources[cancel_ns]=sources[dependency]-64/e.RATE;regions[cancel_ns]=regions[dependency]
        events.append(dict(creative='b_paused',event='start',delivered=False,
            decision=dict(decision_ns=cancel_ns),authorization=dict(authorization_ns=cancel_ns+1),
            threshold_seconds=first))
        class Matcher:
            def __init__(self):self.source_order=[];self.cache={}
            def match(self,ns):
                if ns in self.cache:return self.cache[ns]
                if ns not in sources:return dict(qualified=False,reason='waveform_correlation')
                source=sources[ns]
                point=dict(qualified=True,host_ns=ns,source_seconds=source,
                    capture_frame=(ns-clock_ns)*e.RATE/1e9,region_id=regions[ns],
                    channels=[dict(source_center_sample=source*e.RATE) for _ in range(2)])
                e.CaptureMatcher._check_source_order(self,point);self.cache[ns]=point
                return point
            def threshold_host(self,threshold,decision):
                ns=thresholds[decision['host_ns']]
                if not self.match(ns)['qualified']:raise ValueError('threshold_pause_uncertainty')
                return ns
        joined=dict(errors=[],qualified=[dict(query_host_ns=clock_ns,row_sequence=1,row_sha256='test',seconds=first)],
            events=events,callbacks=[dict(event='pause',issued_mach_ns=pause_ns)],native_accounting={},
            http_receipts=[],tracking_decisions=[],authorizations=[])
        return e.evaluate(joined,Matcher())
    def test_canceled_decision_invalidating_required_clock_fails_final_gate(self):
        result=self.audit('clock')
        self.assertFalse(result['clock'][0]['capture']['qualified'])
        self.assertIn('qualified_clock_capture_uncertain',result['errors'])
        self.assertFalse(result['gate_passed'])
    def test_late_invalidated_pause_decision_and_threshold_fail_final_gate(self):
        for target,reason in (('pause','paused_ad_overlap_inconclusive'),
                              ('decision','event_capture_uncertain'),('threshold','event_threshold_uncertain')):
            with self.subTest(target=target):
                result=self.audit(target)
                self.assertIn(reason,result['errors'])
                self.assertFalse(result['gate_passed'])
    def test_uncertain_canceled_decision_alone_does_not_fail_required_evidence(self):
        result=self.audit(None)
        self.assertTrue(result['gate_passed'],result['errors'])
        self.assertFalse(result['events'][-1]['capture_at_decision']['qualified'])


class HalJoinTests(unittest.TestCase):
    def test_original_query_hash_bracket_and_s0_are_required(self):
        self.assertTrue(hasattr(e,'join_hal_evidence'),'distinct HAL evidence join missing')
        # An AE launch cannot select the HAL schema by presenting a HAL row.
        with self.assertRaises(ValueError):
            e.join_hal_evidence([], [dict(kind='owned_launch',output_backend='ae')], [])

class CapturePointTests(unittest.TestCase):
    def matcher(self):
        try:import numpy as np
        except ImportError:self.skipTest('NumPy analysis runtime required')
        from analyze_hls_capture import local_match
        matcher=e.CaptureMatcher.__new__(e.CaptureMatcher)
        matcher.np=np;matcher.local_match=local_match;matcher.cache={}
        matcher.reference=np.random.default_rng(17).normal(size=(24000,2)).astype('<f4')
        matcher.audio=np.zeros((33000,2),dtype='<f4')
        matcher.audio[100:12100]=matcher.reference[:12000]
        matcher.audio[20000:32000]=matcher.reference[12000:]
        matcher.timing=[dict(host_pts_ns=1000000000+round(i*1e9/e.RATE),frame_offset=i,frame_count=min(256,33000-i)) for i in range(0,33000,256)]
        matcher.host=np.array([x['host_pts_ns'] for x in matcher.timing],dtype=np.int64)
        matcher.offset=np.array([x['frame_offset'] for x in matcher.timing],dtype=np.int64)
        matcher.analysis={'segments':[{'index':0,'span':{'start':.004,'end':12100/e.RATE},
            'channels':[{'anchors':[{'capture_start_sample':2000,'offset_samples':100}]} for _ in range(2)]},
            {'index':1,'span':{'start':20000/e.RATE,'end':32000/e.RATE},
            'channels':[{'anchors':[{'capture_start_sample':22000,'offset_samples':8000}]} for _ in range(2)]}],
            'prefix_tail':[{'prefix':{'capture_start_sample':100,'reference_start_sample':0,'correlation':1.}} for _ in range(2)]}
        return matcher
    def test_forward_start_window_preserves_actual_subframe_position(self):
        matcher=self.matcher();point=matcher.match(matcher.frame_host(104.25))
        self.assertTrue(point['qualified'])
        self.assertAlmostEqual(point['source_seconds'],4.25/e.RATE,places=8)
        self.assertNotEqual(point['source_seconds'],0)
    def test_threshold_cannot_cross_pause(self):
        matcher=self.matcher()
        self.assertFalse(matcher.match(matcher.frame_host(20002))['qualified'])
        point=matcher.match(matcher.frame_host(20102))
        self.assertTrue(point['qualified'])
        with self.assertRaises(ValueError):matcher.threshold_host(5900/e.RATE,point)
    def test_actual_buffer_gap_is_not_interpolated(self):
        matcher=self.matcher();matcher.host[1:]+=10000000
        with self.assertRaises(ValueError):matcher.host_frame(1000000000+8000000)

class FractionalCaptureTests(unittest.TestCase):
    """Analytical fixture: no production interpolator generates expected audio."""
    def matcher(self, shifts=(.49,.49), length=48000):
        import numpy as np
        from analyze_hls_capture import local_match
        m=e.CaptureMatcher.__new__(e.CaptureMatcher)
        m.np=np;m.local_match=local_match;m.cache={}
        rng=np.random.default_rng(211)
        frequencies=rng.uniform(.015,.39,(2,48));phases=rng.uniform(-3,3,(2,48))
        def signal(t,ch):
            return np.sin(t[:,None]*frequencies[ch]*2*np.pi+phases[ch]).sum(axis=1)/40
        t=np.arange(length,dtype=float)
        m.reference=np.column_stack([signal(t,ch) for ch in (0,1)]).astype('<f4')
        m.audio=np.zeros((length+300,2),dtype='<f4')
        for ch in (0,1):m.audio[100:length+100,ch]=signal(t+shifts[ch],ch)
        m.timing=[dict(host_pts_ns=1000000000+round(i*1e9/e.RATE),frame_offset=i,
                       frame_count=min(256,len(m.audio)-i)) for i in range(0,len(m.audio),256)]
        m.host=np.array([t['host_pts_ns'] for t in m.timing],dtype=np.int64)
        m.offset=np.array([t['frame_offset'] for t in m.timing],dtype=np.int64)
        m.analysis={'segments':[dict(index=0,span=dict(start=96/e.RATE,end=(length+100)/e.RATE),
            channels=[dict(anchors=[dict(capture_start_sample=12000,offset_samples=100-round(
                float(shifts[ch][11900]) if np.ndim(shifts[ch]) else shifts[ch]))]) for ch in (0,1)])],
            'prefix_tail':[dict(prefix=dict(capture_start_sample=100,reference_start_sample=0,correlation=1.),
                                tail=dict(capture_start_sample=length-860,reference_start_sample=length-960,correlation=1.)) for ch in (0,1)]}
        return m
    def point(self,m,frame):return m.match(m.frame_host(frame))
    def test_hal_fractional_prefix_reproved_from_disjoint_audio_not_integer_threshold(self):
        m=self.matcher((.49,.49));m.allow_fractional_endpoints=True
        for item in m.analysis['prefix_tail']:
            item['prefix']['correlation']=.8;item['tail']['correlation']=.8
        result=self.point(m,100.25)
        self.assertTrue(result['qualified'],result)
        self.assertTrue(hasattr(m,'endpoint_proof'),'independent endpoint proof missing')
        self.assertEqual(m.endpoint_proof['prefix'][0]['heldout_source_frames'],[0,1920])
        self.assertGreaterEqual(m.endpoint_proof['prefix'][0]['correlation'],.99)
        self.assertEqual(m.endpoint_proof['tail'][0]['heldout_source_frames'],[48000-1920,48000])
        self.assertGreaterEqual(m.endpoint_proof['tail'][0]['correlation'],.99)
        self.assertTrue(m.endpoint_proof['prefix'][0]['training']['source_frames'][0]>=4320)

    def test_hal_contaminated_prefix_cannot_be_proven_by_later_training(self):
        m=self.matcher();m.allow_fractional_endpoints=True
        m.audio[100:2020]*=-1
        for item in m.analysis['prefix_tail']:item['prefix']['correlation']=.8
        result=self.point(m,100.25)
        self.assertFalse(result['qualified'],result)

    def test_fractional_audio_and_exact_query_position(self):
        for shift in (-.49,-.24,-.02,.02,.24,.49):
            with self.subTest(shift=shift):
                m=self.matcher((shift,shift));v=self.point(m,14000.25)
                self.assertTrue(v['qualified'],v)
                self.assertAlmostEqual(v['source_seconds']*e.RATE,13900.25+shift,delta=.012)
                self.assertTrue(all(c['correlation']>=.99 for c in v['channels']))
    def test_source_zero_bounds_inside_rms_span_and_tail(self):
        m=self.matcher()
        for frame in (104.25,48090.25):
            v=self.point(m,frame);self.assertTrue(v['qualified'],v)
            self.assertAlmostEqual(v['source_seconds']*e.RATE,frame-99.51,delta=.012)
        self.assertFalse(self.point(m,98)['qualified'])
    def test_stereo_phase_disagreement(self):
        m=self.matcher((.49,-.74));v=self.point(m,14000)
        self.assertFalse(v['qualified'],v)
        self.assertEqual(v['reason'],'stereo_disagreement')
    def test_disjoint_training_cache_and_call_order(self):
        m=self.matcher();a=self.point(m,14000);b=self.point(m,14200)
        self.assertTrue(a['qualified'],a);self.assertTrue(b['qualified'],b)
        for point in (a,b):
            for c in point['channels']:
                lo,hi=c['heldout_capture_frames'];tl,th=c['training']['capture_frames']
                self.assertTrue(th<=lo-2400 or tl>=hi+2400)
        self.assertEqual(a['channels'][0]['training']['id'],b['channels'][0]['training']['id'])
        other=self.matcher();self.point(other,14200)
        self.assertEqual(a,self.point(other,14000))
        m.cache.clear();m.phase_cache.clear()
        self.assertEqual(a,self.point(m,14000))
    def test_quiet_dropout_and_filter_edge_are_never_interpolated(self):
        for channels in ((0,1),(0,)):
            m=self.matcher();m.audio[16000:16417,list(channels)]=0
            for frame in (16020,16200,16420):
                self.assertFalse(self.point(m,frame)['qualified'])
            before=self.point(m,12000);after=self.point(m,21000)
            self.assertTrue(before['qualified'],before);self.assertTrue(after['qualified'],after)
            with self.assertRaises(ValueError):m.threshold_host(before['source_seconds'],after)
    def test_integer_jump_and_omit_replay_are_not_fractional_alignment(self):
        for replay in (False,True):
            m=self.matcher((0,0))
            m.audio[16000:24000]=m.reference[15920:23920]
            if not replay:m.audio[24000:47000]=m.reference[23920:46920]
            v=self.point(m,16005)
            self.assertFalse(v['qualified'],v)
    def test_threshold_zero_requires_independent_prefix_rematch(self):
        m=self.matcher();v=self.point(m,500)
        self.assertTrue(v['qualified'],v)
        host=m.threshold_host(0,v);z=m.match(host)
        self.assertTrue(z['qualified'],z);self.assertAlmostEqual(z['source_seconds'],0,delta=1/e.RATE)
        m=self.matcher();m.audio[100:517]=0
        v=self.point(m,5000)
        self.assertTrue(v['qualified'],v)
        with self.assertRaises(ValueError):m.threshold_host(0,v)
    def test_recorder_gap_splits_threshold_region(self):
        m=self.matcher();m.host[m.offset>=16384]+=10000000
        v=self.point(m,21000);self.assertTrue(v['qualified'],v)
        with self.assertRaises(ValueError):m.threshold_host(12000/e.RATE,v)
    def test_recorder_cut_rejects_filter_edges_and_keeps_all_sinc_support_on_one_side(self):
        m=self.matcher();cut=16384;m.host[m.offset>=cut]+=10000000
        for frame in (cut-1,cut-46,cut-95,cut,cut+6,cut+46,cut+95):
            with self.subTest(frame=frame):self.assertFalse(self.point(m,frame)['qualified'])
        for frame in (cut-97,cut+97):
            point=self.point(m,frame);self.assertTrue(point['qualified'],point)
            for channel in point['channels']:
                for start,stop in (channel['heldout_capture_frames'],channel['training']['capture_frames']):
                    # Actual 96-tap support, including the final output frame.
                    if frame<cut:self.assertLessEqual(stop+48,cut)
                    else:self.assertGreaterEqual(start-47,cut)
        with self.assertRaises(ValueError):m.threshold_host((cut-200-99.51)/e.RATE,point)
    def test_clock_metadata_cannot_select_alignment(self):
        m=self.matcher();v=self.point(m,14000)
        other=self.matcher();other.analysis['clock']={'seconds':-900,'accepted_frames':99999999,'threshold':999}
        self.assertEqual(v,self.point(other,14000))
    def test_short_span_cannot_reuse_heldout_as_training(self):
        m=self.matcher((0,0));m.analysis['segments'][0]['span']['end']=6100/e.RATE
        v=self.point(m,104.25)
        self.assertFalse(v['qualified'],v)
        self.assertEqual(v['reason'],'disjoint_training_unavailable')
    def test_zero_energy_and_missing_tail_reject(self):
        m=self.matcher();m.audio[47000:]=0
        self.assertFalse(self.point(m,48090)['qualified'])
        m=self.matcher();m.audio[:]=0
        self.assertFalse(self.point(m,14000)['qualified'])
    def test_source_prefix_can_contain_its_own_initial_quiet(self):
        m=self.matcher((0,0));m.reference[:48]=0;m.audio[100:148]=0
        v=self.point(m,500)
        self.assertTrue(v['qualified'],v)
        self.assertTrue(self.point(m,100)['qualified'])
        z=m.match(m.threshold_host(0,v))
        self.assertTrue(z['qualified'],z)
        self.assertAlmostEqual(z['source_seconds'],0,delta=1/e.RATE)
    def test_local_fallback_revalidates_heldout_and_retains_both_attempts(self):
        m=self.matcher();different=self.matcher((-.24,-.24))
        m.audio[23900:26500]=different.audio[23900:26500]
        v=self.point(m,14000)
        self.assertTrue(v['qualified'],v)
        self.assertEqual(len(v['training_attempts']),2)
        self.assertEqual(v['training_attempts'][0]['reason'],'waveform_correlation')
        self.assertAlmostEqual(v['source_seconds']*e.RATE,13900.49,delta=.012)
    def test_failed_fallback_is_bounded_and_never_reuses_heldout(self):
        m=self.matcher();different=self.matcher((-.24,-.24))
        for lo,hi in ((23900,26500),(17260,19860)):m.audio[lo:hi]=different.audio[lo:hi]
        v=self.point(m,14000)
        self.assertFalse(v['qualified'],v);self.assertEqual(len(v['training_attempts']),2)
    def test_integer_step_between_training_and_heldout_is_not_retrained_away(self):
        m=self.matcher((0,0));m.audio[16000:47000]=m.reference[15920:46920]
        v=self.point(m,14000)
        self.assertFalse(v['qualified'],v)
        self.assertEqual(v['reason'],'source_mapping_discontinuity')
        self.assertEqual(len(v['training_attempts']),1)
    def test_short_omission_replay_inside_heldout_fails(self):
        m=self.matcher((0,0));m.audio[14000:14120]=m.reference[13940:14060]
        v=self.point(m,14000)
        self.assertFalse(v['qualified'],v)
    def test_phase_cache_eviction_repeats_identical_audio_calculation(self):
        m=self.matcher();first=self.point(m,14000)
        self.assertTrue(first['qualified'],first)
        region=m.regions[0]
        for start in range(10000,10514):m._phase_training(region,start,0)
        self.assertLessEqual(len(m.phase_cache),512)
        m.cache.clear()
        self.assertEqual(first,self.point(m,14000))
    def test_backward_source_progress_is_detected_in_query_order(self):
        outputs=[]
        for order in ((40000,68000),(68000,40000)):
            m=self.matcher((0,0),length=144000)
            m.audio[60100:144100]=m.reference[24000:108000]
            for channel in m.analysis['segments'][0]['channels']:
                channel['anchors']=[dict(capture_start_sample=p,offset_samples=o)
                                    for p,o in ((12000,100),(84000,36100),(132000,36100))]
            points={frame:self.point(m,frame) for frame in order}
            self.assertTrue(all(not p['qualified'] for p in points.values()),points)
            self.assertTrue(all(p['reason']=='source_mapping_nonmonotonic' for p in points.values()))
            outputs.append(points)
        self.assertEqual(outputs[0],outputs[1])
    def test_gradual_fractional_drift_has_no_global_one_sample_limit(self):
        import numpy as np
        phase=np.linspace(0,2,144000)
        m=self.matcher((phase,phase),length=144000)
        for channel in m.analysis['segments'][0]['channels']:
            channel['anchors']=[dict(capture_start_sample=p,offset_samples=100-round(float(phase[p-100])))
                                for p in (12000,84000,132000)]
        for frame in (12000,132000):
            v=self.point(m,frame)
            self.assertTrue(v['qualified'],v)
            self.assertAlmostEqual(v['source_seconds']*e.RATE,frame-100+float(phase[frame-100]),delta=.12)
    def test_nonfinite_region_metadata_is_structured_uncertainty(self):
        for field,value in (('start',float('inf')),('end',float('nan')),('start',-1)):
            m=self.matcher();m.analysis['segments'][0]['span'][field]=value
            try:point=self.point(m,14000)
            except (ValueError,OverflowError) as exc:self.fail('region metadata escaped match: '+str(exc))
            self.assertFalse(point['qualified'],point)
            self.assertEqual(point['reason'],'invalid_audio_region')
    def test_bad_anchor_is_structured_uncertainty(self):
        m=self.matcher()
        for c in m.analysis['segments'][0]['channels']:c['anchors'][0]['offset_samples']=10**12
        v=self.point(m,14000)
        self.assertFalse(v['qualified']);self.assertEqual(v['reason'],'source_window_unavailable')

if __name__=='__main__':unittest.main()
