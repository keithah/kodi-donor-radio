#!/usr/bin/env python3
"""Audit recorded owned clock/decision/authorization/HTTP evidence against capture.

No estimator is imported or replayed. Every claimed qualified query and event
uses its actual Mach time and independent stereo waveform matching. The existing
whole-audio analyzer supplies capture-only anchors within each audible span.
"""
import argparse
import bisect
from collections import Counter, OrderedDict
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
from fixture_beacons import FIRST_PACKET,PCM_SHA,PLACEMENTS,EVENTS,URL,TOTAL,load_contract

RATE=48000
TOLERANCE=1024/RATE
DECISION_BOUND=.1+.02+TOLERANCE
ESTIMATOR_SHA='7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87'
FRACTIONS=dict(zip(EVENTS,(0,0,.25,.5,.75,1)))
# Accounting bounds only; these never extend clock or beacon freshness.
SHUTDOWN_TAIL_MAX_ROWS=64
SHUTDOWN_TAIL_MAX_NS=3_000_000_000
MATCHER_VERSION='owned-fractional-sinc96-v2'
MATCHER_METHOD=dict(version=MATCHER_VERSION,heldout_frames=1920,training_frames=2400,
                    training_separation_frames=2400,sinc_taps=96,sinc_window='normalized_Blackman',
                    phase_bound_samples=[-1,1],coarse_phase_step_samples=.05,fine_phase_step_samples=.005,
                    phase_cache_limit=512,maximum_training_attempts=2,quiet_bin_frames=24,
                    quiet_rms_floor=1e-4,interior_filter_guard_frames=96,
                    minimum_stereo_correlation=.99,maximum_stereo_difference_samples=1,
                    clock_metadata_used_for_alignment=False,
                    continuity_scope='bounded_windows_only_separate_whole_audio_gate_required')


def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def opaque(value):return hashlib.sha256(value.encode()).hexdigest()[:16]


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def query_time(row):
    keys=('mach_absolute_ns','delay_query_start_steady_ns','delay_query_end_steady_ns','steady_ns')
    if any(type(row.get(k)) is not int for k in keys):return None
    return row[keys[0]]+(row[keys[1]]+row[keys[2]])//2-row[keys[3]]


def expected_events(first_qualified):
    names=['a','c','d','e']
    if finite(first_qualified) and 0<=first_qualified<=.25:names.insert(0,'preroll')
    return {(name,event) for name in names for event in EVENTS}


def event_timing(threshold,actual_decision,network_delay):
    delta=actual_decision-threshold
    return dict(threshold_seconds=threshold,decision_source_seconds=actual_decision,
                source_threshold_to_decision_seconds=delta,early=delta<0,
                early_beyond_clock_tolerance=delta < -TOLERANCE,
                violation=delta < -TOLERANCE or delta>DECISION_BOUND,
                decision_to_http_receipt_seconds=network_delay)


def account_native(native,rows):
    """Partition every clock row into ordered consumption or an explicit suffix.

    Invalid provider snapshots may omit their source identity. The monitor still
    records that identity on each input and emits the returned snapshots in the
    same order, so compare the complete ordered payloads without changing Task2.
    """
    errors=[]
    inputs=[r for r in rows if r.get('kind')=='owned_clock_input']
    snapshots=[r for r in rows if r.get('kind')=='owned_clock_snapshot']
    consumed=[r.get('row_sequence') for r in inputs]
    if consumed!=list(range(1,len(inputs)+1)):errors.append('native_input_order')
    if len(inputs)!=len(snapshots) or any(
        record.get('snapshot')!={k:v for k,v in snapshot.items() if k not in ('kind','observed')}
        for record,snapshot in zip(inputs,snapshots)):
        errors.append('input_snapshot_accounting')
    for record in inputs:
        entry=native.get(record.get('row_sequence'))
        if entry is None or entry[0]!=record.get('row_sha256'):errors.append('input_native_identity')
    consumed_set=set(consumed)
    missing=[seq for seq in native if seq not in consumed_set]
    excluded=[]
    stops=[r for r in rows if r.get('kind')=='owned_callback' and r.get('event')=='stop']
    exits=[r for r in rows if r.get('kind')=='owned_monitor_exit']
    stop_ns=stops[0].get('issued_mach_ns') if len(stops)==1 else None
    exit_ns=exits[0].get('checked_mach_ns') if len(exits)==1 else None
    def terminal(row):
        own=row.get('ownership',{})
        return (own.get('eligible') is False and row.get('failed') is False
                and (row.get('drained') is True or row.get('control_state') in ('stop','drained')
                     or own.get('reason') in ('drain','close','control_stop')))
    suffix_ok=False
    if (missing and not errors and inputs and missing==list(range(len(inputs)+1,len(native)+1))
            and len(missing)<=SHUTDOWN_TAIL_MAX_ROWS and len(exits)==1 and exits[0].get('retired') is True
            and type(stop_ns) is int and type(exit_ns) is int
            and 0<=stop_ns<=exit_ns<=stop_ns+SHUTDOWN_TAIL_MAX_NS):
        last=native[consumed[-1]][1];last_query=query_time(last)
        last_received=inputs[-1].get('received_mach_ns')
        suffix_ok=(terminal(last) and (inputs[-1].get('snapshot') or {}).get('valid') is False
                   and type(last_query) is int and type(last_received) is int
                   and last_query<=last_received<=exit_ns)
        previous=last_query
        for seq in missing:
            row=native[seq][1];query=query_time(row)
            if (not suffix_ok or not terminal(row) or type(query) is not int
                    or not max(stop_ns,previous)<query<=stop_ns+SHUTDOWN_TAIL_MAX_NS):
                suffix_ok=False;break
            previous=query
    def details(seq):
        digest,row=native[seq];own=row.get('ownership',{})
        return dict(row_sequence=seq,row_sha256=digest,query_host_ns=query_time(row),
                    native_reason=own.get('reason'),eligible=own.get('eligible'),
                    control_state=row.get('control_state'),drained=row.get('drained'),failed=row.get('failed'))
    if suffix_ok:
        excluded=[dict(details(seq),exclusion_reason='bounded_terminal_shutdown_suffix') for seq in missing]
    elif missing:errors.append('native_input_missing')
    report=dict(native_clock_rows=len(native),consumed_sequences=consumed,excluded_terminal_tail=excluded,
                unaccounted_rows=[] if suffix_ok else [details(seq) for seq in missing],
                stop_mach_ns=stop_ns,monitor_exit_mach_ns=exit_ns,
                shutdown_accounting_max_rows=SHUTDOWN_TAIL_MAX_ROWS,
                shutdown_accounting_max_ns=SHUTDOWN_TAIL_MAX_NS)
    return errors,report


