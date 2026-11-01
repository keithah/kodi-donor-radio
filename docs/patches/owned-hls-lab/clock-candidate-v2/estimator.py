#!/usr/bin/env python3
"""V2 native-only clock: frozen v1 math plus delay validation. Offline research."""
import argparse
from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
from statistics import median

RATE = 48000
PERIOD = 1024 / RATE
WINDOW = 9
MIN_SPAN = .120
MAX_GAP = .100
MAX_QUERY = .005
MAX_MAD = PERIOD / 2
MAX_INNOVATION = PERIOD * 2


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def strict_json(value, **kwargs):
    """Keep unavailable numeric evidence as null, never nonstandard JSON tokens."""
    def clean(item):
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        return item
    return json.dumps(clean(value), allow_nan=False, **kwargs)


class Estimator:
    def __init__(self):
        self.origin_ns = None
        self.last_t = None
        self.last_a = None
        self.epoch = None
        self.feed = False
        self.window = deque(maxlen=WINDOW)
        self.anchor = None
        self.position = 0.0
        self.at_endpoint = False

    def clear_anchor(self):
        self.window.clear()
        self.anchor = None

    def update(self, row):
        qs = row['delay_query_start_steady_ns']
        qe = row['delay_query_end_steady_ns']
        host_ns = row['mach_absolute_ns'] + (qs+qe)//2-row['steady_ns']
        if self.origin_ns is None:
            self.origin_ns = host_ns
        t = (host_ns-self.origin_ns)/1e9
        a = row['accepted_adddata_frames']/RATE
        delay = row.get('audioengine_delay_s')
        delay_valid = finite_number(delay)
        raw = a-delay if delay_valid else None
        epoch = row['source_epoch']
        state = row['control_state']
        feed = state in ('play', 'resume')
        reasons = []
        if self.epoch is not None and (epoch != self.epoch or a < self.last_a):
            self.clear_anchor()
            self.position = 0.0
            self.at_endpoint = False
            reasons.append('epoch_reset')
        bad = (not delay_valid or not all(math.isfinite(v) for v in (a, t)) or a < 0 or delay < 0
               or qe < qs or (qe-qs)/1e9 > MAX_QUERY)
        if bad:
            self.clear_anchor()
            reasons.append('invalid_query')
        if self.last_t is not None and not 0 < t-self.last_t <= MAX_GAP:
            self.clear_anchor()
            reasons.append('observation_gap')
        if not row['playback_started'] or row['failed']:
            self.clear_anchor()
            reasons.append('not_started_or_failed')
        if feed and not self.feed:
            self.clear_anchor()
            reasons.append('feed_acquisition')
        if feed and self.at_endpoint and self.last_a is not None and a > self.last_a:
            self.clear_anchor()
            self.at_endpoint = False
            reasons.append('underrun_reacquisition')

        can_observe = (feed and not bad and row['playback_started']
                       and not row['failed'] and not row['drained'])
        mad = None
        if can_observe:
            offset = raw-t
            if self.anchor is not None and abs(offset-self.anchor) > MAX_INNOVATION:
                self.clear_anchor()
                reasons.append('anchor_innovation')
            self.window.append((t, offset))
            if len(self.window) == WINDOW and t-self.window[0][0] >= MIN_SPAN:
                center = median(x[1] for x in self.window)
                mad = median(abs(x[1]-center) for x in self.window)
                if mad <= MAX_MAD:
                    self.anchor = center
                else:
                    self.anchor = None
                    reasons.append('unstable_anchor')
        if self.anchor is None:
            reasons.append('acquiring')
        else:
            prediction = t+self.anchor
            if prediction < self.position-1/RATE:
                reasons.append('backward_correction')
            self.position = min(a, max(self.position, prediction, 0.0))
            if prediction >= a:
                self.at_endpoint = True
                reasons.append('accepted_endpoint')
        if not feed:
            reasons.append('pause_tail_uncertain' if state == 'pause' else 'nonfeed_uncertain')
        if row['drained']:
            reasons.append('drained_not_audible_end')
        self.last_t, self.last_a, self.epoch, self.feed = t, a, epoch, feed
        return dict(query_host_ns=host_ns, seconds=self.position, raw_seconds=raw,
                    valid=not reasons, reasons=reasons, anchor=self.anchor,
                    median_absolute_deviation_s=mad, window_size=len(self.window),
                    control_state=state)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ledger', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--comparison-report', type=Path,
                    help='Independent waveform report; evaluation only, never estimator input')
    args=ap.parse_args()
    estimator=Estimator()
    rows=[]
    for line in args.ledger.open():
        row=json.loads(line)
        if row['kind']=='clock_sample':
            out=estimator.update(row)
            out['ledger_row']=len(rows)
            rows.append(out)
    report=dict(kind='native_clock_candidate_v2', estimator_sha256=sha(__file__),
                algorithm_sha256=sha(Path(__file__).with_name('algorithm.md')),
                ledger=str(args.ledger), ledger_sha256=sha(args.ledger),
                parameters=dict(rate=RATE,period_s=PERIOD,window=WINDOW,
                                min_span_s=MIN_SPAN,max_gap_s=MAX_GAP,
                                max_query_s=MAX_QUERY,max_mad_s=MAX_MAD,
                                max_innovation_s=MAX_INNOVATION),
                samples=len(rows), valid_samples=sum(x['valid'] for x in rows),
                invalid_reasons=dict(Counter(r for x in rows for r in x['reasons'])),
                physical_speaker_mapping_proven=False,
                heldout_validation_proven=False)
    if args.comparison_report:
        original=json.loads(args.comparison_report.read_text())
        comparisons=[]
        for sample in original['clock']['samples']:
            if not sample['qualified']:
                continue
            estimate=rows[sample['ledger_row']]
            if estimate['query_host_ns'] != sample['query_host_ns']:
                raise ValueError('Comparison report does not belong to this ledger')
            errors=[(estimate['seconds']-c['source_center_sample']/RATE)*1000
                    for c in sample['channels']]
            comparisons.append(dict(ledger_row=sample['ledger_row'],
                                    grid_capture_seconds=sample['grid_capture_seconds'],
                                    valid=estimate['valid'], reasons=estimate['reasons'],
                                    candidate_error_ms=errors,
                                    original_error_ms=[c['error_ms'] for c in sample['channels']]))
        def stats(items):
            errors=[v for x in items for v in x['candidate_error_ms']]
            return dict(points=len(items),error_range_ms=[min(errors),max(errors)] if errors else None,
                        violations=sum(any(abs(e)>PERIOD*1000 for e in x['candidate_error_ms']) for x in items))
        report['comparison']=dict(report=str(args.comparison_report),
                                  all_original_qualified=stats(comparisons),
                                  valid_original_qualified=stats([x for x in comparisons if x['valid']]),
                                  invalid_original_qualified=[x for x in comparisons if not x['valid']],
                                  samples=comparisons,
                                  note='Training-set evaluation unless separately designated held out; invalid is not passing.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(strict_json(report,indent=2)+'\n')
    timeline=args.output.with_suffix('.timeline.jsonl')
    timeline.write_text(''.join(strict_json(row)+'\n' for row in rows))
    summary={k:v for k,v in report.items() if k!='comparison'}
    if 'comparison' in report:
        summary['comparison']={k:v for k,v in report['comparison'].items() if k not in ('samples','invalid_original_qualified')}
        summary['comparison']['invalid_original_qualified_count']=len(report['comparison']['invalid_original_qualified'])
    print(strict_json(summary,indent=2))


if __name__=='__main__':
    main()
