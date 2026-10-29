# SPDX-License-Identifier: GPL-2.0-or-later
"""Exact-next native probes. No ledger reads, directory scans or state-lock IO."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from collections import deque

from owned_clock import _uint, _hex, _json, _identity, _private, _open_directory, _rename_exclusive

LIMIT=32768
MAX_BYTES=4096
FIELDS=('query_host_ns','query_start_ns','query_end_ns','row_sequence','row_sha256','segment_sha256',
        'native_generation','effective_intent','playback_epoch','gain_epoch','device_generation','stream_generation',
        'source_zero_sequence','source_zero_sha256','source_zero_ticks','timebase_numer','timebase_denom','initial_deadline_ns')
RESPONSE_FIELDS=('request_sha256','authorized','reason','check_start_ns','check_end_ns','actual_gate','actual_intent',
                 'applied_sequence','applied_sha256','callback_entry_ns','lease_expiry_ns','source_end_ns')


def canonical(row):return json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
def sha(raw):return hashlib.sha256(raw).hexdigest()


def valid_times(row,now):
    fields=('request_start_ns','deadline_ns','query_host_ns','check_start_ns','check_end_ns','callback_entry_ns','lease_expiry_ns','source_end_ns')
    if not _uint(now) or any(not _uint(row.get(k)) for k in fields):return False
    start,end=row['check_start_ns'],row['check_end_ns']
    return (row['request_start_ns']<=start<=end<=now<=row['deadline_ns']
        and now-row['request_start_ns']<=20_000_000 and end-start<=5_000_000 and now-end<=5_000_000
        and 0<=end-row['callback_entry_ns']<=40_000_000 and 0<=now-row['query_host_ns']<=100_000_000
        and now<row['lease_expiry_ns'] and (not row['source_end_ns'] or now<row['source_end_ns']))


@dataclass(frozen=True)
class Proof:
    request_items: tuple
    response_items: tuple
    response_sha256: str
    @property
    def request(self):return dict(self.request_items)
    @property
    def response(self):return dict(self.response_items)


class FairAuthorizationSlot:
    """FIFO admission for the single exact-next native exchange.

    The caller supplies the deadline sampled before it joins this queue.  The
    slot never extends that deadline; it only prevents a late waiter from
    barging ahead of an earlier one.  It is deliberately independent from
    provider, tracker, gain and beacon-worker locks.
    """
    def __init__(self):
        self._condition=threading.Condition();self._waiters=deque();self._held=False

    @property
    def pending(self):
        with self._condition:return len(self._waiters)

    def wake(self):
        with self._condition:self._condition.notify_all()

    def acquire(self,deadline_ns,now_ns,cancel):
        token=object()
        with self._condition:
            self._waiters.append(token)
            while True:
                now=now_ns()
                if cancel.is_set() or now>deadline_ns:
                    self._waiters.remove(token);self._condition.notify_all();return False
                if not self._held and self._waiters[0] is token:
                    self._waiters.popleft();self._held=True;return True
                self._condition.wait(min(.001,max(0,(deadline_ns-now)/1e9)))

    def release(self,before=None):
        with self._condition:
            if not self._held:raise RuntimeError('authorization_slot_release')
            try:
                if before:before()
            finally:
                self._held=False;self._condition.notify_all()

    def try_acquire(self,allow_cancelled_waiters=False):
        with self._condition:
            if self._held or (self._waiters and not allow_cancelled_waiters):return False
            self._held=True;return True


class HalAuthorizationTransport:
    def __init__(self,session,now_ns,create=True,emit=None):
        self.path=Path(session)/'authorization';self.now_ns=now_ns
        if create:self.path.mkdir(mode=0o700)
        self.fd=_open_directory(self.path)
        try:
            self.identity=os.fstat(self.fd)
            if not _private(self.identity,True):raise ValueError('authorization_permissions')
        except BaseException:
            try:os.close(self.fd)
            except OSError:pass
            self.fd=None
            raise
        self.slot=FairAuthorizationSlot();self.cancel=threading.Event();self.closing=False
        self.count=0;self.failed=False;self.emit=emit

    def _check(self):
        current=os.stat(self.path,follow_symlinks=False)
        if not _private(current,True) or _identity(current)!=_identity(self.identity):raise ValueError('authorization_replaced')

    def _write(self,name,raw):
        self._check()
        if len(raw)>MAX_BYTES:raise ValueError('authorization_size')
        temp='.request-'+uuid.uuid4().hex
        fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
        try:
            os.fchmod(fd,0o600)
            if os.write(fd,raw)!=len(raw):raise ValueError('authorization_short_write')
        finally:os.close(fd)
        _rename_exclusive(self.fd,temp,name);self._check()

    def _read(self,name):
        self._check();fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
        try:
            before=os.fstat(fd)
            if not _private(before) or not 0<before.st_size<=MAX_BYTES:raise ValueError('authorization_file')
            raw=os.read(fd,MAX_BYTES+1);after=os.fstat(fd);current=os.stat(name,dir_fd=self.fd,follow_symlinks=False)
            fingerprint=lambda s:(_identity(s),s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if len(raw)!=before.st_size or fingerprint(before)!=fingerprint(after) or fingerprint(after)!=fingerprint(current):raise ValueError('authorization_mutated')
        finally:os.close(fd)
        self._check();return raw

    def probe(self,binding,evidence,generation,stage,*,initial=False,decision_id='',event_digest='',parent=None,decision_ns=0,queue_ns=0,lease_expiry_ns):
        started=self.now_ns();deadline=min(started+20_000_000,evidence.query_host_ns+100_000_000,lease_expiry_ns-1)
        if initial and stage!='dispatch':deadline=min(deadline,evidence.initial_deadline_ns)
        acquired=False;request=None
        try:
            if stage not in ('tracker','queue','dispatch') or self.cancel.is_set() or self.failed: return None
            acquired=self.slot.acquire(deadline,self.now_ns,self.cancel)
            if not acquired:raise ValueError('authorization_slot_timeout')
            if self.cancel.is_set() or self.failed:return None
            if self.now_ns()>deadline:raise ValueError('authorization_slot_timeout')
            if self.count>=LIMIT:raise ValueError('authorization_exhausted')
            self.count+=1
            request={k:getattr(evidence,k) for k in FIELDS}
            request.update(nonce=binding.nonce,incarnation=binding.incarnation,timeline_sha256=binding.timeline_sha256,
                output_backend='hal_callback_v1',hal_phase='clock',hal_clock_version=1,request_id=self.count,
                expected_generation=generation,stage=stage,request_start_ns=started,deadline_ns=deadline,initial=int(initial),
                decision_id=decision_id,event_digest=event_digest,parent_id=parent.response['request_id'] if parent else 0,
                parent_sha256=parent.response_sha256 if parent else '',decision_ns=decision_ns,queue_ns=queue_ns)
            wire=canonical(request);self._write('request-%06d.json'%self.count,wire)
            while not self.cancel.is_set():
                if self.now_ns()>deadline:raise ValueError('authorization_timeout')
                try:raw=self._read('response-%06d.json'%self.count)
                except FileNotFoundError:self.cancel.wait(.0005);continue
                received=self.now_ns();response=_json(raw)
                if (set(response)!=set(request)|set(RESPONSE_FIELDS) or any(response[k]!=v or type(response[k]) is not type(v) for k,v in request.items())
                        or response['request_sha256']!=sha(wire) or type(response['authorized']) is not bool
                        or type(response['reason']) is not str or len(response['reason'])>80
                        or any(not _uint(response.get(k)) for k in RESPONSE_FIELDS if k not in ('request_sha256','authorized','reason','applied_sha256','actual_gate'))
                        or type(response['actual_gate']) is not int or not 0<=response['actual_gate']<=2**64-1
                        or not _hex(response['applied_sha256'],64) or received>deadline):raise ValueError('authorization_response_schema')
                if response['authorized'] and (not valid_times(response,received) or response['actual_gate']!=evidence.native_generation
                        or response['actual_intent']!=evidence.effective_intent):raise ValueError('authorization_response_authority')
                proof=Proof(tuple(request.items()),tuple(response.items()),sha(raw))
                if self.emit:self.emit(dict(kind='owned_hal_probe',received_ns=received,request_sha256=sha(wire),response_sha256=sha(raw),response=response))
                return proof
            return None
        except (OSError,ValueError,TypeError,KeyError,OverflowError):
            self.failed=True;self.cancel.set();return None
        finally:
            if acquired:
                def close_if_closing():
                    if self.closing and self.fd is not None:
                        os.close(self.fd);self.fd=None
                self.slot.release(close_if_closing)

    def cancel_now(self):self.cancel.set();self.slot.wake()
    def close(self):
        with self.slot._condition:self.closing=True
        self.cancel.set();self.slot.wake()
        # Cancellation is set before this claim, so queued waiters can only
        # remove their own tokens and cannot start an exchange.  Claiming even
        # while they drain closes the descriptor without relying on a holder's
        # deferred cleanup path.
        if self.slot.try_acquire(allow_cancelled_waiters=True):
            try:
                if self.fd is not None:os.close(self.fd);self.fd=None
            finally:self.slot.release()