def join_evidence(native_lines,rows,receipts):
    """Validate exact recorded chain without calculating a clock estimate."""
    errors=[];native={};snapshots={};events=[];qualified=[]
    launches=[r for r in rows if r.get('kind')=='owned_launch']
    if len(launches)!=1:raise ValueError('one_owned_launch_required')
    launch=launches[0]
    if launch.get('output_backend')=='hal_callback_v1':return join_hal_evidence(native_lines,rows,receipts)
    if (launch.get('selected_url')!=URL or launch.get('first_packet_sha256')!=FIRST_PACKET
            or launch.get('estimator_sha256')!=ESTIMATOR_SHA or launch.get('production_eligible') is not False):
        raise ValueError('owned_launch_identity')
    nonce=None;incarnation=None;previous=0
    for line in native_lines:
        raw=line.rstrip(b'\r\n');row=json.loads(raw)
        if row.get('kind')!='clock_sample':continue
        own=row.get('ownership',{});seq=own.get('observation_sequence')
        if type(seq) is not int or seq!=previous+1:errors.append('native_sequence');continue
        previous=seq;digest=hashlib.sha256(raw).hexdigest();native[seq]=(digest,row)
        if nonce is None:nonce=own.get('nonce');incarnation=own.get('incarnation')
        if (own.get('nonce')!=nonce or own.get('incarnation')!=incarnation
                or own.get('timeline_sha256')!=launch.get('timeline_sha256')):errors.append('native_owner_identity')
    if not nonce or opaque(nonce)!=launch.get('session_id'):errors.append('launch_native_identity')
    for row in rows:
        if row.get('kind')!='owned_clock_snapshot' or row.get('valid') is not True:continue
        seq=row.get('row_sequence');entry=native.get(seq)
        if seq in snapshots:errors.append('duplicate_snapshot')
        snapshots[seq]=row;qualified.append(row)
        if not entry or entry[0]!=row.get('row_sha256'):
            errors.append('snapshot_native_identity');continue
        original=entry[1];own=original['ownership']
        if (query_time(original)!=row.get('query_host_ns') or not finite(row.get('seconds'))
                or row.get('native_generation')!=own.get('state_generation_before')
                or row.get('native_generation')!=own.get('state_generation_after')
                or own.get('eligible') is not True or own.get('source_zero_confirmed') is not True):
            errors.append('snapshot_native_proof')
    inputs={}
    for record in rows:
        if record.get('kind')!='owned_clock_input':continue
        seq=record.get('row_sequence')
        if seq in inputs:errors.append('duplicate_clock_input')
        inputs[seq]=record
        if seq not in native or native[seq][0]!=record.get('row_sha256'):
            errors.append('input_native_identity')
        if (record.get('snapshot') or {}).get('valid') is True and seq not in snapshots:
            errors.append('qualified_input_snapshot_missing')
    for snapshot in qualified:
        record=inputs.get(snapshot['row_sequence'])
        if (record is None or record.get('row_sha256')!=snapshot['row_sha256']
                or record.get('snapshot')!= {k:v for k,v in snapshot.items() if k not in ('kind','observed')}):
            errors.append('snapshot_contemporaneous_input')
            continue
        query=snapshot['query_host_ns'];received=record.get('received_mach_ns');checked=snapshot.get('authorization_ns')
        if (any(type(t) is not int for t in (query,received,checked))
                or not launch['launch_mach_ns']<=query<=received<=checked<=query+100000000):
            errors.append('snapshot_freshness')
    accounting_errors,accounting=account_native(native,rows)
    errors.extend(accounting_errors)
    return finish_evidence(launch,nonce,errors,qualified,snapshots,accounting,rows,receipts)


HAL_PROOF_KEYS=('backend','query_start_ns','query_end_ns','segment_sha256','applied_sequence','applied_sha256',
    'effective_intent','playback_epoch','gain_epoch','device_generation','stream_generation','source_zero_sequence',
    'source_zero_sha256','source_zero_ticks','timebase_numer','timebase_denom','initial_deadline_ns')


def hal_segment_start(segment):
    """Exact callback host time for a segment, without a float conversion."""
    return Fraction(segment['output_ticks']*segment['timebase_numer'],segment['timebase_denom'])


def hal_covering_segments(segment_index,segment_starts,query_host_ns):
    """Return every overlapping predecessor at a query point in bounded time.

    Segments are chronological 512-frame callback records, but partial records
    can overlap their immediate predecessor.  `bisect_right` puts all starts
    at or before the query before `position`; checking the two immediately
    preceding records preserves overlap detection while avoiding O(N*M).
    """
    point=Fraction(query_host_ns);position=bisect.bisect_right(segment_starts,point)
    covering=[]
    for start,_,value in segment_index[max(0,position-2):position]:
        end=start+Fraction(value['copied_frames']*10**9,RATE)
        if start<=point<end:covering.append(value)
    return covering


def hal_source_frame(segment,query_host_ns):
    return segment['source_start']+int((Fraction(query_host_ns)-hal_segment_start(segment))*RATE/10**9)


