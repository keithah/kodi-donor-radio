#!/usr/bin/env python3
"""Run the fixed-count native HAL authorization benchmark outside capture.

The harness owns every native response. This driver supplies a fully populated
gain directory, 32,768 exact-next authorization requests, and five final-stage
probes while retaining all request/response bytes and timing samples.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


NONCE = 'a' * 32
INCARNATION = 'b' * 32
TIMELINE = 'c' * 64


def mach_host_ns():
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    info = Timebase()
    if lib.mach_timebase_info(ctypes.byref(info)) or not info.denom:
        raise RuntimeError('mach_timebase')
    lib.mach_absolute_time.restype = ctypes.c_uint64
    return lib.mach_absolute_time() * info.numer // info.denom


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def write_private_bytes(path, raw):
    temporary = path.with_name('.' + path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(descriptor, raw) != len(raw):
            raise OSError('short_write')
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    os.rename(temporary, path)


def write_private(path, value):
    write_private_bytes(path, canonical(value))


def query(now, sequence=1):
    return dict(kind='hal_query', output_backend='hal_callback_v1', hal_contract_version=1,
                hal_phase='clock', hal_clock_version=1, nonce=NONCE, incarnation=INCARNATION,
                timeline_sha256=TIMELINE, sequence=sequence, query_start_ns=now, query_end_ns=now,
                query_host_ns=now, qualified=True, readiness=False, initial_failed=False,
                reason='qualified', source_frame=4096, segment_sequence=21,
                segment_sha256='d' * 64, applied_sequence=21, applied_sha256='e' * 64,
                source_zero_sequence=19, source_zero_sha256='f' * 64, source_zero_ticks=max(0, now - 64000000),
                timebase_numer=1, timebase_denom=1, initial_deadline_ns=now + 120000000,
                native_gate=1, effective_intent=2, device_generation=1, stream_generation=1,
                playback_epoch=2, gain_epoch=1, readiness_count=9, consistent_count=9,
                applied_request_start_ns=now - 10000000, applied_request_end_ns=now - 9000000,
                applied_issue_ns=now - 8000000, applied_expiry_ns=now + 500000000)


def request(row, number, start, stage='tracker'):
    fields = ('query_host_ns', 'query_start_ns', 'query_end_ns', 'sequence', 'segment_sha256',
              'native_gate', 'effective_intent', 'playback_epoch', 'gain_epoch',
              'device_generation', 'stream_generation', 'source_zero_sequence',
              'source_zero_sha256', 'source_zero_ticks', 'timebase_numer', 'timebase_denom',
              'initial_deadline_ns')
    value = {key: row[key] for key in fields}
    value['row_sequence'] = value.pop('sequence')
    value['row_sha256'] = hashlib.sha256(canonical(row).rstrip(b'\n')).hexdigest()
    value['native_generation'] = value.pop('native_gate')
    value.update(nonce=NONCE, incarnation=INCARNATION, timeline_sha256=TIMELINE,
                 output_backend='hal_callback_v1', hal_phase='clock', hal_clock_version=1,
                 request_id=number, expected_generation=1, stage=stage, request_start_ns=start,
                 deadline_ns=start + 20000000, initial=0, decision_id='1' * 64,
                 event_digest='', parent_id=0, parent_sha256='', decision_ns=0, queue_ns=0)
    return value


def gain(revision):
    return dict(nonce=NONCE, incarnation=INCARNATION, timeline_sha256=TIMELINE,
                output_backend='hal_callback_v1', hal_contract_version=1, device_generation=1,
                revision=revision, effective_epoch=1, request_start_ns=revision,
                request_end_ns=revision, issue_ns=revision, expiry_ns=revision + 500000000,
                volume=100, muted=False, multiplier=1)


def command(revision):
    if revision == 1:
        state = 'waiting'
    elif revision == 2:
        state = 'play'
    else:
        state = 'pause' if revision % 2 else 'resume'
    return ('v1 %s %s %d %s %d\n' % (NONCE, INCARNATION, revision, state, revision)).encode('ascii')


def wait_for(path, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(.001)
    raise TimeoutError(str(path))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--harness', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('refusing_existing_output')
    args.output.mkdir(mode=0o700, parents=True)
    session = args.output / 'session'
    session.mkdir(mode=0o700)
    started = mach_host_ns()
    seed_query = query(started)
    write_private(session / 'query.json', seed_query)
    write_private(session / 'seed-request.json', request(seed_query, 1, started))
    # The real Python transport creates this pinned child before native Open.
    (session / 'authorization').mkdir(mode=0o700)
    # This deliberately fills the retained gain validation set, without asking
    # the native owner to apply a new gain command during the authorization run.
    for index in range(1, 6001):
        write_private(session / ('gain-%06d.json' % index), gain(index))
    for index in range(1, 65):
        write_private_bytes(session / ('command-%06d.txt' % index), command(index))
    process = subprocess.Popen([str(args.harness), str(session), 'benchmark'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        setup_deadline = time.monotonic() + 15
        while not (session / 'populated').exists() and time.monotonic() < setup_deadline:
            if process.poll() is not None:
                out, err = process.communicate()
                raise RuntimeError('native_setup_exit_%s:%s:%s' % (process.returncode, out, err))
            time.sleep(.001)
        if not (session / 'populated').exists():
            out, err = process.communicate(timeout=3)
            raise RuntimeError('native_setup_timeout:%s:%s' % (out, err))
        final_now = mach_host_ns()
        # Native lookup is by the immutable original query sequence. The
        # preload row remains sequence 1, so the post-population probes bind
        # a distinct row instead of accidentally selecting that stale cache row.
        final = query(final_now, sequence=2)
        write_private(session / 'final-query.json', final)
        authorization = session / 'authorization'
        for number in range(32764, 32769):
            write_private(authorization / ('request-%06d.json' % number), request(final, number, mach_host_ns()))
            response_path = authorization / ('response-%06d.json' % number)
            response_deadline = time.monotonic() + 15
            while not response_path.exists() and time.monotonic() < response_deadline:
                if process.poll() is not None:
                    out, err = process.communicate()
                    raise RuntimeError('native_response_exit_%s:%s:%s' % (process.returncode, out, err))
                time.sleep(.001)
            if not response_path.exists():
                raise TimeoutError(str(response_path))
        out, err = process.communicate(timeout=20)
        if process.returncode:
            raise RuntimeError('native_harness_%s:%s' % (process.returncode, err))
        responses = [json.loads((authorization / ('response-%06d.json' % n)).read_text()) for n in range(32764, 32769)]
        cadence = [json.loads(line) for line in (session / 'native-cadence.jsonl').read_text().splitlines()]
        latency = dict(
            request_to_action_max_ns=max(row['check_end_ns'] - row['request_start_ns'] for row in responses),
            native_bracket_max_ns=max(row['check_end_ns'] - row['check_start_ns'] for row in responses),
            reply_action_age_max_ns=0,
            control_max_ns=max(row['control_end_ns'] - row['start_ns'] for row in cadence),
            gain_max_ns=max(row['gain_end_ns'] - row['control_end_ns'] for row in cadence),
            gain_renewal_observed=any(row.get('gain_pending') == 1 for row in cadence))
        result = dict(schema='owned-hal-authorization-benchmark-v1', harness=str(args.harness),
                      authorization_requests_prepopulated=32763, authorization_requests_final=5,
                      gain_records=6000, responses=responses, cadence=cadence,
                      all_final_authorized=all(row['authorized'] for row in responses), latency=latency,
                      limits=dict(request_to_action_max_ns=20000000, native_bracket_max_ns=5000000),
                      native_stdout=out, native_stderr=err)
        write_private(args.output / 'result.json', result)
        if (not result['all_final_authorized'] or latency['request_to_action_max_ns'] > 20000000
                or latency['native_bracket_max_ns'] > 5000000 or not latency['gain_renewal_observed']):
            raise RuntimeError('benchmark_limits_or_authorization_failed')
        print(json.dumps(result, sort_keys=True))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


if __name__ == '__main__':
    main()
