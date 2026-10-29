# SPDX-License-Identifier: GPL-2.0-or-later
"""Historical native-q HAL evidence. No AE estimator or read-now extrapolation."""
from collections import OrderedDict
from dataclasses import replace
from fractions import Fraction
import hashlib
import math

from owned_clock import OwnedClockSnapshot, _json, _uint, _hex, mach_host_ns
from owned_hal_gain import HalControlSession, kodi_gain

BACKEND = 'hal_callback_v1'
QUERY_UINT = ('sequence','query_start_ns','query_end_ns','query_host_ns','source_frame',
    'segment_sequence','applied_sequence','source_zero_sequence','source_zero_ticks','timebase_numer',
    'timebase_denom','initial_deadline_ns','native_gate','effective_intent','device_generation',
    'stream_generation','playback_epoch','gain_epoch','readiness_count','consistent_count',
    'applied_request_start_ns','applied_request_end_ns','applied_issue_ns','applied_expiry_ns')
EPOCHS = ('device_generation','stream_generation','playback_epoch','gain_epoch','native_gate')


def host(row, key='output_ticks'):
    return Fraction(row[key]*row['timebase_numer'],row['timebase_denom'])


def usable(s):
    return (s['control_acquired'] and s['playing'] and s['resume_granted'] and s['started']
        and s['sample_zero_proven'] and not s['muted'] and s['applied_gain']>0 and not s['terminal']
        and not s['native_gate'] & ((1<<63)|(1<<62)))


