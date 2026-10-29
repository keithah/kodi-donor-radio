# SPDX-License-Identifier: GPL-2.0-or-later
"""Fixture HAL controls only: no estimator, clock snapshots, or beacon sender."""
from dataclasses import asdict, dataclass, replace
import json
import hashlib
import math
import threading

from owned_clock import OwnedCommand, OwnedTransportError, URL, _hex, _json, _uint

HAL_FORMAT = dict(rate=48000, frames=512, channels=2, buffers=1, format='float32_interleaved')


def kodi_gain(volume, muted):
    if type(volume) is not int or not 0 <= volume <= 100 or type(muted) is not bool:
        raise ValueError('invalid_gain_value')
    if muted or volume == 0:
        return 0.0
    multiplier = 10**(60*(volume/100-1)/20)
    return 1.0 if multiplier >= .99 else multiplier


def _controls_only(row, required=False):
    return all((key not in row and not required) or row.get(key) is False
               for key in ('clock_authority','beacons_enabled'))


def _valid_gain_ack(row):
    """Validate emitted callback provenance before either consumer accepts it."""
    positive=('device_generation','gain_revision','gain_epoch','in_flight_generation',
              'playback_revision','playback_epoch','segment_sequence','publication_sequence','applied_intent')
    unsigned=('request_start_ns','request_end_ns','issue_ns','expiry_ns','output_host_ns',
              'source_start','native_gate','authorized_native_epoch','resume_playback_revision','volume')
    if (not isinstance(row,dict) or row.get('kind')!='hal_gain_ack'
            or row.get('output_backend')!='hal_callback_v1'
            or not _hex(row.get('nonce'),32) or not _hex(row.get('incarnation'),32)
            or not _hex(row.get('timeline_sha256'),64)
            or type(row.get('hal_contract_version')) is not int or row['hal_contract_version']!=1
            or any(not _uint(row.get(k)) or not row[k] for k in positive)
            or any(not _uint(row.get(k)) for k in unsigned)
            or type(row.get('muted')) is not bool or type(row.get('resume_granted')) is not bool
            or type(row.get('applied_gain')) not in (int,float) or not 0<=row['applied_gain']<=1 or not math.isfinite(row['applied_gain'])
            or not _controls_only(row) or row['volume']>100):
        return False
    paused=1<<62
    epoch=row['native_gate'] & (paused-1)
    granted=not (row['native_gate'] & paused) and bool(row['resume_playback_revision']) and row['authorized_native_epoch']==epoch
    return (bool(epoch) and row['native_gate']==row['in_flight_generation']
            and row['authorized_native_epoch']<paused
            and bool(row['authorized_native_epoch'])==bool(row['resume_playback_revision'])
            and row['resume_playback_revision']<=row['playback_revision']
            and row['resume_granted']==granted
            and row['request_start_ns']<=row['request_end_ns']<=row['issue_ns']
            and row['request_end_ns']-row['request_start_ns']<=50_000_000
            and row['issue_ns']<=row['request_start_ns']+100_000_000
            and row['expiry_ns']==row['request_start_ns']+500_000_000
            and row['output_host_ns']>=row['issue_ns']
            and abs(row['applied_gain']-kodi_gain(row['volume'],row['muted']))<=1e-12)


def _valid_terminal(row):
    unsigned=('source_end_ns','hold_until_ns','observed_mach_ns','core_failure')
    flags=('failed','exposed_context_retained_until_exit','quarantined')
    if (any(not _uint(row.get(k)) for k in unsigned)
            or any(type(row.get(k)) is not bool for k in flags)
            or not _controls_only(row,required=True)
            or row['exposed_context_retained_until_exit'] is not True
            or type(row.get('native_control_reason')) is not str or not row['native_control_reason']
            or row['core_failure']>10 or not row['observed_mach_ns']
            or type(row.get('cleanup_result')) is not int or row['cleanup_result'] not in (-1,0,1,2)):
        return False
    end=row['source_end_ns']
    if (row['hold_until_ns']!=(end+1_000_000_000 if end else 0)
            or row['observed_mach_ns']<end):
        return False
    reason=row.get('reason')
    if reason=='natural_eof':
        return bool(end) and not row['failed'] and row['core_failure']==0 and row['cleanup_result']==-1
    if reason=='retired':
        return end==0 and row['cleanup_result']==-1
    if reason=='cleanup_clean':
        return row['cleanup_result']==0
    if reason=='cleanup_failed':
        return row['cleanup_result'] in (1,2) and row['failed']
    return False


