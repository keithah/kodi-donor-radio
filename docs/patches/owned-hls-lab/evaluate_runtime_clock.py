#!/usr/bin/env python3
"""Compare recorded runtime outputs with independent waveform matches; never run an estimator."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

RATE = 48000
THRESHOLD_SECONDS = 1024 / RATE
FRESH_NS = 100_000_000
EXPECTED_URL = "http://127.0.0.1:18789/live/index.m3u8"
EXPECTED_SHA256 = "7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalized(value):
    # Match strict runtime JSON emission while retaining the original bytes hash.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def query_time(row):
    fields = ('mach_absolute_ns', 'delay_query_start_steady_ns', 'delay_query_end_steady_ns', 'steady_ns')
    if any(type(row.get(field)) is not int for field in fields):
        return None
    return row['mach_absolute_ns'] + (row['delay_query_start_steady_ns'] + row['delay_query_end_steady_ns']) // 2 - row['steady_ns']


def runtime_reasons(record, launch):
    invalid = list(record.get('reasons') or [])
    estimate = record.get('estimate') or {}
    if record.get('valid') is not True:
        invalid = invalid or ['runtime_invalid_without_reason']
    if estimate.get('valid') is not True:
        invalid.extend(estimate.get('reasons') or ['estimate_invalid'])
    if not finite(estimate.get('seconds')):
        invalid.append('estimate_seconds_unavailable')
    query, received, evaluated = (record.get(k) for k in ('query_host_ns', 'received_ns', 'evaluated_ns'))
    if type(query) is not int or type(received) is not int or type(evaluated) is not int or received < query or evaluated < received:
        invalid.append('invalid_runtime_timing')
    elif received-query > FRESH_NS or evaluated-query > FRESH_NS:
        invalid.append('query_freshness')
    if type(query) is int and query < launch['launch_ns']:
        invalid.append('before_launch')
    return list(dict.fromkeys(invalid))


def evaluate(analysis, native_lines, runtime_rows):
    """native_lines retain original bytes (including optional newline) for identity proof."""
    launches = [r for r in runtime_rows if r.get('kind') == 'runtime_clock_launch']
    if len(launches) != 1:
        raise ValueError('exactly one runtime launch manifest required')
    launch = launches[0]
    if (not isinstance(launch.get('launch_id'), str) or not launch['launch_id']
            or type(launch.get('launch_ns')) is not int
            or launch.get('selected_url') != EXPECTED_URL
            or launch.get('estimator_sha256') != EXPECTED_SHA256
            or launch.get('production_eligible') is not False):
        raise ValueError('invalid runtime launch manifest')
    for record in runtime_rows:
        if record.get('kind', '').startswith('runtime_clock_'):
            if record.get('launch_id') != launch['launch_id']:
                raise ValueError('mixed runtime launch identity')
            if 'launch_ns' in record and record['launch_ns'] != launch['launch_ns']:
                raise ValueError('mixed runtime launch timestamp')
    native = []
    clocks = []
    for line in native_lines:
        raw = line.rstrip(b'\r\n')
        row = json.loads(raw)
        native.append((row, hashlib.sha256(raw).hexdigest()))
        if row.get('kind') == 'clock_sample':
            clocks.append(len(native) - 1)
    recorded = {}
    for record in runtime_rows:
        if record.get('kind') != 'runtime_clock_input':
            continue
        identity = record.get('identity') or {}
        line = identity.get('ledger_line')
        if type(line) is not int or not 1 <= line <= len(native):
            raise ValueError('runtime native ledger line missing or out of range')
        index = line - 1
        row, digest = native[index]
        if row.get('kind') != 'clock_sample' or normalized(record.get('input')) != normalized(row) or identity.get('row_sha256') != digest:
            raise ValueError('runtime native row identity mismatch')
        if index in recorded:
            raise ValueError('duplicate runtime native query row')
        schema_invalid = (record.get('valid') is False and
                          'invalid_schema' in (record.get('reasons') or []))
        if record.get('query_host_ns') is None and schema_invalid:
            pass  # Preserve unusable native inputs as invalid evidence, never a clock point.
        elif record.get('query_host_ns') != query_time(row) or query_time(row) is None:
            raise ValueError('runtime query timestamp mismatch')
        recorded[index] = record
    samples = []
    seen = set()
    reasons = Counter()
    for point in analysis['clock']['samples']:
        if not point['qualified']:
            continue
        ordinal = point['ledger_row']
        if type(ordinal) is not int or not 0 <= ordinal < len(clocks) or ordinal in seen:
            raise ValueError('duplicate or out-of-range analyzer native query row')
        seen.add(ordinal)
        index = clocks[ordinal]
        native_row = native[index][0]
        if query_time(native_row) is None or point['query_host_ns'] != query_time(native_row):
            raise ValueError('analyzer query timestamp mismatch')
        channels = point['channels']
        if len(channels) != 2 or any(not finite(c.get('source_center_sample')) for c in channels):
            raise ValueError('qualified analyzer point lacks independent stereo source alignment')
        result = dict(ledger_row=ordinal, ledger_line=index + 1, query_host_ns=point['query_host_ns'], status='missing', reasons=['runtime_missing'], error_seconds=None, query_to_receive_ms=None, query_to_evaluate_ms=None)
        record = recorded.get(index)
        if record is not None:
            estimate = record.get('estimate') or {}
            invalid = runtime_reasons(record, launch)
            seconds = estimate.get('seconds')
            received, evaluated = record.get('received_ns'), record.get('evaluated_ns')
            if type(record.get('query_host_ns')) is int and type(received) is int and type(evaluated) is int:
                result['query_to_receive_ms'] = (received-point['query_host_ns']) / 1e6
                result['query_to_evaluate_ms'] = (evaluated-point['query_host_ns']) / 1e6
            result.update(status='invalid' if invalid else 'valid', reasons=list(dict.fromkeys(invalid)), seconds=seconds if finite(seconds) else None)
            if finite(seconds):
                result['error_seconds'] = [seconds-c['source_center_sample']/RATE for c in channels]
        reasons.update(result['reasons'])
        result['threshold_violation'] = bool(result['error_seconds'] and any(abs(e) > THRESHOLD_SECONDS for e in result['error_seconds']))
        samples.append(result)
    counts = Counter(x['status'] for x in samples)
    errors = [e for x in samples for e in (x['error_seconds'] or [])]
    violations = sum(x['threshold_violation'] for x in samples)
    all_records = list(recorded.values())
    all_reasons = Counter(r for record in all_records for r in runtime_reasons(record, launch))
    all_valid = sum(not runtime_reasons(record, launch) for record in all_records)
    def ages(field):
        values = [(r[field]-r['query_host_ns']) / 1e6 for r in all_records if type(r.get(field)) is int and type(r.get('query_host_ns')) is int]
        return dict(min=min(values), max=max(values)) if values else dict(min=None, max=None)
    return dict(kind='independent_recorded_runtime_clock_evaluation', mapping_proven=False,
                comparison='Exact native clock ordinal, line bytes hash and query midpoint; recorded runtime estimates only; no estimator replay or fitted offset.',
                launch=launch, all_runtime_rows=dict(total=len(all_records), valid=all_valid, invalid=len(all_records)-all_valid, invalid_reasons=dict(all_reasons)),
                age_ms=dict(query_to_receive=ages('received_ns'), query_to_evaluate=ages('evaluated_ns')),
                threshold_seconds=THRESHOLD_SECONDS, all_independently_qualified=len(samples), runtime_valid=counts['valid'], runtime_invalid=counts['invalid'], runtime_missing=counts['missing'], invalid_reasons=dict(reasons), violation_count=violations,
                error_range_seconds=[min(errors), max(errors)] if errors else None,
                gate_passed=bool(samples) and counts['valid'] == len(samples) and not violations,
                samples=samples,
                limits=['A passing diagnostic gate does not prove production mapping or physical speaker timing.', 'Independent audio acceptance and runtime control/snapshot acceptance remain separate checks.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', '--comparison-report', dest='analysis', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(json.loads(args.analysis.read_text()), args.ledger.read_bytes().splitlines(keepends=True), [json.loads(line) for line in args.runtime.read_text().splitlines()])
    report['evidence'] = {key: dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for key, path in [('analysis', args.analysis), ('native_ledger', args.ledger), ('runtime_ledger', args.runtime)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'samples'}, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
