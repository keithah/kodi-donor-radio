#!/usr/bin/env python3
"""Audit recorded owned clock/decision/authorization/HTTP evidence against capture.

No estimator is imported or replayed. Every claimed qualified query and event
uses its actual Mach time and independent stereo waveform matching. The existing
whole-audio analyzer supplies capture-only anchors within each audible span.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from fixture_beacons import FIRST_PACKET,PCM_SHA,PLACEMENTS,EVENTS,URL,TOTAL,load_contract

RATE=48000
TOLERANCE=1024/RATE
DECISION_BOUND=.1+.02+TOLERANCE
ESTIMATOR_SHA='7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87'
FRACTIONS=dict(zip(EVENTS,(0,0,.25,.5,.75,1)))


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


def join_evidence(native_lines,rows,receipts):
    """Validate exact recorded chain without calculating a clock estimate."""
    errors=[];native={};snapshots={};events=[];qualified=[]
    launches=[r for r in rows if r.get('kind')=='owned_launch']
    if len(launches)!=1:raise ValueError('one_owned_launch_required')
    launch=launches[0]
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
        matched=[(i,r) for i,r in enumerate(authorizations) if key(r)==event_key]
        if len(matched)!=1:errors.append('missing_or_duplicate_authorization');continue
        index,auth=matched[0];used_authorizations.add(index)
        threshold=(PLACEMENTS[name][0]+(PLACEMENTS[name][1]-PLACEMENTS[name][0])*FRACTIONS[event])*1024/RATE
        if (any(auth.get(k)!=decision.get(k) for k in ('row_sequence','row_sha256','query_host_ns','generation',
                'native_generation','mapped_seconds','decision_ns','session_id')) or auth.get('threshold_seconds')!=threshold):
            errors.append('authorization_decision_identity')
        times=[decision.get('query_host_ns'),decision.get('decision_ns'),auth.get('authorization_ns')]
        if any(type(t) is not int for t in times) or not times[0]<=times[1]<=times[2]<=times[0]+100000000:
            errors.append('authorization_freshness')
        matched=[(i,r) for i,r in enumerate(receipts) if (r.get('session_id'),r.get('creative'),r.get('event'))==(launch['session_id'],name,event)]
        authorized=auth.get('authorized') is True
        if not authorized:
            if matched:errors.append('receipt_without_authorization')
            events.append(dict(creative=name,event=event,delivered=False,decision=decision,authorization=auth,threshold_seconds=threshold))
            continue
        if decision.get('reason')!='queued':errors.append('authorized_unqueued_decision')
        if len(matched)!=1:errors.append('missing_or_duplicate_receipt');continue
        index,receipt=matched[0];used_receipts.add(index)
        if (receipt.get('accepted') is not True or receipt.get('duplicate') is not False or receipt.get('receipt_bound_exhausted') is True
                or receipt.get('status')!=204 or receipt.get('method')!='GET'
                or type(receipt.get('receipt_mach_ns')) is not int
                or receipt['receipt_mach_ns']<auth.get('authorization_ns',0)):
            errors.append('invalid_http_receipt')
        events.append(dict(creative=name,event=event,delivered=True,decision=decision,authorization=auth,
                           receipt=receipt,threshold_seconds=threshold))
    if len(used_authorizations)!=len(authorizations):errors.append('orphan_authorization')
    if len(used_receipts)!=len(receipts):errors.append('unexpected_http_attempt')
    return dict(launch=launch,nonce=nonce,errors=sorted(set(errors)),qualified=qualified,events=events,
                http_receipts=receipts,tracking_decisions=decisions,authorizations=authorizations,
                callbacks=[r for r in rows if r.get('kind')=='owned_callback'])


class CaptureMatcher:
    """Direct matching at actual buffer timestamps, constrained to one audible span."""
    def __init__(self,capture,reference,analysis):
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

    def match(self,ns):
        if ns in self.cache:return self.cache[ns]
        try:
            frame=self.host_frame(ns)
            segment=next((s for s in self.analysis['segments'] if s['span']['start']*RATE<=frame<s['span']['end']*RATE),None)
            # The query may precede the first RMS audio bin. The independent
            # matched source prefix bounds this startup allowance, never a clock.
            if segment is None and self.analysis['segments']:
                first=self.analysis['segments'][0]
                prefix=[v['prefix'] for v in self.analysis['prefix_tail']]
                if len(prefix)==2 and all(v and v['correlation']>=.99 for v in prefix):
                    begin=max(v['capture_start_sample']-v['reference_start_sample'] for v in prefix)
                    if begin<=frame<first['span']['start']*RATE:segment=first
            if segment is None:raise ValueError('pause_or_audio_edge')
            lower=max(0,math.ceil(segment['span']['start']*RATE))
            upper=min(len(self.audio),math.floor(segment['span']['end']*RATE))
            if upper-lower<1920:raise ValueError('short_audio_span')
            # Forward at onset, backward at tail: query-relative offset is exact.
            start=max(lower,min(round(frame)-960,upper-1920));stop=start+1920
            channels=[]
            for ch in (0,1):
                anchors=segment['channels'][ch]['anchors']
                if not anchors:raise ValueError('no_capture_anchor')
                anchor=min(anchors,key=lambda a:abs(a['capture_start_sample']-start))
                value=self.local_match(self.reference[:,ch],self.audio[start:stop,ch],start-anchor['offset_samples'],128)
                value['source_center_sample']=value['source_start_sample']+(frame-start)
                channels.append(value)
            if any(c['correlation']<.99 for c in channels):raise ValueError('waveform_correlation')
            if abs(channels[0]['source_center_sample']-channels[1]['source_center_sample'])>1:raise ValueError('stereo_disagreement')
            result=dict(qualified=True,host_ns=ns,capture_frame=frame,source_seconds=sum(c['source_center_sample'] for c in channels)/2/RATE,
                        segment=segment['index'],channels=channels)
        except ValueError as exc:result=dict(qualified=False,host_ns=ns,reason=str(exc))
        self.cache[ns]=result
        return result

    def threshold_host(self,threshold,decision):
        """Re-match threshold window; never extrapolate across a pause or quiet span."""
        frame=decision['capture_frame']+(threshold-decision['source_seconds'])*RATE
        host=self.frame_host(frame);matched=self.match(host)
        if (not matched['qualified'] or matched['segment']!=decision['segment']
                or abs(matched['source_seconds']-threshold)>1/RATE):raise ValueError('threshold_pause_uncertainty')
        return host


def evaluate(joined,matcher):
    errors=list(joined['errors']);clock=[];events=[];callbacks=joined['callbacks']
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
    return dict(kind='independent_owned_beacon_audit',gate_passed=not errors,errors=sorted(set(errors)),
                estimator_replayed=False,clock_tolerance_seconds=TOLERANCE,decision_bound_seconds=DECISION_BOUND,
                qualified_clock_points=len(clock),clock=clock,events=events,pauses=paused,
                http_receipts=joined['http_receipts'],tracking_decisions=joined['tracking_decisions'],authorizations=joined['authorizations'],
                first_qualified_source_seconds=first,preroll_onset_qualified=finite(first) and first<=.25,
                missing_required_events=sorted(required-delivered),unexpected_events=sorted(delivered-required-{('b_paused',event) for event in EVENTS}),
                limits=['Whole-audio continuity and natural EOF require the separate full capture acceptance gate.',
                        'This audit uses process-tap buffer Mach timestamps; physical speaker timing is not established.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('capture','reference-pcm','analysis','native','events','receipts','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args();load_contract()
    rows=[json.loads(x) for x in args.events.read_text().splitlines()]
    # Receipts from other launches are retained in server log but evaluated in their own run.
    launches=[r for r in rows if r.get('kind')=='owned_launch']
    if len(launches)!=1:raise ValueError('one_launch_required')
    all_receipts=[json.loads(x) for x in args.receipts.read_text().splitlines()]
    native=args.native.read_bytes().splitlines()
    # Unknown/malformed attempts are never silently filtered away.
    receipts=[r for r in all_receipts if r.get('accepted') is not True or not r.get('known_launch') or r.get('session_id')==launches[0]['session_id']]
    joined=join_evidence(native,rows,receipts)
    analysis=json.loads(args.analysis.read_text())
    report=evaluate(joined,CaptureMatcher(args.capture,args.reference_pcm,analysis))
    report['receipt_log_total_attempts']=len(all_receipts)
    report['receipt_log_other_launch_attempts']=len(all_receipts)-len(receipts)
    report['input_sha256']={name:hashlib.sha256(getattr(args,name).read_bytes()).hexdigest() for name in ('native','events','receipts','analysis')}
    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('clock','events','pauses','http_receipts','tracking_decisions','authorizations')},indent=2))


if __name__=='__main__':main()