@dataclass(frozen=True)
class GainCommand:
    nonce: str
    incarnation: str
    timeline_sha256: str
    device_generation: int
    revision: int
    effective_epoch: int
    request_start_ns: int
    request_end_ns: int
    issue_ns: int
    expiry_ns: int
    volume: int
    muted: bool
    multiplier: float
    output_backend: str = 'hal_callback_v1'
    hal_contract_version: int = 1

    def wire(self):
        return (json.dumps(asdict(self),sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('ascii')


class HalGainController:
    """One control owner brackets readbacks; Monitor callbacks only set a hint."""
    def __init__(self, rpc, transport, now_ns, before_publish=None):
        self.rpc, self.transport, self.now_ns = rpc, transport, now_ns
        self.before_publish=before_publish
        self._owner = threading.Lock()
        self._hints = threading.Lock()
        self._hint = 1
        self._seen = 0
        self._last_start = None
        self._last_value = None
        self._epoch = 0
        self._commands = []
        self._ack = 0
        self.retired = False
        self.reason = 'waiting'

    def notify(self, sender, method, data):
        # Kodi queues this twice and supplies no origin timestamp. Never use data.
        if sender == 'xbmc' and method == 'Application.OnVolumeChanged':
            with self._hints:
                self._hint += 1

    def _retire(self, reason):
        self.retired, self.reason = True, reason
        retire = getattr(self.transport,'retire',None)
        if retire:
            retire(reason)

    def reconcile(self):
        with self._owner:
            if self.retired:
                return None
            with self._hints:
                hint = self._hint
            start = self.now_ns()
            if self._last_start is not None and hint == self._seen and start-self._last_start < 100_000_000:
                return None
            try:
                if not _uint(start) or (self._last_start is not None and start <= self._last_start):
                    raise ValueError('gain_time_regression')
                value = self.rpc('Application.GetProperties',{'properties':['volume','muted']})
                end = self.now_ns()
                if not _uint(end) or not 0 <= end-start <= 50_000_000:
                    raise ValueError('gain_query_width')
                if not isinstance(value,dict):
                    raise ValueError('gain_properties')
                multiplier = kodi_gain(value.get('volume'),value.get('muted'))
                issue = self.now_ns()
                if not _uint(issue) or not end <= issue <= start+100_000_000:
                    raise ValueError('gain_initial_age')
                observed = (multiplier,value['muted'])
                epoch = self._epoch + (observed != self._last_value)
                b = self.transport.binding
                command = GainCommand(b.nonce,b.incarnation,b.timeline_sha256,
                    self.transport.device_generation,len(self._commands)+1,epoch,start,end,issue,
                    start+500_000_000,value['volume'],value['muted'],multiplier)
                if not _hex(b.incarnation,32):
                    raise ValueError('gain_not_bound')
                if self.before_publish:self.before_publish(command)
                self.transport.write_gain(command)
                self._commands.append(command)
                self._last_start, self._last_value, self._epoch, self._seen = start, observed, epoch, hint
                return command
            except (OSError,ValueError,KeyError,OwnedTransportError,RuntimeError):
                self._retire('gain_readback_or_transport')
                return None

    def acknowledge(self, row):
        with self._owner:
            if not _valid_gain_ack(row):
                self._retire('gain_ack_schema'); return False
            revision = row.get('gain_revision')
            if (not _uint(revision) or not self._ack < revision <= len(self._commands) or not revision):
                self._retire('gain_ack_sequence'); return False
            c = self._commands[revision-1]
            bracket=('request_start_ns','request_end_ns','issue_ns','expiry_ns')
            if (any(type(row.get(k)) is not int or row[k] != getattr(c,k) for k in bracket)
                    or type(row.get('hal_contract_version')) is not int or row['hal_contract_version'] != 1
                    or any(not _uint(row.get(k)) or not row[k] for k in ('in_flight_generation',
                        'playback_revision','playback_epoch','segment_sequence'))
                    or row.get('nonce') != c.nonce or row.get('incarnation') != c.incarnation
                    or row.get('timeline_sha256') != c.timeline_sha256
                    or row.get('output_backend') != c.output_backend
                    or type(row.get('device_generation')) is not int or row['device_generation'] != c.device_generation
                    or type(row.get('gain_epoch')) is not int or row['gain_epoch'] != c.effective_epoch
                    or type(row.get('volume')) is not int or row['volume'] != c.volume
                    or type(row.get('muted')) is not bool or row['muted'] != c.muted
                    or type(row.get('applied_gain')) not in (float,int) or row['applied_gain'] != c.multiplier
                    or not _uint(row.get('output_host_ns')) or row['output_host_ns'] < c.issue_ns
                    or not _uint(row.get('source_start'))):
                self._retire('gain_ack_binding'); return False
            self._ack = revision
            return not self.retired

    def close(self):
        with self._owner:
            self._retire('gain_controller_exit')


class HalControlSession:
    """Playback acknowledgements for controls trials, deliberately no clock API."""
    def __init__(self, binding, now_ns):
        if binding.output_backend != 'hal_callback_v1':
            raise ValueError('hal_session_backend_mismatch')
        self.binding, self.now_ns = binding, now_ns
        self.lock = threading.RLock()
        self._hello = None
        self._commands = [OwnedCommand(binding.nonce,binding.incarnation,1,'waiting',binding.launch_mach_ns)]
        self._ack = 0
        self._sequence = 0
        self.generation = 0
        self.retired = False
        self.reason = 'waiting_hello'
        self.device_generation = None
        self.source_ended = False

    def retire(self, reason):
        with self.lock:
            if not self.retired:
                self.retired, self.reason = True, reason
                self.generation += 1

    def route(self, selected_url):
        if selected_url != self.binding.selected_url or selected_url != URL:
            self.retire('route_mismatch')

    def bind_hello(self, hello):
        with self.lock:
            b = self.binding
            expected = dict(protocol=1,nonce=b.nonce,timeline_sha256=b.timeline_sha256,
                first_packet_sha256=b.first_packet_sha256,native_pid=b.native_pid,
                player_mode=b.player_mode,requested_core=b.requested_core,
                video_queue_tenths=b.video_queue_tenths,video_extradata_hex=b.video_extradata_hex,
                output_backend=b.output_backend,hal_contract_version=b.hal_contract_version,
                device_uid=b.device_uid,device_format=b.device_format,lifecycle_capability=b.lifecycle_capability,
                stream_generation=1,device_generation=1,qualification_scope='controlled_fixture',hal_phase=b.hal_phase)
            if b.hal_phase=='clock':expected['hal_clock_version']=b.hal_clock_version
            if (not isinstance(hello,dict) or any(type(hello.get(k)) is not type(v) or hello[k]!=v for k,v in expected.items())
                    or any(type(hello['device_format'].get(k)) is not type(v) for k,v in b.device_format.items())
                    or not _hex(hello.get('incarnation'),32)
                    or (b.incarnation is not None and b.incarnation != hello['incarnation'])
                    or not _uint(hello.get('native_open_mach_ns'))
                    or not b.launch_mach_ns <= hello['native_open_mach_ns'] <= self.now_ns()
                    or (self._hello is not None and hello != self._hello)):
                self.retire('hello_mismatch'); return False
            self.binding = replace(b,incarnation=hello['incarnation'])
            self._hello = dict(hello)
            self.device_generation = 1
            self._commands = [replace(c,incarnation=hello['incarnation']) for c in self._commands]
            if any(c.issued_mach_ns < hello['native_open_mach_ns'] for c in self._commands[1:]):
                self.retire('control_before_open')
            return not self.retired

    def control(self, state, issued_ns=None):
        with self.lock:
            issued = self.now_ns() if issued_ns is None else issued_ns
            previous = self._commands[-1]
            if (state not in ('play','pause','resume','stop') or not _uint(issued)
                    or issued <= previous.issued_mach_ns or len(self._commands)>=64
                    or (self._hello and issued < self._hello['native_open_mach_ns'])
                    or previous.state == 'stop' or (state == 'play' and previous.state != 'waiting')
                    or (state in ('pause','resume') and previous.state == 'waiting')):
                self.retire('invalid_callback_control'); return None
            c = OwnedCommand(self.binding.nonce,self.binding.incarnation,len(self._commands)+1,state,issued)
            self._commands.append(c); self.generation += 1
            if state == 'stop': self.retire('callback_stop')
            return c

    def pending_commands(self):
        with self.lock:
            return tuple(self._commands) if self._hello else ()

    def ingest(self, raw):
        with self.lock:
            row = _json(raw)
            row['raw_sha256'] = hashlib.sha256(raw).hexdigest()
            b = self.binding
            if (self._hello is None or row.get('kind') not in ('hal_segment','hal_gain_ack','hal_terminal')
                    or any(row.get(k)!=v for k,v in dict(nonce=b.nonce,incarnation=b.incarnation,
                        timeline_sha256=b.timeline_sha256,output_backend=b.output_backend).items())):
                self.retire('hal_record_binding'); return None
            if row['kind'] == 'hal_gain_ack':
                if not _valid_gain_ack(row):
                    self.retire('hal_gain_ack_schema'); return None
                return row
            if row['kind'] == 'hal_terminal':
                if not _valid_terminal(row):
                    self.retire('hal_terminal_schema'); return None
                if row['reason'] == 'natural_eof':
                    self.source_ended = True
                    self.reason = 'natural_eof_hold'
                    return row
                self.retire('native_terminal'); return row
            if row['kind'] == 'hal_segment':
                integers=('sequence','schema_version','hal_contract_version','device_generation','stream_generation',
                    'in_flight_generation','playback_revision','playback_epoch','gain_revision','gain_epoch','source_start',
                    'source_count','requested_frames','consumed_frames','copied_frames','zero_frames','output_flags',
                    'now_flags','output_ticks','now_ticks','entry_ticks','exit_ticks','timebase_numer','timebase_denom',
                    'output_host_ns','source_end_ns','reason','publication_sequence','applied_intent','desired_intent',
                    'native_gate','authorized_native_epoch','resume_playback_revision','request_start_ns','request_end_ns',
                    'issue_ns','expiry_ns','volume')
                flags=('muted','playing','terminal','control_acquired','clock_authority','beacons_enabled',
                       'requested_playing','resume_granted','started','sample_zero_proven')
                if (any(not _uint(row.get(k)) for k in integers)
                        or any(type(row.get(k)) is not bool for k in flags)
                        or type(row.get('applied_gain')) not in (int,float) or not 0<=row['applied_gain']<=1
                        or type(row.get('output_sample_time')) not in (int,float) or not 0<=row['output_sample_time']<=2**53-1
                        or any(not math.isfinite(row[k]) for k in ('applied_gain','output_sample_time'))
                        or row['schema_version']!=1 or row['hal_contract_version']!=1
                        or row['requested_frames']!=512 or row['source_count']!=row['consumed_frames']
                        or row['copied_frames']!=row['consumed_frames']
                        or row['copied_frames']+row['zero_frames']!=512
                        or row['clock_authority'] or row['beacons_enabled']):
                    self.retire('hal_record_types'); return None
                sequence = row.get('sequence')
                if type(sequence) is not int or sequence != self._sequence+1:
                    self.retire('hal_segment_sequence'); return None
                self._sequence = sequence
                if row.get('control_acquired') is not True:
                    return row
                revision = row.get('playback_revision')
                if (not _uint(revision) or not self._ack <= revision <= len(self._commands) or not revision
                        or not _uint(row.get('output_host_ns'))
                        or row['output_host_ns'] < self._commands[revision-1].issued_mach_ns):
                    self.retire('hal_playback_ack'); return None
                command = self._commands[revision-1]
                if command.state not in ('play','resume') or (row['playing'] and row['resume_granted']):
                    self._ack = revision
            return row
