# SPDX-License-Identifier: GPL-2.0-or-later
"""Session-owned controlled-fixture clock. No public Kodi time fallback.

The injected estimator factory must load the verified frozen V2 implementation.
Callbacks only mutate bounded memory; the monitoring loop owns transport IO.
"""
import ctypes
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import uuid

URL = 'http://127.0.0.1:18789/live/index.m3u8'
SESSION_ROOT = Path('/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer/clock-sessions')
FRESH_NS = 100_000_000
MAX_QUERY_NS = 5_000_000
TOTAL_SAMPLES = 8_641_536


def _mach_clock():
    if sys.platform != 'darwin':
        return lambda: (_ for _ in ()).throw(RuntimeError('Mach clock requires macOS'))
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    info = Timebase()
    lib.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
    lib.mach_timebase_info.restype = ctypes.c_int
    if lib.mach_timebase_info(ctypes.byref(info)) or not info.denom:
        raise RuntimeError('Mach timebase unavailable')
    lib.mach_absolute_time.argtypes = []
    lib.mach_absolute_time.restype = ctypes.c_uint64
    return lambda: lib.mach_absolute_time()*info.numer//info.denom


mach_host_ns = _mach_clock()


def _uint(value):
    return type(value) is int and 0 <= value <= 2**63-1


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value


@dataclass(frozen=True)
class OwnedClockBinding:
    nonce: str
    incarnation: object
    timeline_sha256: str
    first_packet_sha256: str
    selected_url: str
    launch_mach_ns: int
    native_pid: int
    rate: int = 48000
    source_origin: int = 0

    def __post_init__(self):
        if (not _hex(self.nonce,32) or (self.incarnation is not None and not _hex(self.incarnation,32))
                or not _hex(self.timeline_sha256,64) or not _hex(self.first_packet_sha256,64)
                or not _uint(self.launch_mach_ns) or not _uint(self.native_pid) or not self.native_pid
                or type(self.rate) is not int or self.rate != 48000
                or type(self.source_origin) is not int or self.source_origin != 0):
            raise ValueError('invalid_owned_binding')


@dataclass(frozen=True)
class OwnedCommand:
    nonce: str
    incarnation: object
    revision: int
    state: str
    issued_mach_ns: int

    def wire(self):
        if self.incarnation is None:
            raise ValueError('command_not_bound')
        return ('v1 %s %s %d %s %d\n' % (self.nonce,self.incarnation,self.revision,
                                        self.state,self.issued_mach_ns)).encode('ascii')


@dataclass(frozen=True)
class OwnedClockSnapshot:
    valid: bool = False
    reasons: tuple = ('waiting',)
    seconds: object = None
    query_host_ns: object = None
    row_sequence: object = None
    row_sha256: object = None
    native_generation: object = None
    generation: int = 0
    authorization_ns: object = None