def join_hal_evidence(native_lines,rows,receipts):
    """Join actual HAL raw rows. They are never rewritten as AE observations."""
    launches=[r for r in rows if r.get('kind')=='owned_launch']
    if len(launches)!=1:raise ValueError('one_owned_launch_required')
    launch=launches[0]
    if (launch.get('output_backend')!='hal_callback_v1' or launch.get('hal_phase')!='clock'
            or type(launch.get('hal_clock_version')) is not int or launch['hal_clock_version']!=1
            or launch.get('selected_url')!=URL or launch.get('first_packet_sha256')!=FIRST_PACKET
            or launch.get('production_eligible') is not False):raise ValueError('hal_launch_identity')
    native={};segments={};errors=[];nonce=incarnation=None;zero=None
    for line in native_lines:
        raw=line.rstrip(b'\r\n');row=json.loads(raw);kind=row.get('kind')
        if not isinstance(kind,str) or not kind.startswith('hal_'):continue
        if nonce is None:nonce=row.get('nonce');incarnation=row.get('incarnation')
        if (row.get('nonce')!=nonce or row.get('incarnation')!=incarnation
                or row.get('timeline_sha256')!=launch.get('timeline_sha256')):errors.append('native_owner_identity')
        digest=hashlib.sha256(raw).hexdigest()
        if kind=='hal_segment':
            seq=row.get('sequence')
            if type(seq) is not int or seq!=len(segments)+1:errors.append('hal_segment_order');continue
            segments[seq]=(digest,row)
            if row.get('copied_frames') and zero is None:zero=(digest,row)
        elif kind=='hal_query':
            seq=row.get('sequence')
            if type(seq) is not int or seq!=len(native)+1:errors.append('native_sequence');continue
            native[seq]=(digest,row)
            if any(row.get(k)!=launch.get(k) for k in ('output_backend','hal_phase','hal_clock_version')):errors.append('hal_query_binding')
            # A reference must already have been committed in the raw ledger.
            for field in ('segment','applied','source_zero'):
                sequence=row.get(field+'_sequence')
                if sequence and (sequence not in segments or segments[sequence][0]!=row.get(field+'_sha256')):
                    errors.append('hal_query_segment_identity')
    if not nonce or opaque(nonce)!=launch.get('session_id'):errors.append('launch_native_identity')
    inputs=[r for r in rows if r.get('kind')=='owned_clock_input']
    all_snapshots=[r for r in rows if r.get('kind')=='owned_clock_snapshot']
    consumed=[r.get('row_sequence') for r in inputs]
    if consumed!=list(range(1,len(inputs)+1)):errors.append('native_input_order')
    if consumed!=list(native):errors.append('native_input_missing')
    if len(inputs)!=len(all_snapshots):errors.append('input_snapshot_accounting')
    # Segment lookup is ordered by its source host tick.  A query can only be
    # covered by the immediate predecessor (or, at a shared edge, its next
    # neighbor); scanning every segment for every query both hid overlap bugs
    # and turned the final audit into O(N*M).
    segment_index=sorted((hal_segment_start(row),seq,row)
                         for seq,(_,row) in segments.items() if row.get('copied_frames'))
    segment_starts=[entry[0] for entry in segment_index]
    snapshots={};qualified=[]
    for record,snapshot in zip(inputs,all_snapshots):
        sequence=record.get('row_sequence');entry=native.get(sequence)
        if (entry is None or entry[0]!=record.get('row_sha256') or record.get('snapshot')!={k:v for k,v in snapshot.items() if k not in ('kind','observed')}):
            errors.append('input_native_identity');continue
        digest,query=entry
        if snapshot.get('valid') is not True:continue
        snapshots[sequence]=snapshot;qualified.append(snapshot)
        try:
            start,end,q=(query[k] for k in ('query_start_ns','query_end_ns','query_host_ns'))
            if (any(type(t) is not int for t in (start,end,q)) or not 0<=end-start<=5_000_000
                    or q!=start+(end-start)//2 or query['qualified'] is not True
                    or snapshot['row_sha256']!=digest or snapshot['query_host_ns']!=q
                    or snapshot['native_generation']!=query['native_gate']
                    or any(snapshot[k]!=query[k] for k in HAL_PROOF_KEYS if k!='backend')
                    or snapshot['backend']!='hal_callback_v1'):raise ValueError('hal_snapshot_identity')
            covering=hal_covering_segments(segment_index,segment_starts,q)
            if len(covering)!=1 or covering[0]['sequence']!=query['segment_sequence']:raise ValueError('hal_query_coverage')
            segment=covering[0];source=hal_source_frame(segment,q)
            if (snapshot['seconds']!=source/RATE or source!=query['source_frame'] or segment['muted']
                    or not segment['playing'] or segment['applied_gain']<=0 or not segment['sample_zero_proven']):raise ValueError('hal_mapping_proof')
            applied=segments[query['applied_sequence']][1]
            for key in ('device_generation','stream_generation','playback_epoch','gain_epoch','native_gate'):
                if segment[key]!=query[key] or applied[key]!=query[key]:raise ValueError('hal_epoch_proof')
            if segment['applied_intent']!=query['effective_intent'] or applied['applied_intent']!=query['effective_intent']:raise ValueError('hal_intent_proof')
            for key in ('request_start_ns','request_end_ns','issue_ns','expiry_ns'):
                if query['applied_'+key]!=applied[key]:raise ValueError('hal_applied_lease_identity')
            received=record['received_mach_ns'];checked=snapshot['authorization_ns']
            if not q<=received<=checked<=q+100_000_000:raise ValueError('snapshot_freshness')
            if zero is None or zero[1]['source_start']!=0 or query['source_zero_sha256']!=zero[0]:raise ValueError('hal_source_zero')
            if len(qualified)==1:
                zero_start=hal_segment_start(zero[1])
                if (source<=1024 or q<=zero_start+Fraction(1024*10**9,RATE)
                        or checked>query['initial_deadline_ns'] or query['initial_deadline_ns']!=int(hal_segment_start(zero[1]))+120_000_000):
                    raise ValueError('hal_initial_opportunity')
        except (KeyError,ValueError,TypeError,ZeroDivisionError,OverflowError) as exc:
            errors.append(str(exc) if isinstance(exc,ValueError) else 'hal_proof_schema')
    accounting=dict(native_clock_rows=len(native),consumed_sequences=consumed,excluded_terminal_tail=[],
                    unaccounted_rows=[n for n in native if n not in consumed],hal_segment_rows=len(segments))
    audit_hal_authorization_chain(rows,errors)
    return finish_evidence(launch,nonce,errors,qualified,snapshots,accounting,rows,receipts,hal=True)


