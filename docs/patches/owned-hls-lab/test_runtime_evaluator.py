import copy
import hashlib
import json
import unittest
from evaluate_runtime_clock import evaluate, THRESHOLD_SECONDS, EXPECTED_SHA256, EXPECTED_URL


def fixture():
    row = dict(kind='clock_sample', mach_absolute_ns=1_000_000_000,
               steady_ns=200, delay_query_start_steady_ns=190,
               delay_query_end_steady_ns=210)
    lines = [b'{"kind":"source_packet"}\n', json.dumps(row).encode()+b'\n']
    report = {'clock': {'samples': [dict(qualified=True, ledger_row=0,
                query_host_ns=1_000_000_000, channels=[{'source_center_sample':48000}, {'source_center_sample':48000}])]}}
    runtime = dict(kind='runtime_clock_input', launch_id='run', launch_ns=900_000_000, identity=dict(ledger_line=2,
                row_sha256=hashlib.sha256(lines[1].rstrip(b'\n')).hexdigest()),
                input=row, query_host_ns=1_000_000_000,
                received_ns=1_010_000_000, evaluated_ns=1_012_000_000,
                valid=True, reasons=[], estimate=dict(valid=True, reasons=[], seconds=1.0))
    launch=dict(kind='runtime_clock_launch',launch_id='run',launch_ns=900_000_000, selected_url=EXPECTED_URL,estimator_sha256=EXPECTED_SHA256,production_eligible=False)
    return report, lines, [runtime, launch]


