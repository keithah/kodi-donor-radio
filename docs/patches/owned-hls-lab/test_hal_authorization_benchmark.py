import importlib.util
import hashlib
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'benchmark', Path(__file__).with_name('run_hal_authorization_benchmark.py'))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_seed_request_stays_in_native_unsigned_schema_at_early_monotonic_time():
    row = benchmark.query(1)
    request = benchmark.request(row, 1, 1)
    assert row['source_zero_ticks'] == 0
    assert request['source_zero_ticks'] == 0
    assert len(request) == 36
    assert request['request_id'] == 1


def test_maximum_control_history_is_contiguous_and_parseable():
    records = [benchmark.command(index).decode('ascii').split() for index in range(1, 65)]
    assert [int(record[3]) for record in records] == list(range(1, 65))
    assert records[0][4] == 'waiting'
    assert records[1][4] == 'play'
    assert all(record[4] in ('pause', 'resume') for record in records[2:])


def test_final_probe_uses_a_distinct_native_query_identity():
    preload = benchmark.query(1_000_000_000, sequence=1)
    final = benchmark.query(1_010_000_000, sequence=2)
    assert benchmark.request(preload, 1, 1_000_000_000)['row_sequence'] == 1
    assert benchmark.request(final, 32764, 1_010_000_000)['row_sequence'] == 2


def test_staged_request_carries_exact_parent_response_hash():
    row=benchmark.query(1_000_000_000,sequence=2)
    tracker=benchmark.request(row,32754,1_000_000_000,'tracker',event_digest='e'*64)
    tracker['response_sha256']='a'*64
    queue=benchmark.request(row,32755,1_000_000_001,'queue',event_digest='e'*64,parent=tracker,decision_ns=9)
    assert queue['parent_id']==32754 and queue['parent_sha256']=='a'*64
    queue['response_sha256']='b'*64
    dispatch=benchmark.request(row,32756,1_000_000_002,'dispatch',event_digest='e'*64,parent=queue,decision_ns=9,queue_ns=10)
    assert dispatch['parent_id']==32755 and dispatch['parent_sha256']=='b'*64 and dispatch['queue_ns']==10


def test_response_is_validated_before_an_action_timestamp_can_be_taken():
    expected=benchmark.request(benchmark.query(1_000_000_000,sequence=2),32754,1_000_000_001)
    response=dict(expected,request_sha256=hashlib.sha256(benchmark.canonical(expected)).hexdigest(),authorized=True,
                  check_start_ns=1_000_000_002,check_end_ns=1_000_000_003,callback_entry_ns=1_000_000_002,
                  lease_expiry_ns=2_000_000_000,source_end_ns=0,actual_gate=1,actual_intent=2,
                  applied_sequence=21,applied_sha256='a'*64,reason='authorized')
    assert benchmark.validate_response(benchmark.canonical(response),expected)['authorized'] is True
    response['request_id']=999
    try:benchmark.validate_response(benchmark.canonical(response),expected)
    except ValueError as exc:assert str(exc)=='benchmark_response_schema'
    else:raise AssertionError('mismatched reply accepted')


def test_pre_slot_deadline_rejects_delayed_exact_next_authorization():
    benchmark.require_slot_budget(100,100+benchmark.AUTHORIZATION_BUDGET_NS)
    try:benchmark.require_slot_budget(100,101+benchmark.AUTHORIZATION_BUDGET_NS)
    except TimeoutError as exc:assert str(exc)=='benchmark_authorization_slot_timeout'
    else:raise AssertionError('pre-lock latency excluded from action budget')


def test_maximum_gain_history_leaves_one_exact_successor_for_renewal():
    assert benchmark.gain(6000)['revision'] == 6000
    assert benchmark.gain(6000)['effective_epoch'] == 1


def test_mach_clock_library_and_timebase_are_initialized_once(monkeypatch):
    class Function:
        def __init__(self, callback):
            self.callback = callback
            self.restype = None
        def __call__(self, *args):
            return self.callback(*args)

    class Library:
        def __init__(self):
            self.ticks = 0
            def timebase(pointer):
                info = benchmark.ctypes.cast(pointer, benchmark.ctypes.POINTER(benchmark._Timebase)).contents
                info.numer, info.denom = 3, 2
                return 0
            self.mach_timebase_info = Function(timebase)
            self.mach_absolute_time = Function(self.absolute)
        def absolute(self):
            self.ticks += 1
            return self.ticks

    library = Library()
    loads = []
    monkeypatch.setattr(benchmark.ctypes, 'CDLL', lambda path: loads.append(path) or library)
    benchmark._mach_clock.cache_clear()
    try:
        assert benchmark.mach_host_ns() == 1
        assert benchmark.mach_host_ns() == 3
        assert loads == ['/usr/lib/libSystem.B.dylib']
    finally:
        benchmark._mach_clock.cache_clear()
