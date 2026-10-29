import json
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

    def test_qos_experiment_evidence_roundtrips_without_inference(self):
        for values in (
            dict(requested_class=25,set_status=0,initial_class=9,
                 initial_relative_priority=0,initial_status=0,request_confirmed=True),
            dict(requested_class=25,set_status=22,initial_class=9,
                 initial_relative_priority=-2,initial_status=1,request_confirmed=False),
        ):
            with self.subTest(values=values):
                row=fixture()
                row.update({'renderer_qos_'+key:value for key,value in values.items()})
                report=json.loads(json.dumps(summarize([row],BASE)))
                qos=report['qos_observations'][0]
                self.assertEqual({key:qos[key] for key in values},values)
                self.assertEqual(qos['status'],0)
    def test_baseline_qos_experiment_fields_remain_unavailable(self):
        qos=summarize([fixture()],BASE)['qos_observations'][0]
        fields=('requested_class','set_status','initial_class','initial_relative_priority',
                'initial_status','request_confirmed')
        self.assertEqual({key:qos[key] for key in fields},{key:None for key in fields})


ADDED = ('output_guard_before','output_guard_after','clock_observation','native_append',
         'host_demux_call','host_demux_interarrival')
def fixture_v2():
    row=fixture(); d=row['stall_diagnostics']; d['version']=2
    d['host_threads']=[]; d['dropped_host_threads']=0
    d['operations'].update({name:dict(count=0,max_ns=0,max_start_mach_ns=0,max_end_mach_ns=0) for name in ADDED})
    return row

class VersionTwoTests(unittest.TestCase):
    def test_component_maxima_and_mixed_archive_keep_old_metrics(self):
        old=fixture(); row=fixture_v2(); d=row['stall_diagnostics'];d['revision']=3
        d['operations']['host_demux_interarrival']=dict(count=1,max_ns=70_000_000,max_start_mach_ns=BASE+200_000_000,max_end_mach_ns=BASE+270_000_000)
        d['operations']['host_demux_call']=dict(count=2,max_ns=1_000_000,max_start_mach_ns=BASE+270_000_000,max_end_mach_ns=BASE+271_000_000)
        d['long_events'].append(dict(operation='host_demux_interarrival',start_mach_ns=BASE+200_000_000,end_mach_ns=BASE+270_000_000))
        report=summarize([old,row],BASE,[(.25,.26)])
        self.assertEqual(report['diagnostic_versions'],[1,2])
        self.assertEqual(report['operations']['host_demux_call']['maximum']['duration_ms'],1)
        self.assertEqual(report['operations']['host_demux_interarrival']['long_events'][0]['gap_overlaps'],[dict(gap_index=0,overlap_ms=10)])
        self.assertEqual(report['operations']['renderer_sleep_overshoot']['count'],6)
        self.assertIsNone(report['operations']['native_append']['maximum'])
    def test_host_readbacks_and_overflow_roundtrip_without_inference(self):
        row=fixture_v2();d=row['stall_diagnostics'];d['dropped_host_threads']=3
        thread=dict(thread_id=42,observed_mach_ns=BASE+10,identity_status=0,qos_status=0,qos_class=9,relative_priority=-2)
        unknown=dict(thread_id=0,observed_mach_ns=BASE+12,identity_status=-1,qos_status=-1,qos_class=-1,relative_priority=-1)
        d['host_threads']=[thread,unknown]
        later=json.loads(json.dumps(row));later['stall_diagnostics']['revision']=3;later['stall_diagnostics']['dropped_host_threads']=0
        report=summarize([row,later],BASE)
        self.assertEqual(report['host_thread_observations'],[thread,unknown])
        self.assertEqual(report['dropped_host_threads'],3)
    def test_host_schema_is_strict_bounded_and_unavailable_has_no_data(self):
        thread=dict(thread_id=42,observed_mach_ns=BASE,identity_status=0,qos_status=0,qos_class=9,relative_priority=0)
        for edit in [lambda d:d.update(host_threads=[thread]*5),
                     lambda d:d.update(host_threads=[thread,thread]),
                     lambda d:d.update(host_threads=[dict(thread,thread_id=True)]),
                     lambda d:d.update(host_threads=[dict(thread,qos_class='9')]),
                     lambda d:d.update(host_threads=[dict(thread,relative_priority=2**31)]),
                     lambda d:d.update(dropped_host_threads=-1),
                     lambda d:d.pop('host_threads')]:
            row=fixture_v2();edit(row['stall_diagnostics'])
            with self.assertRaises(ValueError):summarize([row],BASE)
        row=fixture_v2();d=row['stall_diagnostics'];d['available']=False
        for k in ('revision','dropped_contention','dropped_events'):d[k]=0
        d['long_events']=[]
        for m in d['operations'].values():
            for k in m:m[k]=0
        summarize([row],BASE)
        d['host_threads']=[thread]
        with self.assertRaises(ValueError):summarize([row],BASE)
    def test_v1_shape_stays_exact_and_v2_requires_all_components(self):
        for version,edit in [(1,lambda d:d['operations'].update({'host_demux_call':{}})),
                             (2,lambda d:d['operations'].pop('native_append'))]:
            row=fixture() if version==1 else fixture_v2();edit(row['stall_diagnostics'])
            with self.assertRaises(ValueError):summarize([row],BASE)

def override_fixture():
    return dict(version=1,enabled=True,active=True,allowed=True,failed=False,terminal=False,owner_running=True,
        generation_before=5,generation_after=5,control_revision=2,owner_id=7,target_id=42,target_name='PAPlayer',
        requested_class=25,requested_relative_priority=0,start_attempts=1,starts=1,end_attempts=0,ends=0,
        ignored_preparation=2,blocked_through_revision=1,outstanding=1,max_outstanding=1,
        last_start_mach_ns=BASE,last_end_mach_ns=0,last_end_owner_id=0,last_start_status=0,last_end_status=-1,reason='none')

class HostOverrideTests(unittest.TestCase):
    def test_active_and_consumed_cleanup_evidence_roundtrip(self):
        row=fixture_v2();row['host_override']=override_fixture()
        closed=json.loads(json.dumps(row));closed['stall_diagnostics']['revision']=3
        closed['host_override'].update(active=False,allowed=False,terminal=True,owner_running=False,
            generation_before=6,generation_after=6,end_attempts=1,ends=1,outstanding=0,
            last_end_mach_ns=BASE+10,last_end_owner_id=7,last_end_status=0)
        report=summarize([row,closed],BASE)
        self.assertEqual(report['host_override_observations'],[row['host_override'],closed['host_override']])
        self.assertEqual(summarize([fixture()],BASE)['host_override_observations'],[])
    def test_invalid_token_or_target_claim_rejected(self):
        for change in [dict(outstanding=2),dict(starts=0),dict(ends=2),dict(target_name='JobWorker'),
                       dict(owner_id=42),dict(active=True,failed=True),dict(owner_running=False),
                       dict(last_start_status=True),dict(requested_class=33),dict(extra=1)]:
            row=fixture_v2();row['host_override']=dict(override_fixture(),**change)
            with self.assertRaises(ValueError):summarize([row],BASE)

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