class RuntimeEvaluationTests(unittest.TestCase):
    def test_exact_recorded_output_and_age(self):
        args=fixture(); result=evaluate(*args)
        self.assertTrue(result['gate_passed'])
        self.assertFalse(result['mapping_proven'])
        self.assertEqual(result['all_independently_qualified'],1)
        self.assertEqual(result['runtime_valid'],1)
        self.assertEqual(result['samples'][0]['query_to_receive_ms'],10)
        self.assertEqual(result['samples'][0]['query_to_evaluate_ms'],12)
        # Intentionally arbitrary native fields: evaluator only uses recorded estimate.
        args[2][0]['estimate']['seconds']=2
        self.assertFalse(evaluate(*args)['gate_passed'])
        self.assertEqual(evaluate(*args)['violation_count'],1)

    def test_missing_and_invalid_are_not_dropped(self):
        report,lines,runtime=fixture()
        missing=evaluate(report,lines,runtime[1:])
        self.assertEqual(missing['runtime_missing'],1)
        self.assertFalse(missing['gate_passed'])
        runtime[0].update(valid=False,reasons=['query_freshness'])
        invalid=evaluate(report,lines,runtime)
        self.assertEqual(invalid['runtime_invalid'],1)
        self.assertEqual(invalid['invalid_reasons'],{'query_freshness':1})
        self.assertFalse(invalid['gate_passed'])
        self.assertEqual(invalid['samples'][0]['error_seconds'],[0,0])

    def test_mismatches_fail(self):
        for field,value in [('query_host_ns',1),('input',{}),('identity',{'ledger_line':2,'row_sha256':'bad'})]:
            report,lines,runtime=fixture(); runtime[0][field]=value
            with self.subTest(field=field), self.assertRaises(ValueError): evaluate(report,lines,runtime)
        report,lines,runtime=fixture(); report['clock']['samples'][0]['query_host_ns']=2
        with self.assertRaises(ValueError): evaluate(report,lines,runtime)

    def test_nonstring_runtime_row_kind_is_rejected(self):
        for value in (None, 5, {}):
            report,lines,runtime=fixture()
            runtime.insert(0, {'kind': value})
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'runtime record kind'):
                evaluate(report,lines,runtime)

    def test_duplicates_fail(self):
        report,lines,runtime=fixture()
        with self.assertRaises(ValueError): evaluate(report,lines,runtime+[runtime[0]])
        report['clock']['samples']*=2
        with self.assertRaises(ValueError): evaluate(report,lines,runtime)

    def test_invalid_seconds_and_timing(self):
        for change in [('received_ns',0),('evaluated_ns',1_000_000_000)]:
            report,lines,runtime=fixture(); runtime[0][change[0]]=change[1]
            result=evaluate(report,lines,runtime)
            self.assertEqual(result['runtime_invalid'],1)
            self.assertIn('invalid_runtime_timing',result['invalid_reasons'])
        for seconds in [None,float('nan'),True]:
            report,lines,runtime=fixture(); runtime[0]['estimate']['seconds']=seconds
            self.assertFalse(evaluate(report,lines,runtime)['gate_passed'])

    def test_empty_qualified_never_passes_and_snapshots_not_inputs(self):
        report,lines,runtime=fixture(); runtime[0]['kind']='runtime_clock_snapshot'
        self.assertEqual(evaluate(report,lines,runtime)['runtime_missing'],1)
        report['clock']['samples'][0]['qualified']=False
        self.assertFalse(evaluate(report,lines,runtime)['gate_passed'])

    def test_manifest_identity_and_bounds(self):
        for field,value in [('selected_url','other'), ('estimator_sha256','bad'), ('production_eligible',True)]:
            report,lines,runtime=fixture(); runtime[1][field]=value
            with self.subTest(field=field), self.assertRaises(ValueError): evaluate(report,lines,runtime)
        report,lines,runtime=fixture(); runtime[0]['launch_id']='other'
        with self.assertRaises(ValueError): evaluate(report,lines,runtime)
        report,lines,runtime=fixture()
        with self.assertRaises(ValueError): evaluate(report,lines,runtime[:1])
        with self.assertRaises(ValueError): evaluate(report,lines,runtime+[runtime[1]])
        for field in ['received_ns','evaluated_ns']:
            report,lines,runtime=fixture(); runtime[0][field]=1_100_000_001
            if field=='received_ns': runtime[0]['evaluated_ns']=1_100_000_002
            result=evaluate(report,lines,runtime)
            self.assertFalse(result['gate_passed'])
            self.assertEqual(result['all_runtime_rows']['invalid'],1)
            self.assertIn('query_freshness',result['invalid_reasons'])
            self.assertGreater(result['age_ms']['query_to_evaluate']['max'],100)
        report,lines,runtime=fixture()
        for r in runtime: r['launch_ns']=1_000_000_001
        result=evaluate(report,lines,runtime)
        self.assertIn('before_launch',result['invalid_reasons'])
        self.assertFalse(result['gate_passed'])

    def test_all_runtime_counts_include_unqualified(self):
        report,lines,runtime=fixture(); report['clock']['samples'][0]['qualified']=False
        result=evaluate(report,lines,runtime)
        self.assertEqual(result['all_runtime_rows'],dict(total=1,valid=1,invalid=0,invalid_reasons={}))
        self.assertEqual(result['age_ms']['query_to_receive'],dict(min=10,max=10))

    def test_actual_invalid_schema_outputs_survive_strict_emission(self):
        from runtime_clock import RuntimeClockSession
        for missing_timestamp in (False, True):
            report, lines, runtime = fixture()
            row = json.loads(lines[1])
            row.update(accepted_adddata_frames=48000, audioengine_delay_s=float('nan'),
                       source_epoch=0, control_state='play', playback_started=True,
                       failed=False, drained=False)
            if missing_timestamp:
                del row['mach_absolute_ns']
                report['clock']['samples'][0]['qualified'] = False
            lines[1] = json.dumps(row).encode()+b'\n'
            session = RuntimeClockSession(EXPECTED_URL, 900_000_000, now_ns=lambda: 1_010_000_000)
            session.control('play', 900_000_001)
            identity = dict(ledger_line=2, row_sha256=hashlib.sha256(lines[1].rstrip(b'\n')).hexdigest())
            actual = session.ingest(row, identity=identity)
            self.assertIsNone(actual['query_host_ns'])
            self.assertIn('invalid_schema', actual['reasons'])
            emitted = json.loads(session.module.strict_json(actual))
            runtime[0] = emitted
            runtime[1].update(launch_id=session.launch_id, launch_ns=session.launch_ns)
            result = evaluate(report, lines, runtime)
            self.assertEqual(result['all_runtime_rows']['invalid'], 1)
            self.assertIn('invalid_schema', result['all_runtime_rows']['invalid_reasons'])
            self.assertEqual(result['age_ms']['query_to_receive'], dict(min=None, max=None))
            self.assertFalse(result['gate_passed'])
            if not missing_timestamp:
                self.assertEqual(result['runtime_invalid'], 1)
                self.assertIsNone(result['samples'][0]['query_to_receive_ms'])
            # Normalization cannot bypass byte-level identity checks.
            runtime[0]['identity']['row_sha256'] = 'bad'
            with self.assertRaises(ValueError): evaluate(report, lines, runtime)

    def test_threshold_both_channels(self):
        report,lines,runtime=fixture()
        report['clock']['samples'][0]['channels'][1]['source_center_sample']=48000+1025
        result=evaluate(report,lines,runtime)
        self.assertEqual(result['violation_count'],1)
        self.assertFalse(result['gate_passed'])
        self.assertEqual(result['threshold_seconds'],THRESHOLD_SECONDS)


if __name__=='__main__': unittest.main()