class OwnedHalClockProvider(HalControlSession):
    def __init__(self,binding,now_ns=mach_host_ns):
        if binding.hal_phase!='clock' or binding.hal_clock_version!=1:
            raise ValueError('explicit_hal_clock_binding_required')
        super().__init__(binding,now_ns)
        self._segments=OrderedDict();self._latest=None;self._applied=None;self._zero=None
        self._acquiring=False;self._consistent=[];self._last_query_ns=None
        self._query_sequence=0;self._last=OwnedClockSnapshot(backend=BACKEND)
        self._readiness=[];self._ready_key=None;self._first_qualified=False
        self._desired_gain_epoch=None;self._initial_closed=False
        self._query_rows=OrderedDict()
        self.authorization=None;self._consumed_proofs=set()

    def _invalidate(self,reason):
        self.reason=reason
        self._last=OwnedClockSnapshot(reasons=(reason,),generation=self.generation,backend=BACKEND)

    def gain_pending(self,command):
        with self.lock:
            epoch=command.effective_epoch
            if self._desired_gain_epoch is not None and epoch!=self._desired_gain_epoch:
                self.generation+=1;self._invalidate('gain_pending')
                if self._zero is not None and not self._first_qualified:self._initial_closed=True
            self._desired_gain_epoch=epoch

    def control(self,state,issued_ns=None):
        with self.lock:
            result=super().control(state,issued_ns)
            if self._zero is not None and not self._first_qualified:self._initial_closed=True
            self._invalidate('callback_'+state)
            return result

    def _segment(self,row):
        if (row['output_sample_time']!=int(row['output_sample_time']) or row['output_flags']&3!=3
                or row['now_flags']&3!=3 or not 0<row['timebase_numer']<=2**32-1
                or not 0<row['timebase_denom']<=2**32-1 or row['reason']!=0):
            raise ValueError('hal_timestamp_schema')
        output=host(row);entry=host(row,'entry_ticks');exit=host(row,'exit_ticks')
        if (output>2**64-1 or entry>2**64-1 or exit>2**64-1 or host(row,'now_ticks')>2**64-1
                or not 0<=output-entry<=100_000_000 or exit<entry or int(output)!=row['output_host_ns']):
            raise ValueError('hal_timestamp_range')
        if self._latest:
            prior=self._latest
            if (row['timebase_numer']!=prior['timebase_numer'] or row['timebase_denom']!=prior['timebase_denom']
                    or row['output_sample_time']!=prior['output_sample_time']+prior['requested_frames']
                    or abs(output-host(prior)-Fraction(prior['requested_frames']*10**9,48000))>Fraction(10**9,48000)
                    or row['source_start']!=prior['source_start']+prior['consumed_frames']):
                raise ValueError('hal_segment_continuity')
        if row['control_acquired']:
            if (row['device_generation']!=1 or row['stream_generation']!=1
                    or row['native_gate']!=row['in_flight_generation']
                    or row['gain_epoch']==0 or row['playback_epoch']==0 or row['publication_sequence']==0
                    or not 0<=row['volume']<=100 or abs(row['applied_gain']-kodi_gain(row['volume'],row['muted']))>1e-12
                    or not row['request_start_ns']<=row['request_end_ns']<=row['issue_ns']<=entry
                    or row['request_end_ns']-row['request_start_ns']>50_000_000
                    or row['expiry_ns']!=row['request_start_ns']+500_000_000
                    or not row['terminal'] and row['expiry_ns']<output+Fraction(row['copied_frames']*10**9,48000)):
                raise ValueError('hal_applied_schema')
            if self._applied and any(row[k]<self._applied[k] for k in ('gain_revision','gain_epoch','playback_revision','playback_epoch','publication_sequence','applied_intent')):
                raise ValueError('hal_applied_regression')
            self._applied=row
        if row['copied_frames'] and self._zero is None:
            if row['source_start']!=0 or not usable(row):raise ValueError('initial_source_zero')
            self._zero=row
        self._latest=row;self._segments[row['sequence']]=row
        if len(self._segments)>64:self._segments.popitem(last=False)
        if row['sequence']>80000:raise ValueError('hal_segment_bound')

    def _query(self,row,raw):
        if (any(not _uint(row.get(k)) for k in QUERY_UINT)
                or any(type(row.get(k)) is not bool for k in ('qualified','readiness','initial_failed'))
                or any(type(row.get(k)) is not str for k in ('reason','segment_sha256','applied_sha256','source_zero_sha256'))
                or row.get('hal_phase')!='clock' or type(row.get('hal_clock_version')) is not int or row['hal_clock_version']!=1
                or type(row.get('hal_contract_version')) is not int or row['hal_contract_version']!=1
                or row['sequence']!=self._query_sequence+1 or row['sequence']>30000):
            raise ValueError('hal_query_schema')
        self._query_sequence=row['sequence'];self._query_rows[row['sequence']]=row
        if len(self._query_rows)>64:self._query_rows.popitem(last=False)
        start,end,q=(row[k] for k in ('query_start_ns','query_end_ns','query_host_ns'))
        now=self.now_ns() # sampled after read/parse, never loop entry
        if (not 0<=end-start<=5_000_000 or q!=start+(end-start)//2 or end>now or self._last_query_ns is not None and q<=self._last_query_ns):
            raise ValueError('hal_query_bracket')
        self._last_query_ns=q
        if row['initial_failed']:self._initial_closed=True
        applied=self._segments.get(row['applied_sequence'])
        if row['applied_sequence']:
            if (applied is None or self._applied!=applied or applied['raw_sha256']!=row['applied_sha256']
                    or any(row['applied_'+k]!=applied[k] for k in ('request_start_ns','request_end_ns','issue_ns','expiry_ns'))):
                raise ValueError('hal_query_applied_identity')
        key=tuple(row[k] for k in EPOCHS)+ (row['effective_intent'],)
        if key!=self._ready_key:
            self._readiness=[];self._ready_key=key
            if self._first_qualified:self._acquiring=True;self._consistent=[]
            if self._zero is not None and not self._first_qualified:self._initial_closed=True
        if row['reason']=='startup_readiness':
            if (not applied or not usable(applied) or not self._latest or self._latest['consumed_frames']
                    or self._latest['source_start'] or not 0<=end-host(self._latest,'entry_ticks')<=40_000_000
                    or applied['expiry_ns']<end or any(applied[k]!=row[k] for k in EPOCHS)
                    or applied['applied_intent']!=row['effective_intent']):raise ValueError('hal_readiness_proof')
            self._readiness.append(q)
            if row['readiness_count']!=len(self._readiness):raise ValueError('hal_readiness_count')
            if row['readiness']!=(len(self._readiness)>=9 and q-self._readiness[0]>=120_000_000):raise ValueError('hal_readiness_span')
        evidence=dict(generation=self.generation,backend=BACKEND,query_start_ns=start,query_end_ns=end,query_host_ns=q,
            row_sequence=row['sequence'],row_sha256=hashlib.sha256(raw).hexdigest(),native_generation=row['native_gate'],
            **{k:row[k] for k in ('segment_sha256','applied_sequence','applied_sha256','effective_intent','playback_epoch',
                'gain_epoch','device_generation','stream_generation','source_zero_sequence','source_zero_sha256','source_zero_ticks',
                'timebase_numer','timebase_denom','initial_deadline_ns')})
        if not row['qualified'] and row['reason']!='reacquiring':
            self._last=OwnedClockSnapshot(reasons=(row['reason'],),**evidence);return self._last
        if (self._initial_closed or self._zero is None or row['source_zero_sequence']!=self._zero['sequence']
                or row['source_zero_sha256']!=self._zero['raw_sha256'] or row['source_zero_ticks']!=self._zero['output_ticks']
                or row['timebase_numer']!=self._zero['timebase_numer'] or row['timebase_denom']!=self._zero['timebase_denom']
                or row['initial_deadline_ns']!=int(host(self._zero))+120_000_000):
            raise ValueError('hal_initial_provenance')
        if not self._first_qualified:
            if (now>row['initial_deadline_ns'] or len(self._readiness)<9 or self._readiness[-1]-self._readiness[0]<120_000_000
                    or row['source_frame']<=1024 or row['source_frame']>12000
                    or q<=host(self._zero)+Fraction(1024*10**9,48000)):
                self._initial_closed=True;raise ValueError('hal_initial_opportunity')
        covering=[s for s in self._segments.values() if s['copied_frames'] and host(s)<=q<host(s)+Fraction(s['copied_frames']*10**9,48000)]
        if len(covering)!=1:raise ValueError('hal_query_coverage')
        s=covering[0];frame=s['source_start']+int((q-host(s))*48000/10**9)
        if (s['sequence']!=row['segment_sequence'] or s['raw_sha256']!=row['segment_sha256'] or frame!=row['source_frame']
                or not usable(s) or not applied or not usable(applied) or any(s[k]!=row[k] or applied[k]!=row[k] for k in EPOCHS)
                or s['applied_intent']!=row['effective_intent'] or applied['applied_intent']!=row['effective_intent']
                or not 0<=end-host(self._latest,'entry_ticks')<=40_000_000 or applied['expiry_ns']<end
                or not 0<=now-q<=100_000_000 or self._ack!=len(self._commands)
                or self._desired_gain_epoch is not None and row['gain_epoch']!=self._desired_gain_epoch):
            raise ValueError('hal_query_proof')
        if self._acquiring:
            self._consistent.append(q)
            if row['qualified'] and (len(self._consistent)<9 or q-self._consistent[0]<120_000_000):
                raise ValueError('hal_reacquisition_span')
            if row['qualified']:self._acquiring=False
        if not row['qualified']:
            self._last=OwnedClockSnapshot(reasons=(row['reason'],),**evidence);return self._last
        self._first_qualified=True
        self._last=OwnedClockSnapshot(valid=True,reasons=(),seconds=frame/48000,authorization_ns=now,**evidence)
        return self._last

    def ingest(self,raw):
        with self.lock:
            try:
                row=_json(raw)
                if self.retired:return self.snapshot() if row.get('kind')=='hal_query' else None
                if row.get('kind')=='hal_query':
                    if (self._hello is None or any(row.get(k)!=getattr(self.binding,k) for k in ('nonce','incarnation','timeline_sha256','output_backend'))):
                        raise ValueError('hal_query_binding')
                    return self._query(row,raw)
                result=super().ingest(raw)
                if result and result['kind']=='hal_segment':self._segment(result)
                if result and result['kind']=='hal_terminal':self._invalidate('native_terminal')
                return None
            except (ValueError,TypeError,KeyError,OverflowError,ZeroDivisionError):
                self.retire('hal_evidence_invalid');self._invalidate('hal_evidence_invalid');return self._last

    def snapshot(self):
        with self.lock:
            ev=self._last;now=self.now_ns()
            if (self.retired or not ev.valid or ev.generation!=self.generation or self.source_ended
                    or not 0<=now-ev.query_host_ns<=100_000_000 or not self._applied or not usable(self._applied)
                    or self._applied['expiry_ns']<now or self._desired_gain_epoch is not None and ev.gain_epoch!=self._desired_gain_epoch
                    or any(getattr(ev,k)!=self._applied[k] for k in ('device_generation','stream_generation','playback_epoch','gain_epoch'))
                    or ev.native_generation!=self._applied['native_gate'] or ev.effective_intent!=self._applied['applied_intent']):
                return replace(ev,valid=False,reasons=(self.reason if self.retired else 'stale_or_changed',))
            return ev

    def retire(self,reason):
        super().retire(reason)
        authorization=getattr(self,'authorization',None)
        if authorization:authorization.cancel_now()

    def probe(self,evidence,stage,**kwargs):
        with self.lock:
            current=self.snapshot()
            if (not current.valid or evidence.generation!=self.generation or not self.authorization):return None
            generation=self.generation;lease=self._applied['expiry_ns'];binding=self.binding
        proof=self.authorization.probe(binding,evidence,generation,stage,lease_expiry_ns=lease,**kwargs)
        if self.authorization.failed:self.retire('native_authorization_transport')
        return proof

    def consume_proof(self,evidence,proof,stage,action):
        from owned_hal_authorization import FIELDS,valid_times
        with self.lock:
            if proof is None:return False
            request,response=proof.request,proof.response
            identity=request.get('request_id')
            if not _uint(identity) or identity in self._consumed_proofs:return False
            self._consumed_proofs.add(identity)
            if len(self._consumed_proofs)>32768:self.retire('authorization_bound');return False
            now=self.now_ns();current=self.snapshot()
            if (not current.valid or not evidence.valid or evidence.generation!=self.generation
                    or request.get('expected_generation')!=self.generation or request.get('stage')!=stage
                    or any(request.get(k)!=getattr(evidence,k) for k in FIELDS)
                    or response.get('authorized') is not True or not valid_times(response,now)
                    or response.get('actual_gate')!=evidence.native_generation
                    or response.get('actual_intent')!=evidence.effective_intent
                    or request.get('initial')==1 and stage!='dispatch' and now>evidence.initial_deadline_ns):return False
            return action(replace(evidence,authorization_ns=now,proof_id=identity,proof_sha256=proof.response_sha256))

    def authorize(self,evidence,action):
        # HAL callers must choose a stage and obtain a fresh proof outside locks.
        return False