class OwnedClockProvider:
    def __init__(self, binding, estimator_factory, now_ns=mach_host_ns):
        self.binding, self.now_ns = binding, now_ns
        self.estimator = estimator_factory()
        self.lock = threading.RLock()
        self.generation = 0
        self.retired = False
        self.reason = 'waiting_hello'
        self._hello = None
        self._commands = [OwnedCommand(binding.nonce,binding.incarnation,1,'waiting',binding.launch_mach_ns)]
        self._sequence = 0
        self._ack = 0
        self._ack_applied = 0
        self._native_generation = self._guard_generation = None
        self._epoch = self._counter = None
        self._last_query = None
        self._last = OwnedClockSnapshot()
        if binding.selected_url != URL:
            self.retire('route_mismatch')

    def _invalidate(self, reason):
        self.reason = reason
        self.estimator.clear_anchor()
        self._last = OwnedClockSnapshot(reasons=(reason,),generation=self.generation)

    def retire(self, reason):
        with self.lock:
            if not self.retired:
                self.retired = True
                self.generation += 1
                self._invalidate(reason)

    def route(self, selected_url):
        if selected_url != URL or selected_url != self.binding.selected_url:
            self.retire('route_mismatch')

    def bind_hello(self, hello):
        with self.lock:
            b = self.binding
            valid = (isinstance(hello,dict) and type(hello.get('protocol')) is int and hello['protocol']==1
                and hello.get('nonce')==b.nonce and _hex(hello.get('incarnation'),32)
                and (b.incarnation is None or hello['incarnation']==b.incarnation)
                and hello.get('timeline_sha256')==b.timeline_sha256
                and hello.get('first_packet_sha256')==b.first_packet_sha256
                and type(hello.get('native_pid')) is int and hello['native_pid']==b.native_pid
                and type(hello.get('stream_generation')) is int and hello['stream_generation']==1
                and hello.get('qualification_scope')=='controlled_fixture'
                and _uint(hello.get('native_open_mach_ns'))
                and b.launch_mach_ns <= hello['native_open_mach_ns'] <= self.now_ns())
            if not valid or (self._hello is not None and self._hello != hello):
                self.retire('hello_mismatch')
                return False
            # Even retired evidence can bind controls so a valid pause is not lost.
            self.binding = replace(b, incarnation=hello['incarnation'])
            self._hello = dict(hello)
            self._commands = [replace(c,incarnation=hello['incarnation']) for c in self._commands]
            if any(c.issued_mach_ns < hello['native_open_mach_ns'] for c in self._commands[1:]):
                self.retire('control_before_open')
            return not self.retired

    def control(self, state, issued_ns=None):
        """Invalidate synchronously; never perform transport or await native ack."""
        with self.lock:
            issued = self.now_ns() if issued_ns is None else issued_ns
            if (state not in ('play','pause','resume','stop') or not _uint(issued)
                    or issued <= self._commands[-1].issued_mach_ns or len(self._commands)>=64
                    or (self._hello and issued < self._hello['native_open_mach_ns'])
                    or self._commands[-1].state=='stop'
                    or (state=='play' and self._commands[-1].state!='waiting')
                    or (state in ('pause','resume') and self._commands[-1].state=='waiting')):
                self.retire('invalid_callback_control')
                return None
            command = OwnedCommand(self.binding.nonce,self.binding.incarnation,
                                   len(self._commands)+1,state,issued)
            self._commands.append(command)
            if not self.retired:
                self.generation += 1
                self._invalidate('callback_'+state)
            if state=='stop':
                self.retire('callback_stop')
            return command

    def pending_commands(self):
        with self.lock:
            return tuple(self._commands) if self._hello else ()

    def ingest(self, raw):
        """Consume every complete clock line exactly once, before any estimator update."""
        with self.lock:
            digest = hashlib.sha256(raw).hexdigest()
            try:
                if len(raw)>16384:
                    raise ValueError('row_bound')
                row = _json(raw)
            except (ValueError,UnicodeError,RecursionError):
                self.retire('malformed_row')
                return self.snapshot()
            if row.get('kind') != 'clock_sample':
                return self.snapshot()
            own = row.get('ownership')
            # Terminal observations take precedence even if their timing numbers are broken.
            if (row.get('failed') is True or row.get('drained') is True
                    or row.get('control_state') in ('stop','drained')):
                self.retire('native_terminal')
            if self.retired:
                return self.snapshot()
            if not self._hello or not isinstance(own,dict):
                self.retire('unbound_row')
                return self.snapshot()
            b = self.binding
            integers = ('protocol','observation_sequence','control_revision','control_issued_mach_ns',
                        'control_applied_mach_ns','state_generation_before','state_generation_after',
                        'stream_generation','output_guard_generation')
            if (any(not _uint(own.get(k)) for k in integers) or own['protocol']!=1
                    or own.get('nonce')!=b.nonce or own.get('incarnation')!=b.incarnation
                    or own.get('timeline_sha256')!=b.timeline_sha256 or own['stream_generation']!=1
                    or any(type(own.get(k)) is not bool for k in ('eligible','source_zero_confirmed','output_guard_valid'))
                    or own.get('output_guard_scope')!='fixture_default_device_observation'
                    or own['observation_sequence'] != self._sequence+1):
                self.retire('ownership_or_sequence')
                return self.snapshot()
            self._sequence = own['observation_sequence']
            reason = own.get('reason')
            pending = ('eligible','output_guard_unarmed','qos_unconfirmed','generation_changed',
                       'source_unconfirmed','speed_resume_required','control_inactive')
            if reason not in pending or (not own['output_guard_valid'] and own['output_guard_generation']!=1):
                self.retire('native_retired')
                return self.snapshot()
            if (own['source_zero_confirmed'] and
                    (type(row.get('first_source_pts')) not in (int,float) or row['first_source_pts']!=0)):
                self.retire('source_zero_mismatch')
                return self.snapshot()
            revision = own['control_revision']
            if revision > len(self._commands) or revision < self._ack or revision > self._ack+1:
                self.retire('control_ack_sequence')
                return self.snapshot()
            if revision:
                command = self._commands[revision-1]
                if (own['control_state']!=command.state or own['control_issued_mach_ns']!=command.issued_mach_ns
                        or own['control_applied_mach_ns'] < max(command.issued_mach_ns,self._hello['native_open_mach_ns'])
                        or (revision==self._ack and own['control_applied_mach_ns']!=self._ack_applied)
                        or (revision>self._ack and own['control_applied_mach_ns']<self._ack_applied)):
                    self.retire('control_ack_mismatch')
                    return self.snapshot()
            elif own['control_state']!='unbound' or own['control_issued_mach_ns'] or own['control_applied_mach_ns']:
                self.retire('control_ack_mismatch')
                return self.snapshot()
            self._ack, self._ack_applied = revision, own['control_applied_mach_ns']
            before, after = own['state_generation_before'], own['state_generation_after']
            guard = own['output_guard_generation']
            epoch, counter = row.get('source_epoch'), row.get('accepted_adddata_frames')
            # Independent irreversible evidence cannot be hidden by a broken delay,
            # query timestamp or playback flag in the same owned observation.
            if ((_uint(epoch) and self._epoch is not None and epoch!=self._epoch)
                    or (_uint(counter) and self._counter is not None and counter<self._counter)
                    or (self._native_generation is not None and after<self._native_generation)
                    or (self._guard_generation is not None and guard<self._guard_generation)
                    or (_uint(counter) and counter>TOTAL_SAMPLES)):
                self.retire('epoch_counter_or_generation_rewind')
                return self.snapshot()
            ints = ('steady_ns','mach_absolute_ns','delay_query_start_steady_ns',
                    'delay_query_end_steady_ns','accepted_adddata_frames','source_epoch')
            delay = row.get('audioengine_delay_s')
            if (any(not _uint(row.get(k)) for k in ints)
                    or any(type(row.get(k)) is not bool for k in ('playback_started','failed','drained','renderer_qos_request_confirmed'))
                    or type(delay) not in (float,int) or not 0 <= delay <= 2**63-1 or not math.isfinite(delay)):
                self._invalidate('invalid_schema')
                return self.snapshot()
            if self._native_generation != after or self._guard_generation != guard:
                self.generation += 1
                self.estimator.clear_anchor()
            self._native_generation,self._guard_generation = after,guard
            qs,qe = row['delay_query_start_steady_ns'],row['delay_query_end_steady_ns']
            start = row['mach_absolute_ns']+qs-row['steady_ns']
            query = row['mach_absolute_ns']+(qs+qe)//2-row['steady_ns']
            now = self.now_ns()
            reasons = []
            if not 0 <= now-query <= FRESH_NS or (self._last_query is not None and query<=self._last_query):
                reasons.append('query_freshness')
            self._last_query = query
            if not 0 <= qe-qs <= MAX_QUERY_NS:
                reasons.append('query_width')
            if (revision != len(self._commands) or self._commands[-1].state not in ('play','resume')
                    or row.get('control_state') != own['control_state'] or start <= self._ack_applied):
                reasons.append('control_pending')
            if (before!=after or not own['eligible'] or reason!='eligible' or not own['source_zero_confirmed']
                    or not own['output_guard_valid'] or not row['renderer_qos_request_confirmed']
                    or not row['playback_started'] or row.get('first_source_pts')!=0):
                reasons.append('native_unqualified')
            if reasons:
                self.estimator.clear_anchor()
                result = None
            else:
                result = self.estimator.update(row)
                reasons.extend(result['reasons'])
            checked = self.now_ns()
            if not 0 <= checked-query <= FRESH_NS:
                reasons.append('evaluation_freshness')
                self.estimator.clear_anchor()
            elif result is not None:
                # Only fully validated, fresh estimator inputs establish or advance
                # source baselines; stale/future rows cannot poison later checks.
                self._epoch,self._counter = epoch,counter
            self._last = OwnedClockSnapshot(not reasons,tuple(dict.fromkeys(reasons)),
                result['seconds'] if result and not reasons else None,query,self._sequence,digest,
                after,self.generation,checked)
            return self._last

    def snapshot(self):
        with self.lock:
            now = self.now_ns()
            last = self._last
            reasons = list(last.reasons)
            if self.retired:
                reasons.append(self.reason)
            if last.generation != self.generation:
                reasons.append('superseded_generation')
            if last.query_host_ns is None or not 0 <= now-last.query_host_ns <= FRESH_NS:
                reasons.append('snapshot_freshness')
            return replace(last,valid=last.valid and not reasons,seconds=last.seconds if last.valid and not reasons else None,
                           reasons=tuple(dict.fromkeys(reasons)),authorization_ns=now)

    def authorize(self, evidence, action):
        """Recheck and run a bounded in-memory decision under the callback lock."""
        with self.lock:
            current = self.snapshot()
            if (not current.valid or current.generation!=evidence.generation
                    or not evidence.valid or current.native_generation!=evidence.native_generation
                    or not 0 <= self.now_ns()-evidence.query_host_ns <= FRESH_NS):
                return False
            return action(current)


