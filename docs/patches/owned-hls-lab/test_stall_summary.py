import unittest
from summarize_stalls import summarize,OPERATIONS
BASE=1_000_000_000

def fixture():
    metrics={name:dict(count=0,max_ns=0,max_start_mach_ns=0,max_end_mach_ns=0) for name in OPERATIONS}
    metrics['renderer_sleep_overshoot']=dict(count=3,max_ns=50_000_000,max_start_mach_ns=BASE+100_000_000,max_end_mach_ns=BASE+150_000_000)
    return dict(kind='clock_sample',renderer_qos_status=0,renderer_qos_class=9,renderer_qos_relative_priority=0,
                stall_diagnostics=dict(version=1,threshold_ns=40_000_000,available=True,revision=2,
                dropped_contention=2,dropped_events=3,operations=metrics,
                long_events=[dict(operation='renderer_sleep_overshoot',start_mach_ns=BASE+100_000_000,end_mach_ns=BASE+150_000_000)]))

class SummaryTests(unittest.TestCase):
    def test_overlap_and_drop_evidence(self):
        report=summarize([dict(kind='source_packet'),fixture()],BASE,[(.12,.13),(.15,.16)])
        self.assertEqual(report['dropped_events'],3)
        self.assertEqual(report['dropped_contention'],2)
        operation=report['operations']['renderer_sleep_overshoot']
        self.assertEqual(operation['count'],3)
        self.assertEqual(operation['maximum']['duration_ms'],50)
        self.assertEqual(operation['long_events'][0]['gap_overlaps'],[dict(gap_index=0,overlap_ms=10)])
        self.assertEqual(report['long_events_overlapping_supplied_gaps'],1)
        self.assertTrue(report['overlap_is_not_causation'])
    def test_unavailable_and_legacy_rows(self):
        unavailable=fixture(); diagnostic=unavailable['stall_diagnostics']; diagnostic['available']=False
        for key in ('revision','dropped_contention','dropped_events'): diagnostic[key]=0
        diagnostic['long_events']=[]
        for metric in diagnostic['operations'].values():
            for key in metric: metric[key]=0
        report=summarize([dict(kind='clock_sample'),unavailable,fixture()],BASE)
        self.assertEqual(report['clock_rows_without_diagnostics'],1)
        self.assertEqual(report['unavailable_snapshots'],1)
        self.assertEqual(report['dropped_events'],3)
    def test_bad_gap_and_bad_revision_rejected(self):
        for gaps in ([('bad',1)],[(float('nan'),1)],[(2,1)]):
            with self.assertRaises((ValueError,TypeError)): summarize([],BASE,gaps)
        with self.assertRaises(ValueError): summarize([fixture(),fixture()],BASE)
    def test_bad_bracket_rejected(self):
        row=fixture(); row['stall_diagnostics']['operations']['renderer_sleep_overshoot']['max_ns']=1
        with self.assertRaises(ValueError): summarize([row],BASE)


class StrictSchemaTests(unittest.TestCase):
    def bad(self,row):
        with self.assertRaises(ValueError): summarize([row],BASE)
    def test_missing_unknown_operations(self):
        row=fixture(); del row['stall_diagnostics']['operations']['getspace']; self.bad(row)
        row=fixture(); row['stall_diagnostics']['operations']['unknown']={}; self.bad(row)
    def test_strict_bool_and_version(self):
        for key,value in [('available',1),('available','true'),('version',True),('version',1.0)]:
            row=fixture(); row['stall_diagnostics'][key]=value; self.bad(row)
    def test_uint64_bounds_and_bool(self):
        for value in (True,-1,2**64,1.5):
            row=fixture(); row['stall_diagnostics']['revision']=value; self.bad(row)
        row=fixture(); row['stall_diagnostics']['operations']['getspace']['count']=2**64; self.bad(row)
    def test_bad_structures(self):
        for value in ([],None,1):
            row=fixture(); row['stall_diagnostics']=value; self.bad(row)
        row=fixture(); row['stall_diagnostics']['operations']=[]; self.bad(row)
        row=fixture(); row['stall_diagnostics']['long_events']={}; self.bad(row)
        row=fixture(); row['stall_diagnostics']['extra']=0; self.bad(row)
    def test_event_bounds_and_count(self):
        row=fixture(); row['stall_diagnostics']['long_events']*=13; self.bad(row)
        row=fixture(); row['stall_diagnostics']['long_events'][0]['operation']='getspace'; self.bad(row)
        row=fixture(); row['stall_diagnostics']['operations']['getspace']['max_end_mach_ns']=1; self.bad(row)
    def test_unavailable_cannot_claim_data(self):
        row=fixture(); row['stall_diagnostics']['available']=False; self.bad(row)

if __name__=='__main__': unittest.main()