def audit_hal_authorization_chain(rows,errors):
    """Require every tracker/queue/dispatch decision to have one native proof.

    The raw native response echoes its request.  That lets the offline
    evaluator independently bind ledger records to the native request/reply
    pair, including each proof hash and both parent links; a Python-only row
    can therefore never manufacture a delivery authorization.
    """
    probes=[row for row in rows if row.get('kind')=='owned_hal_probe']
    proofs={}
    for probe in probes:
        response=probe.get('response');proof_id=response.get('request_id') if isinstance(response,dict) else None
        digest=probe.get('response_sha256')
        if (type(proof_id) is not int or type(digest) is not str or len(digest)!=64
                or response.get('stage') not in ('tracker','queue','dispatch')
                or digest in proofs or proof_id in proofs):
            errors.append('hal_native_proof_schema');continue
        if response.get('request_sha256')!=probe.get('request_sha256'):
            errors.append('hal_native_request_reply_binding');continue
        proofs[proof_id]=(digest,response)
    def proof(record,stage,parent=None):
        proof_id=record.get('proof_id');item=proofs.get(proof_id)
        if item is None or item[0]!=record.get('proof_sha256'):
            errors.append('hal_stage_native_proof');return None
        _,response=item
        if response.get('stage')!=stage or response.get('decision_id')!=record.get('decision_id'):
            errors.append('hal_stage_native_binding');return None
        if 'authorized' in record and response.get('authorized')!=record.get('authorized'):
            errors.append('hal_stage_authorization_binding');return None
        if parent is not None and (response.get('parent_id')!=parent[0] or response.get('parent_sha256')!=parent[1]):
            errors.append('hal_stage_parent_chain');return None
        return proof_id,item[0],response
    trackers={}
    for record in (row for row in rows if row.get('kind')=='owned_hal_tracker_authorization'):
        item=proof(record,'tracker')
        if item is None:continue
        if record.get('decision_id') in trackers or item[2].get('parent_id')!=0 or item[2].get('parent_sha256')!='':errors.append('hal_tracker_chain')
        trackers[record.get('decision_id')]=item
    queues={}
    for record in (row for row in rows if row.get('kind')=='owned_beacon_queue'):
        tracker=trackers.get(record.get('decision_id'));item=proof(record,'queue',tracker) if tracker else None
        if tracker is None:errors.append('hal_queue_tracker_missing')
        if item is None:continue
        if item[2].get('event_digest')!=record.get('event_digest') or item[2].get('decision_ns')!=record.get('decision_ns'):
            errors.append('hal_queue_event_binding')
        key=(record.get('decision_id'),record.get('placement_id'),record.get('event'))
        if key in queues:errors.append('hal_queue_replayed')
        queues[key]=item
    for record in (row for row in rows if row.get('kind')=='owned_beacon_authorization'):
        stage=record.get('authorization_stage')
        if stage not in ('queue','dispatch'):
            errors.append('hal_authorization_stage');continue
        key=(record.get('decision_id'),record.get('placement_id'),record.get('event'))
        queue=queues.get(key)
        parent=trackers.get(record.get('decision_id')) if stage=='queue' else queue
        item=proof(record,stage,parent) if queue and parent else None
        if queue is None:errors.append('hal_dispatch_queue_missing')
        if record.get('authorized') is True and stage!='dispatch':errors.append('hal_delivery_without_dispatch_proof')
        if item is None:continue
        if (item[2].get('event_digest')!=record.get('event_digest') or item[2].get('decision_ns')!=record.get('decision_ns')
                or (stage=='dispatch' and item[2].get('queue_ns')!=record.get('queue_ns'))):errors.append('hal_dispatch_event_binding')


def finish_evidence(launch,nonce,errors,qualified,snapshots,accounting,rows,receipts,hal=False):
    events=[]
    decisions=[r for r in rows if r.get('kind')=='ad_tracking']
    authorizations=[r for r in rows if r.get('kind')=='owned_beacon_authorization']
    key=lambda r:(r.get('placement_id'),r.get('event'))
    used_authorizations=set();used_receipts=set();decision_keys=set()
    by_opaque={opaque(name):name for name in PLACEMENTS}
    for decision in decisions:
        event_key=key(decision);name=by_opaque.get(event_key[0]);event=event_key[1]
        if event_key in decision_keys:errors.append('duplicate_decision')
        decision_keys.add(event_key)
        if name is None or event not in EVENTS:errors.append('unknown_decision');continue
        seq=decision.get('row_sequence');snapshot=snapshots.get(seq)
        if (snapshot is None or any(decision.get(k)!=snapshot.get(k) for k in
            ('row_sha256','query_host_ns','generation','native_generation'))
            or decision.get('mapped_seconds')!=snapshot.get('seconds')
            or decision.get('session_id')!=launch['session_id']
            or decision.get('creative_id')!=opaque(name)
            or type(decision.get('decision_ns')) is not int
            or type(snapshot.get('authorization_ns')) is not int
            or decision['decision_ns']<snapshot['authorization_ns']):
            errors.append('decision_snapshot_identity')
        if hal and snapshot and any(decision.get(k)!=snapshot.get(k) for k in HAL_PROOF_KEYS):errors.append('hal_decision_provenance')
        matched=[(i,r) for i,r in enumerate(authorizations) if key(r)==event_key]
        if len(matched)!=1:errors.append('missing_or_duplicate_authorization');continue
        index,auth=matched[0];used_authorizations.add(index)
        threshold=(PLACEMENTS[name][0]+(PLACEMENTS[name][1]-PLACEMENTS[name][0])*FRACTIONS[event])*1024/RATE
        if (any(auth.get(k)!=decision.get(k) for k in ('row_sequence','row_sha256','query_host_ns','generation',
                'native_generation','mapped_seconds','decision_ns','session_id')) or auth.get('threshold_seconds')!=threshold):
            errors.append('authorization_decision_identity')
        if hal:
            if any(auth.get(k)!=decision.get(k) for k in HAL_PROOF_KEYS):errors.append('hal_authorization_provenance')
            if name=='preroll' and event in ('impression','start'):
                deadline=decision.get('initial_deadline_ns')
                if type(deadline) is not int or any(type(r.get(k)) is not int or r[k]>deadline for r,k in ((decision,'decision_ns'),(auth,'queue_ns'))):errors.append('hal_initial_deadline')
        times=[decision.get('query_host_ns'),decision.get('decision_ns'),auth.get('authorization_ns')]
        chronology=all(type(t) is int for t in times) and 0<=times[0]<=times[1]<=times[2]
        if not chronology:errors.append('authorization_chronology')
        elif times[1]>times[0]+100000000:errors.append('decision_freshness')
        if type(auth.get('authorized')) is not bool:errors.append('authorization_result_schema')
        if auth.get('authorized') is True and (not chronology or times[2]>times[0]+100000000):
            errors.append('authorization_freshness')
        matched=[(i,r) for i,r in enumerate(receipts) if (r.get('session_id'),r.get('creative'),r.get('event'))==(launch['session_id'],name,event)]
        authorized=auth.get('authorized') is True
        if not authorized:
            if matched:errors.append('receipt_without_authorization')
            cancellation=dict(reason=auth.get('reason','delivery_authorization_rejected'),
                              checked_mach_ns=auth.get('authorization_ns'),
                              proof_stale_at_check=times[2]-times[0]>100000000 if chronology else None,
                              intervening_controls=[r for r in rows if r.get('kind')=='owned_callback'
                                  and type(r.get('issued_mach_ns')) is int and chronology
                                  and times[1]<r['issued_mach_ns']<=times[2]])
            events.append(dict(creative=name,event=event,delivered=False,decision=decision,authorization=auth,
                               threshold_seconds=threshold,cancellation=cancellation))
            continue
        if decision.get('reason')!='queued':errors.append('authorized_unqueued_decision')
        if len(matched)!=1:errors.append('missing_or_duplicate_receipt');continue
        index,receipt=matched[0];used_receipts.add(index)
        authorization_ns=auth.get('authorization_ns')
        if (receipt.get('accepted') is not True or receipt.get('duplicate') is not False or receipt.get('receipt_bound_exhausted') is True
                or receipt.get('status')!=204 or receipt.get('method')!='GET'
                or type(receipt.get('receipt_mach_ns')) is not int
                or type(authorization_ns) is not int
                or receipt['receipt_mach_ns']<authorization_ns):
            errors.append('invalid_http_receipt')
        events.append(dict(creative=name,event=event,delivered=True,decision=decision,authorization=auth,
                           receipt=receipt,threshold_seconds=threshold))
    if len(used_authorizations)!=len(authorizations):errors.append('orphan_authorization')
    if len(used_receipts)!=len(receipts):errors.append('unexpected_http_attempt')
    return dict(launch=launch,nonce=nonce,errors=sorted(set(errors)),qualified=qualified,events=events,native_accounting=accounting,
                http_receipts=receipts,tracking_decisions=decisions,authorizations=authorizations,
                callbacks=[r for r in rows if r.get('kind')=='owned_callback'])


