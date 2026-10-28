"""Bounded diagnostic bridge. No production eligibility or authenticated native owner."""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

URL = 'http://127.0.0.1:18789/live/index.m3u8'
SHA256 = '7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87'
FRESH_NS = 100_000_000

def frozen_module():
    path = Path(__file__).resolve().parent / 'clock-candidate-v2' / 'estimator.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('frozen estimator hash mismatch')
    spec = importlib.util.spec_from_file_location('owned_hls_frozen_v2', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class LedgerError(RuntimeError):
    pass

class LedgerReader:
    """Pin a fresh file at monitor launch, retaining every complete JSON row."""
    def __init__(self, path, max_line=16384, max_batch=262144):
        self.path = Path(path)
        self.preexisting = self.path.exists() and self.path.stat().st_size > 0
        self.max_line, self.max_batch = max_line, max_batch
        self.file = None
        self.identity = None
        self.offset = 0
        self.pending = b''
        self.sequence = 0
        self.failed = False

    def read(self):
        if self.failed:
            raise LedgerError('reader_retired')
        try:
            return self._read()
        except (OSError, ValueError, UnicodeError, LedgerError) as exc:
            self.failed = True
            raise LedgerError(str(exc)) from exc

    def _read(self):
        if self.preexisting:
            raise LedgerError('preexisting_nonempty_ledger')
        if not self.path.exists():
            if self.file is not None:
                raise LedgerError('ledger_removed')
            return []
        if self.file is None:
            self.file = self.path.open('rb')
            st = os.fstat(self.file.fileno())
            self.identity = (st.st_dev, st.st_ino)
        st = self.path.stat()
        if (st.st_dev, st.st_ino) != self.identity:
            raise LedgerError('ledger_replaced')
        if st.st_size < self.offset:
            raise LedgerError('ledger_truncated')
        if st.st_size-self.offset > self.max_batch:
            raise LedgerError('ledger_backlog_bound')
        data = self.file.read(self.max_batch+1)
        if len(data) > self.max_batch:
            raise LedgerError('ledger_batch_bound')
        self.offset += len(data)
        chunks = (self.pending+data).split(b'\n')
        self.pending = chunks.pop()
        if any(len(line) > self.max_line for line in chunks+[self.pending]):
            raise LedgerError('ledger_line_bound')
        result = []
        for line in chunks:
            self.sequence += 1
            row = json.loads(line)
            if not isinstance(row, dict):
                raise LedgerError('ledger_row_schema')
            result.append(dict(row=row, ledger_line=self.sequence,
                               row_sha256=hashlib.sha256(line).hexdigest(),
                               device=self.identity[0], inode=self.identity[1]))
        return result

    def close(self):
        if self.file:
            self.file.close()

class RuntimeClockSession:
    def __init__(self, selected_url, launch_ns=None):
        self.lock = threading.RLock()
        self.module = frozen_module()
        self.estimator = self.module.Estimator()
        self.launch_id = uuid.uuid4().hex
        self.launch_ns = time.monotonic_ns() if launch_ns is None else launch_ns
        self.selected_url = selected_url
        self.retired = selected_url != URL
        self.control_state = 'waiting'
        self.resume_ns = self.launch_ns
        self.last_epoch = self.last_counter = None
        self.last = None
        self.generation = 0
        self.reason = 'route_mismatch' if self.retired else 'waiting'

    def control(self, state, now=None):
        with self.lock:
            now = time.monotonic_ns() if now is None else now
            self.control_state = state
            self.reason = 'callback_'+state
            self.generation += 1
            self.estimator.clear_anchor()
            if state in ('play', 'resume'):
                self.resume_ns = now
            elif state not in ('waiting', 'pause'):
                self.retired = True
            self.last = None

    def route(self, url, now=None):
        if url != self.selected_url or url != URL:
            self.retire('route_mismatch')

    def retire(self, reason):
        with self.lock:
            self.retired = True
            self.reason = reason
            self.generation += 1
            self.last = None

    @staticmethod
    def schema(row):
        ints = ('steady_ns','mach_absolute_ns','delay_query_start_steady_ns',
                'delay_query_end_steady_ns','accepted_adddata_frames','source_epoch')
        if any(type(row.get(k)) is not int or not 0 <= row[k] <= 2**63-1 for k in ints):
            return False
        if any(type(row.get(k)) is not bool for k in ('playback_started','failed','drained')):
            return False
        d = row.get('audioengine_delay_s')
        return (type(d) in (int,float) and 0 <= d <= 2**63-1 and math.isfinite(d)
                and row.get('control_state') in ('waiting','play','resume','pause','stop'))

    def ingest(self, row, received_ns=None, identity=None):
        with self.lock:
            now = time.monotonic_ns() if received_ns is None else received_ns
            reasons = []
            estimate = None
            query = None
            if not self.schema(row):
                reasons.append('invalid_schema')
                self.estimator.clear_anchor()
            else:
                query = row['mach_absolute_ns'] + (row['delay_query_start_steady_ns']+row['delay_query_end_steady_ns'])//2-row['steady_ns']
                if self.last_epoch is not None and (row['source_epoch'] != self.last_epoch or row['accepted_adddata_frames'] < self.last_counter):
                    self.retire('epoch_or_counter_change')
                self.last_epoch, self.last_counter = row['source_epoch'], row['accepted_adddata_frames']
                # Every finite native row reaches the frozen estimator, even when stale.
                estimate = self.estimator.update(row)
                if query < self.launch_ns or query <= self.resume_ns:
                    reasons.append('before_control_boundary')
                if not 0 <= now-query <= FRESH_NS:
                    reasons.append('query_freshness')
                if self.control_state not in ('play','resume') or row['control_state'] not in ('play','resume'):
                    reasons.append('control_not_playing')
                if row['failed'] or row['drained'] or row['control_state']=='stop':
                    self.retire('native_terminal')
                if reasons:
                    self.estimator.clear_anchor()
            if self.retired:
                reasons.append(self.reason)
            if estimate is not None:
                reasons.extend(estimate['reasons'])
            out = dict(kind='runtime_clock_input', launch_id=self.launch_id,
                       launch_ns=self.launch_ns, generation=self.generation,
                       identity=identity, input=row, query_host_ns=query,
                       received_ns=now, evaluated_ns=time.monotonic_ns() if received_ns is None else now,
                       estimate=estimate, valid=not reasons, reasons=list(dict.fromkeys(reasons)))
            self.last = out
            return out

    def snapshot(self, now=None):
        with self.lock:
            now = time.monotonic_ns() if now is None else now
            reasons = []
            if self.retired or self.control_state not in ('play','resume') or self.last is None:
                reasons.append(self.reason)
            elif self.last['query_host_ns'] is None or not 0 <= now-self.last['query_host_ns'] <= FRESH_NS:
                reasons.append('snapshot_freshness')
            elif not self.last['valid']:
                reasons.extend(self.last['reasons'])
            return dict(kind='runtime_clock_snapshot', launch_id=self.launch_id,
                        generation=self.generation, evaluated_ns=now,
                        valid=not reasons, reasons=reasons,
                        query_host_ns=self.last['query_host_ns'] if self.last else None,
                        seconds=self.last['estimate']['seconds'] if self.last and self.last['estimate'] else None)
