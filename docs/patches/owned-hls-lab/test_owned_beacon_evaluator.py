import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import evaluate_owned_beacons as e

class OwnedEvaluatorTests(unittest.TestCase):
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


class CapturePointTests(unittest.TestCase):
    def matcher(self):
        try:import numpy as np
        except ImportError:self.skipTest('NumPy analysis runtime required')
        from analyze_hls_capture import local_match
        matcher=e.CaptureMatcher.__new__(e.CaptureMatcher)
        matcher.np=np;matcher.local_match=local_match;matcher.cache={}
        matcher.reference=np.random.default_rng(17).normal(size=(12000,2)).astype('<f4')
        matcher.audio=np.zeros((17000,2),dtype='<f4')
        matcher.audio[100:6100]=matcher.reference[:6000]
        matcher.audio[10000:16000]=matcher.reference[6000:]
        matcher.timing=[dict(host_pts_ns=1000000000+round(i*1e9/e.RATE),frame_offset=i,frame_count=min(256,17000-i)) for i in range(0,17000,256)]
        matcher.host=np.array([x['host_pts_ns'] for x in matcher.timing],dtype=np.int64)
        matcher.offset=np.array([x['frame_offset'] for x in matcher.timing],dtype=np.int64)
        matcher.analysis={'segments':[{'index':0,'span':{'start':.004,'end':6100/e.RATE},
            'channels':[{'anchors':[{'capture_start_sample':2000,'offset_samples':100}]} for _ in range(2)]},
            {'index':1,'span':{'start':10000/e.RATE,'end':16000/e.RATE},
            'channels':[{'anchors':[{'capture_start_sample':12000,'offset_samples':4000}]} for _ in range(2)]}],
            'prefix_tail':[{'prefix':{'capture_start_sample':100,'reference_start_sample':0,'correlation':1.}} for _ in range(2)]}
        return matcher
    def test_forward_start_window_preserves_actual_subframe_position(self):
        matcher=self.matcher();point=matcher.match(matcher.frame_host(104.25))
        self.assertTrue(point['qualified'])
        self.assertAlmostEqual(point['source_seconds'],4.25/e.RATE,places=8)
        self.assertNotEqual(point['source_seconds'],0)
    def test_threshold_cannot_cross_pause(self):
        matcher=self.matcher();point=matcher.match(matcher.frame_host(10002))
        self.assertTrue(point['qualified'])
        with self.assertRaises(ValueError):matcher.threshold_host(5900/e.RATE,point)
    def test_actual_buffer_gap_is_not_interpolated(self):
        matcher=self.matcher();matcher.host[1:]+=10000000
        with self.assertRaises(ValueError):matcher.host_frame(1000000000+8000000)

if __name__=='__main__':unittest.main()