class CaptureMatcher:
    """Audio-only fractional matching in uninterrupted actual-buffer regions."""
    def __init__(self,capture,reference,analysis,allow_fractional_endpoints=False):
        self.allow_fractional_endpoints=allow_fractional_endpoints
        import numpy as np
        from analyze_hls_capture import local_match
        self.np=np;self.local_match=local_match;self.analysis=analysis;self.cache={}
        self.audio=np.memmap(capture/'capture-analysis.f32',dtype='<f4',mode='r').reshape(-1,2)
        self.reference=np.memmap(reference,dtype='<f4',mode='r').reshape(-1,2)
        if len(self.reference)!=TOTAL or hashlib.sha256(Path(reference).read_bytes()).hexdigest()!=PCM_SHA:
            raise ValueError('reference_identity')
        metadata=json.loads((capture/'capture.json').read_text())
        self.timing=[json.loads(x) for x in (capture/'timing.jsonl').read_text().splitlines()]
        if (metadata['status']!='completed' or metadata['total_frames']!=len(self.audio)
                or metadata['discontinuities'] or analysis['timing']['errors']):raise ValueError('capture_timing_invalid')
        self.host=np.array([t['host_pts_ns'] for t in self.timing],dtype=np.int64)
        self.offset=np.array([t['frame_offset'] for t in self.timing],dtype=np.int64)
        total=0
        for index,record in enumerate(self.timing):
            if (record['frame_offset']!=total or record['input_timestamp']['flags']&3!=3 or record['discontinuity']
                    or index and record['host_pts_ns']<=self.timing[index-1]['host_pts_ns']):raise ValueError('buffer_timing_invalid')
            total+=record['frame_count']
        if total!=len(self.audio):raise ValueError('buffer_frame_count')

    def host_frame(self,ns):
        i=int(self.np.searchsorted(self.host,ns,side='right'))-1
        if i<0:raise ValueError('before_capture')
        record=self.timing[i];delta=(ns-int(self.host[i]))*RATE/1e9
        if delta<0 or delta>=record['frame_count']:raise ValueError('outside_actual_buffer')
        return float(self.offset[i])+delta

    def frame_host(self,frame):
        i=int(self.np.searchsorted(self.offset,frame,side='right'))-1
        if i<0 or frame>=int(self.offset[i])+self.timing[i]['frame_count']:raise ValueError('outside_actual_buffer')
        return int(self.host[i])+round((frame-int(self.offset[i]))*1e9/RATE)

    def _runs(self,mask):
        edges=self.np.diff(self.np.r_[False,mask,False].astype('i1'))
        return list(zip(self.np.flatnonzero(edges==1),self.np.flatnonzero(edges==-1)))

    def _prepare_audio_regions(self):
        """Audio and actual buffer boundaries only; no clock metadata is read."""
        if hasattr(self,'regions'):return
        for segment in self.analysis['segments']:
            span=segment.get('span',{});start=span.get('start');end=span.get('end')
            if not finite(start) or not finite(end) or not 0<=start<end or not finite(end*RATE):
                raise ValueError('invalid_audio_region')
        np=self.np;self.phase_cache=OrderedDict();self.regions=[];self.source_order=[]
        # A per-channel quiet bin rejects one-channel dropouts too. Exact zero
        # runs refine the coarse bins rather than allowing interpolation over them.
        count=len(self.audio)//24
        energy=np.mean(self.audio[:count*24].reshape(count,24,2).astype(float)**2,axis=1)
        quiet=[(int(a)*24,int(b)*24) for a,b in self._runs(np.any(energy<1e-8,axis=1))]
        quiet.extend((int(a),int(b)) for a,b in self._runs(np.any(self.audio==0,axis=1)))
        merged=[]
        for lo,hi in sorted(quiet):
            if merged and lo<=merged[-1][1]:merged[-1][1]=max(hi,merged[-1][1])
            else:merged.append([lo,hi])
        # Only integer-nanosecond rounding (<=2ns) is ignored, never a missing
        # recorder interval. A gap splits threshold and training regions.
        cuts=[int(self.offset[i]) for i in range(1,len(self.timing))
              if abs(int(self.host[i])-int(self.host[i-1])-
                     self.timing[i-1]['frame_count']*1e9/RATE)>2]
        self.quiet_intervals=merged;self.recorder_cuts=cuts
        ends=self.analysis.get('prefix_tail',[])
        if getattr(self,'allow_fractional_endpoints',False):
            self.endpoint_proof=self.prove_endpoints()
            ends=[dict(prefix=self.endpoint_proof['prefix'][ch],tail=self.endpoint_proof['tail'][ch]) for ch in (0,1)]
        prefix=[v.get('prefix') for v in ends]
        prefix_ok=(len(prefix)==2 and all(v and finite(v.get('correlation')) and v['correlation']>=.99
                   and v.get('reference_start_sample')==0 and finite(v.get('capture_start_sample')) for v in prefix))
        prefix_start=max(v['capture_start_sample'] for v in prefix) if prefix_ok else None
        segments=self.analysis['segments']
        for number,segment in enumerate(segments):
            lo=max(0,math.ceil(segment['span']['start']*RATE))
            hi=min(len(self.audio),math.floor(segment['span']['end']*RATE))
            onset=prefix_start if number==0 and prefix_ok else None
            if onset is not None:lo=max(0,int(onset))
            verified_tail=None
            tail=[v.get('tail') for v in ends]
            if number==len(segments)-1 and len(tail)==2 and all(
                    v and finite(v.get('correlation')) and v['correlation']>=.99
                    and finite(v.get('capture_start_sample')) and finite(v.get('reference_start_sample')) for v in tail):
                verified_tail=min(int(v['capture_start_sample']+len(self.reference)-v['reference_start_sample']) for v in tail)
                hi=min(hi,verified_tail)
            pieces=[(lo,hi)];prefix_quiet_end=onset
            for a,b in merged:
                # The independently identified full source prefix may itself
                # start quietly. Its complete held-out waveform must still
                # validate; this exception never applies to an interior gap.
                if onset is not None and a<=onset and b<=onset+1920:
                    prefix_quiet_end=max(prefix_quiet_end,b);continue
                if verified_tail is not None and a>=verified_tail:continue
                # Interior filter ringing is not a stable query/window location.
                left=max(0,a-(96 if a>0 else 0));right=min(len(self.audio),b+(96 if b<len(self.audio) else 0))
                pieces=[part for x,y in pieces for part in ((x,min(y,left)),(max(x,right),y)) if part[1]>part[0]]
            for cut in cuts:
                # Guard the entire interpolation stencil, not just the nominal
                # window: adjacent stored frames can be separated in host time.
                pieces=[part for x,y in pieces for part in ((x,min(y,cut-96)),(max(x,cut+96),y)) if part[1]>part[0]]
            for lower,upper in pieces:
                self.regions.append(dict(id=len(self.regions),segment=segment,lower=lower,upper=upper,
                                         prefix=onset is not None and lower==onset,
                                         prefix_quiet_end=prefix_quiet_end))

    def prove_endpoints(self):
        """Fixed source endpoints, with phase learned only on disjoint audio.

        Integer-only endpoint correlation is not a precondition. Neither endpoint
        can fit its own phase, gain, alignment or threshold. No native query or
        external endpoint proof is consumed here.
        """
        proof={}
        for name,number,source in (('prefix',0,0),('tail',len(self.analysis['segments'])-1,len(self.reference)-1920)):
            segment=self.analysis['segments'][number]
            region=dict(id=-1 if name=='prefix' else -2,segment=segment,
                        lower=max(0,math.ceil(segment['span']['start']*RATE)),
                        upper=min(len(self.audio),math.floor(segment['span']['end']*RATE)))
            channels=[]
            for ch in (0,1):
                anchors=segment['channels'][ch]['anchors']
                if not anchors:raise ValueError('endpoint_anchor_unavailable')
                anchor=min(anchors,key=lambda a:abs(a['capture_start_sample']-a['offset_samples']-source))
                estimate=round(source+anchor['offset_samples']);value=None
                for training_start in self._training_candidates(region,estimate):
                    training=self._phase_training(region,training_start,ch)
                    if training['correlation']<.99:continue
                    offset=training['real_offset_samples'];start=round(source+offset)
                    shift=source+offset-start
                    if (start<0 or start+1920>len(self.audio)
                            or not (training_start+2400<=start-2400 or training_start>=start+1920+2400)):
                        raise ValueError('endpoint_training_overlap')
                    # The complete held-out endpoint and interpolation stencil
                    # must not cross a recorder gap. Actual source edge silence
                    # is allowed only through the independent held-out check.
                    if any(start-47<cut<start+1920+48 for cut in self.recorder_cuts):
                        raise ValueError('endpoint_recorder_gap')
                    metric=self._metric(self.reference[source:source+1920,ch],self._sinc_capture(start,1920,ch,shift))
                    if metric['correlation']<.99:continue
                    value=dict(metric,reference_start_sample=source,capture_start_sample=start,
                               real_capture_start_sample=source+offset,heldout_source_frames=[source,source+1920],
                               heldout_capture_frames=[start,start+1920],training=training)
                    break
                if value is None:raise ValueError('endpoint_waveform_correlation')
                channels.append(value)
            if abs(channels[0]['real_capture_start_sample']-channels[1]['real_capture_start_sample'])>1:
                raise ValueError('endpoint_stereo_disagreement')
            proof[name]=channels
        return proof

    def _integer_match(self,region,start,width,ch):
        anchors=region['segment']['channels'][ch]['anchors']
        if not anchors:raise ValueError('no_capture_anchor')
        if any(not finite(a.get('offset_samples')) or not finite(a.get('capture_start_sample')) for a in anchors):
            raise ValueError('source_window_unavailable')
        anchor=min(anchors,key=lambda a:abs(a['capture_start_sample']-start))
        guess=start-anchor['offset_samples']
        # Guard both operands before local_match: np.correlate swaps short
        # operands and otherwise produces misleading/broadcasting failures.
        lo=max(0,round(guess)-128);hi=min(len(self.reference),round(guess)+128+width)
        if hi-lo<width or start<0 or start+width>len(self.audio):raise ValueError('source_window_unavailable')
        observed=self.audio[start:start+width,ch]
        if not self.np.all(self.np.isfinite(observed)) or float(observed.astype(float)@observed)<1e-15:
            raise ValueError('quiet_or_invalid_waveform')
        result=self.local_match(self.reference[:,ch],observed,guess,128)
        si=result['source_start_sample']
        if si<0 or si+width>len(self.reference):raise ValueError('source_window_unavailable')
        return result

    def _sinc_capture(self,start,width,ch,shift):
        """Normalized 96-tap Blackman sinc at integer frames plus shift.

        Zero padding is permitted only outside the recorded array. Interior
        region guards keep the stencil away from quiet and recorder gaps.
        """
        np=self.np;ks=np.arange(-47,49);weights=np.sinc(shift-ks)*np.blackman(96)
        weights/=weights.sum()
        lo=start-47;hi=start+width+48
        samples=self.audio[max(0,lo):min(len(self.audio),hi),ch].astype(float)
        if lo<0 or hi>len(self.audio):samples=np.pad(samples,(max(0,-lo),max(0,hi-len(self.audio))))
        return np.correlate(samples,weights,'valid')

    def _metric(self,reference,captured):
        np=self.np;x=reference.astype(float);y=captured.astype(float)
        xx=float(x@x);yy=float(y@y)
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)) or min(xx,yy)<1e-15:
            raise ValueError('quiet_or_invalid_waveform')
        dot=float(x@y)
        return dict(correlation=dot/math.sqrt(xx*yy),gain=dot/xx,
                    relative_rms_error=float(np.linalg.norm(x-y)/math.sqrt(xx)))

    def _training_candidates(self,region,start):
        lower,upper=region['lower'],region['upper'];grid=(start//RATE)*RATE+RATE//2
        local=[start+1920+2400,start-2400-2400]
        valid=lambda t: lower+96<=t and t+2400<=upper-96 and (t+2400<=start-2400 or t>=start+1920+2400)
        candidates=[t for t in [grid,grid+RATE//2,grid-RATE//2]+local if valid(t)]
        if not candidates:raise ValueError('disjoint_training_unavailable')
        primary=candidates[0]
        fallback=next((t for t in local if valid(t) and t!=primary),None)
        return [primary]+([fallback] if fallback is not None else [])

    def _phase_training(self,region,start,ch):
        key=(region['id'],start,ch,MATCHER_VERSION)
        if key in self.phase_cache:
            self.phase_cache.move_to_end(key);return self.phase_cache[key]
        integer=self._integer_match(region,start,2400,ch);si=integer['source_start_sample']
        reference=self.reference[si:si+2400,ch]
        def score(phase):
            value=self._metric(reference,self._sinc_capture(start,2400,ch,-phase))
            return value['relative_rms_error'],phase,value
        coarse=min((score(k*.05) for k in range(-20,21)),key=lambda v:(v[0],v[1]))
        center=round(coarse[1]*200)
        best=min((score(k*.005) for k in range(max(-200,center-10),min(200,center+10)+1)),key=lambda v:(v[0],v[1]))
        phase=best[1];value=best[2]
        result=dict(id=[region['id'],start,ch,MATCHER_VERSION],capture_frames=[start,start+2400],
                    source_frames=[si,si+2400],integer_correlation=integer['correlation'],
                    fractional_phase_samples=phase,real_offset_samples=start-si-phase,
                    correlation=value['correlation'],gain=value['gain'])
        self.phase_cache[key]=result
        if len(self.phase_cache)>512:self.phase_cache.popitem(last=False)
        return result

    def _fractional_channel(self,region,start,frame,ch,training_start):
        training=self._phase_training(region,training_start,ch)
        if training['correlation']<.99:raise ValueError('training_waveform_correlation')
        integer=self._integer_match(region,start,1920,ch);si=integer['source_start_sample']
        phase=start-si-training['real_offset_samples']
        if abs(phase)>1.000001:raise ValueError('source_mapping_discontinuity')
        value=self._metric(self.reference[si:si+1920,ch],self._sinc_capture(start,1920,ch,-phase))
        source=si+frame-start+phase
        if source < -.001 or source>=len(self.reference):raise ValueError('source_window_unavailable')
        if region['prefix'] and frame<region['lower'] and si!=0:raise ValueError('source_prefix_unproven')
        value.update(source_start_sample=si,source_center_sample=source,integer_correlation=integer['correlation'],
                     heldout_capture_frames=[start,start+1920],heldout_source_frames=[si,si+1920],
                     fractional_phase_samples=phase,training=training)
        return value

    def _check_source_order(self,result):
        """Reject both endpoints of a backwards mapping, independent of call order.

        The returned/cache dictionaries retain their waveform evidence. Later
        contradictory audio can invalidate an earlier qualification; it never
        changes a numerical match or makes a rejected point qualified.
        """
        for prior in self.source_order:
            if prior['region_id']!=result['region_id']:continue
            earlier,later=sorted((prior,result),key=lambda v:v['capture_frame'])
            if any(a['source_center_sample']>b['source_center_sample']+1
                   for a,b in zip(earlier['channels'],later['channels'])):
                prior.update(qualified=False,reason='source_mapping_nonmonotonic')
                result.update(qualified=False,reason='source_mapping_nonmonotonic')
        self.source_order.append(result)

    def match(self,ns):
        if ns in self.cache:return self.cache[ns]
        attempts=[]
        try:
            self._prepare_audio_regions();frame=self.host_frame(ns)
            region=next((r for r in self.regions if r['lower']-(1 if r['prefix'] else 0)<=frame<r['upper']),None)
            if region is None:raise ValueError('pause_or_audio_edge')
            lower,upper=region['lower'],region['upper']
            if upper-lower<1920:raise ValueError('short_audio_span')
            start=max(lower,min(round(frame)-960,upper-1920))
            if region['prefix'] and frame<region['prefix_quiet_end']:start=lower
            channels=None;failure='waveform_correlation'
            for training_start in self._training_candidates(region,start):
                try:
                    values=[self._fractional_channel(region,start,frame,ch,training_start) for ch in (0,1)]
                    attempts.append(dict(training_start_frame=training_start,channels=values))
                    if any(c['correlation']<.99 for c in values):raise ValueError('waveform_correlation')
                    if abs(values[0]['source_center_sample']-values[1]['source_center_sample'])>1:
                        raise ValueError('stereo_disagreement')
                    channels=values;break
                except ValueError as exc:
                    failure=str(exc)
                    if not attempts or attempts[-1]['training_start_frame']!=training_start:
                        attempts.append(dict(training_start_frame=training_start))
                    attempts[-1]['reason']=failure
                    # A source jump or stereo disagreement cannot be repaired by
                    # picking another phase-training interval.
                    if failure not in ('waveform_correlation','training_waveform_correlation'):raise
            if channels is None:raise ValueError(failure)
            result=dict(qualified=True,host_ns=ns,capture_frame=frame,
                        source_seconds=sum(c['source_center_sample'] for c in channels)/2/RATE,
                        segment=region['segment']['index'],region_id=region['id'],
                        region_capture_frames=[lower,upper],channels=channels)
        except ValueError as exc:result=dict(qualified=False,host_ns=ns,reason=str(exc))
        result.update(matcher_version=MATCHER_VERSION,training_attempts=attempts,
                      continuity_scope='bounded_windows_only_separate_whole_audio_gate_required')
        if result['qualified']:self._check_source_order(result)
        self.cache[ns]=result
        return result

    def threshold_host(self,threshold,decision):
        """Independently re-match in the same uninterrupted audio/buffer region."""
        if not finite(threshold) or not decision.get('qualified'):raise ValueError('threshold_pause_uncertainty')
        frame=decision['capture_frame']+(threshold-decision['source_seconds'])*RATE
        host=self.frame_host(frame);matched=self.match(host)
        if (not matched['qualified'] or matched['region_id']!=decision['region_id']
                or abs(matched['source_seconds']-threshold)>1/RATE):raise ValueError('threshold_pause_uncertainty')
        return host


def evaluate(joined,matcher):
    errors=list(joined['errors']);clock=[];events=[];threshold_matches=[];callbacks=joined['callbacks']
    for snapshot in joined['qualified']:
        match=matcher.match(snapshot['query_host_ns'])
        result=dict(row_sequence=snapshot['row_sequence'],row_sha256=snapshot['row_sha256'],mapped_seconds=snapshot['seconds'],capture=match)
        if not match['qualified']:errors.append('qualified_clock_capture_uncertain')
        else:
            result['error_seconds']=[snapshot['seconds']-c['source_center_sample']/RATE for c in match['channels']]
            if any(abs(value)>TOLERANCE for value in result['error_seconds']):errors.append('clock_tolerance')
        clock.append(result)
    first=joined['qualified'][0]['seconds'] if joined['qualified'] else None
    required=expected_events(first);delivered={(e['creative'],e['event']) for e in joined['events'] if e['delivered']}
    if not clock:errors.append('no_qualified_clock')
    if required-delivered:errors.append('missing_required_events')
    if not finite(first) or first>.25:
        if any(name=='preroll' for name,event in delivered):errors.append('late_preroll_success')
    paused=[]
    for callback in callbacks:
        if callback['event']=='pause':
            match=matcher.match(callback['issued_mach_ns']);paused.append(dict(callback=callback,capture=match))
    b_start,b_end=(v*1024/RATE for v in PLACEMENTS['b_paused'])
    b_pauses=[p for p in paused if p['capture']['qualified'] and b_start<=p['capture']['source_seconds']<b_end]
    if len(b_pauses)!=1:errors.append('paused_ad_overlap_inconclusive')
    for event in joined['events']:
        item=dict(event);decision=event['decision'];matched=matcher.match(decision['decision_ns']);item['capture_at_decision']=matched
        if event['delivered']:
            if not matched['qualified']:errors.append('event_capture_uncertain')
            else:
                network=(event['receipt']['receipt_mach_ns']-decision['decision_ns'])/1e9
                item['timing']=event_timing(event['threshold_seconds'],matched['source_seconds'],network)
                try:
                    threshold_host=matcher.threshold_host(event['threshold_seconds'],matched)
                    # Retain the independently rematched cached object: a later
                    # audio query can revoke it through source-order evidence.
                    threshold_matches.append(matcher.match(threshold_host))
                    delay=(decision['decision_ns']-threshold_host)/1e9
                    item['timing'].update(threshold_host_ns=threshold_host,threshold_to_decision_seconds=delay)
                    if delay < -TOLERANCE or delay>DECISION_BOUND:errors.append('event_decision_timing')
                    if any(min(threshold_host,decision['decision_ns'])<=c['issued_mach_ns']<=max(threshold_host,decision['decision_ns'])
                           for c in callbacks if c['event'] in ('pause','resume')):errors.append('event_crosses_pause_uncertainty')
                except ValueError:errors.append('event_threshold_uncertain')
                if item['timing']['violation']:errors.append('event_decision_timing')
            if event['creative']=='b_paused' and b_pauses:
                pause_ns=b_pauses[0]['callback']['issued_mach_ns']
                if decision['decision_ns']>=pause_ns or event['authorization']['authorization_ns']>=pause_ns:
                    errors.append('paused_ad_backfill')
        events.append(item)
    # Source-order checks can revoke earlier cached qualifications while matching
    # a later pause, event or threshold (including an undelivered decision).
    # Derive the final gate from every required dependency after all matches.
    if any(not row['capture']['qualified'] for row in clock):errors.append('qualified_clock_capture_uncertain')
    if any(item['delivered'] and not item['capture_at_decision']['qualified'] for item in events):
        errors.append('event_capture_uncertain')
    if any(not point['qualified'] for point in threshold_matches):errors.append('event_threshold_uncertain')
    if sum(p['capture']['qualified'] and b_start<=p['capture']['source_seconds']<b_end for p in paused)!=1:
        errors.append('paused_ad_overlap_inconclusive')
    return dict(kind='independent_owned_beacon_audit',gate_passed=not errors,errors=sorted(set(errors)),
                matcher_method=MATCHER_METHOD,estimator_replayed=False,clock_tolerance_seconds=TOLERANCE,decision_bound_seconds=DECISION_BOUND,
                qualified_clock_points=len(clock),clock=clock,events=events,pauses=paused,native_accounting=joined['native_accounting'],
                http_receipts=joined['http_receipts'],tracking_decisions=joined['tracking_decisions'],authorizations=joined['authorizations'],
                first_qualified_source_seconds=first,preroll_onset_qualified=finite(first) and first<=.25,
                missing_required_events=sorted(required-delivered),unexpected_events=sorted(delivered-required-{('b_paused',event) for event in EVENTS}),
                limits=['Whole-audio continuity and natural EOF require the separate full capture acceptance gate.',
                        'This audit uses process-tap buffer Mach timestamps; physical speaker timing is not established.'])


def select_current_launch_receipts(all_receipts, current_session_id):
    """Retain unattributed attempts and this launch's receipts, accepted or not."""
    return [receipt for receipt in all_receipts
            if not receipt.get('known_launch') or receipt.get('session_id') == current_session_id]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('capture','reference-pcm','analysis','native','events','receipts','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args();load_contract()
    if args.output.exists():raise FileExistsError('audit_output_must_be_new: '+str(args.output))
    rows=[json.loads(x) for x in args.events.read_text().splitlines()]
    # Receipts from other launches are retained in server log but evaluated in their own run.
    launches=[r for r in rows if r.get('kind')=='owned_launch']
    if len(launches)!=1:raise ValueError('one_launch_required')
    all_receipts=[json.loads(x) for x in args.receipts.read_text().splitlines()]
    native=args.native.read_bytes().splitlines()
    # Unknown/malformed attempts are never silently filtered away.
    receipts=select_current_launch_receipts(all_receipts, launches[0]['session_id'])
    joined=join_evidence(native,rows,receipts)
    analysis=json.loads(args.analysis.read_text())
    matcher=CaptureMatcher(args.capture,args.reference_pcm,analysis,allow_fractional_endpoints=launches[0].get('output_backend')=='hal_callback_v1')
    report=evaluate(joined,matcher)
    if hasattr(matcher,'endpoint_proof'):report['independent_endpoint_proof']=matcher.endpoint_proof
    report['receipt_log_total_attempts']=len(all_receipts)
    report['receipt_log_other_launch_attempts']=len(all_receipts)-len(receipts)
    report['input_sha256']={name:hashlib.sha256(getattr(args,name).read_bytes()).hexdigest() for name in ('native','events','receipts','analysis')}
    report['input_sha256'].update({name:file_sha256(path) for name,path in {
        'capture_pcm':args.capture/'capture-analysis.f32','capture_metadata':args.capture/'capture.json',
        'capture_timing':args.capture/'timing.jsonl','reference_pcm':args.reference_pcm,
        'evaluator':Path(__file__),'integer_matcher':Path(__file__).with_name('analyze_hls_capture.py')}.items()})
    with args.output.open('x') as output:output.write(json.dumps(report,indent=2,allow_nan=False)+'\n')
    summary={k:v for k,v in report.items() if k not in ('clock','events','pauses','http_receipts','tracking_decisions','authorizations','native_accounting')}
    accounting=report['native_accounting']
    summary['native_accounting']={key:len(accounting[key]) for key in ('consumed_sequences','excluded_terminal_tail','unaccounted_rows')}
    summary['native_accounting']['native_clock_rows']=accounting['native_clock_rows']
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