class OwnedTransportError(RuntimeError):
    pass


def _identity(st):
    return st.st_dev,st.st_ino


def _private(st, directory=False):
    return (st.st_uid==os.getuid() and stat.S_IMODE(st.st_mode)==(0o700 if directory else 0o600)
            and (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink==1))


def _open_directory(path):
    """Walk without following even an intermediate symlink."""
    fd = os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in Path(path).absolute().parts[1:]:
            next_fd = os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rename_exclusive(fd, source, destination):
    # RENAME_EXCL is the same macOS primitive used by the native hello publisher.
    if sys.platform != 'darwin':
        raise OwnedTransportError('exclusive_rename_requires_macos')
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    rename = lib.renameatx_np
    rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(fd,source.encode(),fd,destination.encode(),0x00000004):
        code = ctypes.get_errno()
        raise OSError(code,os.strerror(code))


class OwnedClockTransport:
    """Pinned private session, immutable hello/commands, bounded append-only reads."""
    @classmethod
    def create(cls, root, binding):
        root = Path(root)
        parent = _open_directory(root)
        try:
            os.mkdir(binding.nonce,0o700,dir_fd=parent)
        finally:
            os.close(parent)
        return cls(root/binding.nonce)

    def __init__(self, directory):
        self.directory = Path(directory)
        self.fd = _open_directory(directory)
        self.identity = os.fstat(self.fd)
        if not _private(self.identity,True):
            os.close(self.fd)
            raise OwnedTransportError('session_permissions')
        self.hello = None
        self.ledger = None
        self.ledger_identity = None
        self.offset = 0
        self.pending = b''
        self.commands = {}
        self.failed = False

    def _check_directory(self):
        fd = _open_directory(self.directory)
        try:
            st = os.fstat(fd)
            if not _private(st,True) or _identity(st)!=_identity(self.identity):
                raise OwnedTransportError('session_replaced')
        finally:
            os.close(fd)

    def _read_immutable(self, name, limit):
        fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            if not _private(before) or before.st_size>limit:
                raise OwnedTransportError('immutable_file_bound_or_type')
            raw = os.read(fd,limit+1)
            after = os.fstat(fd)
            current = os.stat(name,dir_fd=self.fd,follow_symlinks=False)
            fingerprint = lambda s: (_identity(s),s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if fingerprint(before)!=fingerprint(after) or fingerprint(after)!=fingerprint(current) or len(raw)!=before.st_size:
                raise OwnedTransportError('immutable_file_changed')
            return raw,fingerprint(after)
        finally:
            os.close(fd)

    def poll(self, provider):
        try:
            if self.failed:
                raise OwnedTransportError('transport_retired')
            self._check_directory()
            try:
                hello = self._read_immutable('hello.json',2048)
            except FileNotFoundError:
                if self.hello is not None:
                    raise OwnedTransportError('hello_removed')
                return ()
            if self.hello is not None and hello!=self.hello:
                raise OwnedTransportError('hello_changed')
            if self.hello is None:
                self.hello = hello
                provider.bind_hello(_json(hello[0]))
            for name,expected in self.commands.items():
                if self._read_immutable(name,512)!=expected:
                    raise OwnedTransportError('command_changed')
            st = os.stat('native.jsonl',dir_fd=self.fd,follow_symlinks=False)
            if not _private(st):
                raise OwnedTransportError('ledger_type')
            if self.ledger is None:
                self.ledger = os.open('native.jsonl',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
                self.ledger_identity = _identity(os.fstat(self.ledger))
            if _identity(st)!=self.ledger_identity or not _private(os.fstat(self.ledger)):
                raise OwnedTransportError('ledger_replaced')
            if st.st_size<self.offset or st.st_size-self.offset>262144:
                raise OwnedTransportError('ledger_size_or_backlog')
            data = os.read(self.ledger,262145)
            if len(data)>262144:
                raise OwnedTransportError('ledger_batch_bound')
            self.offset += len(data)
            current = os.stat('native.jsonl',dir_fd=self.fd,follow_symlinks=False)
            if not _private(current) or _identity(current)!=self.ledger_identity or current.st_size<self.offset:
                raise OwnedTransportError('ledger_changed_during_read')
            self._check_directory()
            lines = (self.pending+data).split(b'\n')
            self.pending = lines.pop()
            if any(len(line)>16384 for line in lines+[self.pending]):
                raise OwnedTransportError('ledger_line_bound')
            result = []
            for line in lines:
                row = _json(line)
                if row.get('kind')=='clock_sample':
                    result.append(provider.ingest(line))
            return tuple(result)
        except (OSError,ValueError,UnicodeError,RecursionError,OwnedTransportError) as exc:
            self.failed = True
            provider.retire('transport_invalid')
            raise OwnedTransportError('transport_invalid') from exc

    def flush_commands(self, provider):
        """Monitoring loop only. Hidden temp then macOS exclusive atomic rename."""
        try:
            self._check_directory()
            for command in provider.pending_commands():
                name = 'command-%06d.txt' % command.revision
                if name in self.commands:
                    continue
                temp = '.command-'+uuid.uuid4().hex+'.tmp'
                fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
                try:
                    wire = command.wire()
                    os.fchmod(fd,0o600)
                    if os.write(fd,wire)!=len(wire):
                        raise OwnedTransportError('command_short_write')
                finally:
                    os.close(fd)
                _rename_exclusive(self.fd,temp,name)
                self.commands[name] = self._read_immutable(name,512)
        except (OSError,ValueError,OwnedTransportError) as exc:
            provider.retire('command_transport_invalid')
            raise OwnedTransportError('command_transport_invalid') from exc

    def close(self):
        if self.ledger is not None:
            os.close(self.ledger)
            self.ledger = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
