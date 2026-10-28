import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from estimator import Estimator, PERIOD


def row(t, position, state='play', accepted=None, delay_error=0):
    endpoint = position + .5 if accepted is None else accepted
    ns = int((1000 + t) * 1e9)
    return dict(kind='clock_sample', mach_absolute_ns=ns, steady_ns=ns+123,
                delay_query_start_steady_ns=ns+123,
                delay_query_end_steady_ns=ns+123,
                accepted_adddata_frames=round(endpoint*48000),
                audioengine_delay_s=endpoint-position+delay_error,
                playback_started=True, failed=False, drained=False,
                source_epoch=0, control_state=state)


class CandidateTests(unittest.TestCase):
    def test_invalid_delay_is_uncertain_and_reacquires(self):
        for value in [None, float('nan'), float('inf'), -float('inf'), 'missing']:
            with self.subTest(value=value):
                e=self.acquired()
                before=e.position
                x=row(.4,.4)
                if value == 'missing':
                    del x['audioengine_delay_s']
                else:
                    x['audioengine_delay_s']=value
                result=e.update(x)
                self.assertFalse(result['valid'])
                self.assertEqual(result['seconds'],before)
                self.assertIsNone(result['raw_seconds'])
                self.assertIsNone(result['anchor'])
                self.assertIn('invalid_query',result['reasons'])
                json.dumps(result,allow_nan=False)
                for i in range(9):
                    result=e.update(row(.42+i*.02,.42+i*.02))
                    self.assertEqual(result['valid'],i==8)

    def test_strict_json_sanitizes_nonfinite_nested_evidence(self):
        from estimator import strict_json
        encoded=strict_json({'bad':[float('nan'),{'v':float('inf')}], 'finite':1.25})
        self.assertEqual(json.loads(encoded),{'bad':[None,{'v':None}], 'finite':1.25})
        self.assertNotIn('NaN',encoded)
        self.assertNotIn('Infinity',encoded)

    def test_cli_retains_invalid_rows_and_writes_strict_json(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/'ledger.jsonl'; output=Path(folder)/'result.json'
            rows=[]
            for i,value in enumerate([None,float('nan'),float('inf'),-float('inf')]):
                x=row(i*.02,i*.02);x['audioengine_delay_s']=value;rows.append(x)
            x=row(.08,.08);del x['audioengine_delay_s'];rows.append(x)
            ledger.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            run=subprocess.run([sys.executable,str(Path(__file__).with_name('estimator.py')),
                                '--ledger',str(ledger),'--output',str(output)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            def reject(value):
                raise AssertionError('Nonstandard JSON constant: '+value)
            report=json.loads(output.read_text(),parse_constant=reject)
            timeline=[json.loads(x,parse_constant=reject) for x in output.with_suffix('.timeline.jsonl').read_text().splitlines()]
            self.assertEqual(report['samples'],5)
            self.assertEqual(report['valid_samples'],0)
            self.assertEqual(len(timeline),5)
            self.assertTrue(all(x['raw_seconds'] is None and not x['valid'] for x in timeline))

    def acquired(self):
        e=Estimator()
        out=[e.update(row(i*.02, i*.02)) for i in range(20)]
        self.assertFalse(out[7]['valid'])
        self.assertTrue(out[8]['valid'])
        return e

    def test_single_stale_delay_is_not_clock_jump(self):
        e=self.acquired()
        result=e.update(row(.4,.4,delay_error=PERIOD))
        self.assertTrue(result['valid'])
        self.assertAlmostEqual(result['seconds'],.4,places=8)

    def test_pause_tail_then_hold_and_resume_reacquires(self):
        e=self.acquired()
        results=[e.update(row(.4+i*.02,.4,'pause',accepted=.88)) for i in range(40)]
        self.assertGreater(results[1]['seconds'],results[0]['seconds'])
        self.assertAlmostEqual(results[-1]['seconds'],.88)
        self.assertTrue(all(not r['valid'] for r in results))
        for i in range(9):
            r=e.update(row(1.2+i*.02,.88+i*.02,'resume'))
            self.assertEqual(r['valid'], i==8)

    def test_large_native_step_reacquires(self):
        e=self.acquired()
        r=e.update(row(.4,.4,delay_error=3*PERIOD))
        self.assertFalse(r['valid'])
        self.assertIn('anchor_innovation',r['reasons'])

    def test_gap_and_wide_query_invalidate(self):
        e=self.acquired()
        self.assertFalse(e.update(row(.6,.6))['valid'])
        e=self.acquired(); x=row(.4,.4)
        x['delay_query_end_steady_ns']+=6_000_000
        self.assertFalse(e.update(x)['valid'])

    def test_endpoint_underrun_and_reacquisition(self):
        e=self.acquired()
        for i in range(26):
            t=.4+i*.02
            r=e.update(row(t,min(t,.88),accepted=.88))
        self.assertFalse(r['valid'])
        self.assertIn('accepted_endpoint',r['reasons'])
        for i in range(9):
            r=e.update(row(.92+i*.02,.88+i*.02))
        self.assertTrue(r['valid'])

    def test_monotonic_and_new_epoch(self):
        e=self.acquired()
        results=[e.update(row(.4+i*.02,.4+i*.02,delay_error=PERIOD)) for i in range(20)]
        self.assertEqual(sorted(x['seconds'] for x in results),[x['seconds'] for x in results])
        x=row(1,0);x['source_epoch']=1
        r=e.update(x)
        self.assertFalse(r['valid'])
        self.assertIn('epoch_reset',r['reasons'])


if __name__=='__main__':
    unittest.main()
