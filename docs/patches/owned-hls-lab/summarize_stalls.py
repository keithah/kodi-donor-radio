#!/usr/bin/env python3
"""Summarize archived addon diagnostics. Interval overlap is not causation."""
import argparse
import json
import math
from pathlib import Path

OPERATIONS = (
    'control_read', 'telemetry_mutex_wait', 'telemetry_write', 'telemetry_flush',
    'renderer_owned_mutex_wait', 'clock_owned_mutex_wait', 'host_owned_mutex_wait',
    'adddata', 'renderer_sleep_overshoot', 'host_packet_interarrival',
    'host_packet_call', 'renderer_loop', 'getspace', 'decode_owned_mutex_wait',
    'decode_owned_mutex_held', 'packet_owned_mutex_wait',
)

def unsigned(value, label):
    if type(value) is not int or not 0 <= value <= 2**64-1:
        raise ValueError('invalid unsigned diagnostic field: '+label)
    return value

def exact_object(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('invalid diagnostic '+label+' structure')

def validate_diagnostic(diagnostic):
    exact_object(diagnostic, ('version','threshold_ns','available','revision',
                            'dropped_contention','dropped_events','operations','long_events'), 'snapshot')
    if type(diagnostic['version']) is not int or diagnostic['version'] != 1:
        raise ValueError('unsupported stall diagnostic version')
    if type(diagnostic['available']) is not bool:
        raise ValueError('invalid diagnostic availability')
    for key in ('threshold_ns','revision','dropped_contention','dropped_events'):
        unsigned(diagnostic[key],key)
    if diagnostic['threshold_ns'] != 40000000:
        raise ValueError('unexpected stall threshold')
    exact_object(diagnostic['operations'], OPERATIONS, 'operations')
    for metric in diagnostic['operations'].values():
        exact_object(metric,('count','max_ns','max_start_mach_ns','max_end_mach_ns'),'metric')
        for key,value in metric.items(): unsigned(value,key)
        if metric['max_end_mach_ns'] < metric['max_start_mach_ns'] or metric['max_end_mach_ns']-metric['max_start_mach_ns'] != metric['max_ns']:
            raise ValueError('maximum duration/bracket mismatch')
        if metric['count']==0 and any(metric[key] for key in ('max_ns','max_start_mach_ns','max_end_mach_ns')):
            raise ValueError('empty metric claims timing data')
    events=diagnostic['long_events']
    if type(events) is not list or len(events)>12:
        raise ValueError('invalid bounded diagnostic event list')
    counts={name:0 for name in OPERATIONS}
    for event in events:
        exact_object(event,('operation','start_mach_ns','end_mach_ns'),'event')
        if type(event['operation']) is not str or event['operation'] not in OPERATIONS:
            raise ValueError('unknown long-event operation')
        start=unsigned(event['start_mach_ns'],'event start')
        end=unsigned(event['end_mach_ns'],'event end')
        metric=diagnostic['operations'][event['operation']]
        counts[event['operation']]+=1
        if end < start or end-start <= diagnostic['threshold_ns'] or end-start > metric['max_ns']:
            raise ValueError('invalid long-event duration')
        if counts[event['operation']] > metric['count']:
            raise ValueError('event count exceeds operation count')
    if not diagnostic['available'] and (diagnostic['revision'] or diagnostic['dropped_contention'] or diagnostic['dropped_events'] or events or any(any(metric.values()) for metric in diagnostic['operations'].values())):
        raise ValueError('unavailable snapshot claims data')

def summarize(rows, capture_first_host_ns, gaps=()):
    unsigned(capture_first_host_ns, 'capture_first_host_ns')
    gap_spans=[]
    for index,(start,end) in enumerate(gaps):
        if not all(math.isfinite(x) for x in (start,end)) or end <= start:
            raise ValueError('gap must contain finite increasing capture seconds')
        gap_spans.append((index,capture_first_host_ns+round(start*1e9),
                          capture_first_host_ns+round(end*1e9)))
    def interval(start,end):
        unsigned(start,'interval start'); unsigned(end,'interval end')
        if end < start: raise ValueError('reversed diagnostic interval')
        return dict(start_mach_ns=start,end_mach_ns=end,duration_ms=(end-start)/1e6,
                    capture_start_seconds=(start-capture_first_host_ns)/1e9,
                    capture_end_seconds=(end-capture_first_host_ns)/1e9,
                    gap_overlaps=[dict(gap_index=index,overlap_ms=(min(end,ge)-max(start,gs))/1e6)
                                  for index,gs,ge in gap_spans if max(start,gs)<min(end,ge)])
    report=dict(kind='owned_hls_stall_summary',diagnostic_version=1,
                capture_first_host_ns=capture_first_host_ns,
                supplied_gap_intervals_seconds=[list(pair) for pair in gaps],
                overlap_is_not_causation=True,
                interpretation='Completed addon operations only; missing or dropped events do not prove absence of stalls. Intervals overlap and durations must not be summed.',
                ledger_rows=0,clock_rows=0,clock_rows_without_diagnostics=0,
                unavailable_snapshots=0,dropped_contention=0,dropped_events=0,
                qos_observations=[],operations={name:dict(count=0,maximum=None,long_events=[]) for name in OPERATIONS})
    last_revision=None
    for line_number,row in enumerate(rows,1):
        report['ledger_rows']+=1
        if type(row) is not dict: raise ValueError('invalid ledger row structure')
        if row.get('kind')!='clock_sample': continue
        report['clock_rows']+=1
        diagnostic=row.get('stall_diagnostics')
        if 'stall_diagnostics' not in row:
            report['clock_rows_without_diagnostics']+=1
            continue
        validate_diagnostic(diagnostic)
        qos=dict(status=row.get('renderer_qos_status'),qos_class=row.get('renderer_qos_class'),
                 relative_priority=row.get('renderer_qos_relative_priority'))
        if qos not in report['qos_observations']: report['qos_observations'].append(qos)
        if diagnostic.get('available') is not True:
            report['unavailable_snapshots']+=1
            continue
        revision=unsigned(diagnostic['revision'],'revision')
        if last_revision is not None and revision<=last_revision:
            raise ValueError('nonincreasing diagnostic snapshot revision')
        last_revision=revision
        report['dropped_contention']+=unsigned(diagnostic['dropped_contention'],'dropped_contention')
        report['dropped_events']+=unsigned(diagnostic['dropped_events'],'dropped_events')
        threshold=unsigned(diagnostic['threshold_ns'],'threshold_ns')
        if threshold!=40000000: raise ValueError('unexpected stall threshold')
        for name,metric in diagnostic['operations'].items():
            if name not in OPERATIONS: raise ValueError('unknown operation: '+name)
            summary=report['operations'][name]
            count=unsigned(metric['count'],'count')
            summary['count']+=count
            if count:
                span=interval(metric['max_start_mach_ns'],metric['max_end_mach_ns'])
                maximum=unsigned(metric['max_ns'],'max_ns')
                if span['end_mach_ns']-span['start_mach_ns']!=maximum:
                    raise ValueError('maximum duration/bracket mismatch')
                span['ledger_line']=line_number
                if summary['maximum'] is None or span['duration_ms']>summary['maximum']['duration_ms']:
                    summary['maximum']=span
        for event in diagnostic['long_events']:
            name=event['operation']
            if name not in OPERATIONS: raise ValueError('unknown long-event operation: '+name)
            span=interval(event['start_mach_ns'],event['end_mach_ns'])
            if span['end_mach_ns']-span['start_mach_ns']<=threshold:
                raise ValueError('long event does not exceed threshold')
            span['ledger_line']=line_number
            report['operations'][name]['long_events'].append(span)
    report['long_events_total']=sum(len(item['long_events']) for item in report['operations'].values())
    report['long_events_overlapping_supplied_gaps']=sum(bool(event['gap_overlaps']) for item in report['operations'].values() for event in item['long_events'])
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',type=Path,required=True,help='Archived, complete native JSONL')
    parser.add_argument('--capture-first-host-ns',type=int,required=True)
    parser.add_argument('--gap',nargs=2,type=float,action='append',default=[],metavar=('START_SECONDS','END_SECONDS'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        with args.ledger.open() as ledger:
            report=summarize((json.loads(line) for line in ledger),args.capture_first_host_ns,args.gap)
        report['ledger']=str(args.ledger)
        args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({key:report[key] for key in ('clock_rows','unavailable_snapshots','dropped_contention','dropped_events','long_events_total','long_events_overlapping_supplied_gaps')}))

if __name__=='__main__': main()
