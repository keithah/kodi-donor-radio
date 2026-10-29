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
