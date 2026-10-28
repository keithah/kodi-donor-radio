import os
import queue
import threading
import ast
import types
import json
import tempfile
import unittest
from pathlib import Path
from runtime_clock import RuntimeClockSession, LedgerReader, LedgerError, URL, frozen_module

BASE=1_000_000_000

def row(i):
    t=BASE+i*20_000_000
    return dict(kind='clock_sample', steady_ns=t, mach_absolute_ns=t,
                delay_query_start_steady_ns=t, delay_query_end_steady_ns=t,
                accepted_adddata_frames=48000+i*960, audioengine_delay_s=.2,
                source_epoch=0, control_state='play', playback_started=True,
                failed=False, drained=False)

class DeterministicSession(RuntimeClockSession):
    def ingest(self, row, received_ns=None, identity=None):
        self.now_ns = lambda:received_ns
        return super().ingest(row,received_ns,identity)

class RuntimeTests(unittest.TestCase):
    def session(self):
        s=DeterministicSession(URL, BASE-1)
        s.control('play', BASE-1)
        return s
    def test_equivalence_acquisition_and_snapshot(self):
        s=self.session(); e=frozen_module().Estimator()
        for i in range(12):
            r=row(i); out=s.ingest(r,BASE+i*20_000_000)
            self.assertEqual(out['estimate'],e.update(r))
        self.assertTrue(s.snapshot(BASE+220_000_000)['valid'])
        self.assertFalse(s.snapshot(BASE+320_000_001)['valid'])
    def test_pause_resume(self):
        s=self.session()
        for i in range(10): s.ingest(row(i),BASE+i*20_000_000)
        s.control('pause', BASE+200_000_000)
        self.assertFalse(s.snapshot(BASE+200_000_000)['valid'])
        s.control('resume',BASE+210_000_000)
        self.assertFalse(s.ingest(row(10),BASE+220_000_000)['valid'])
        for i in range(11,20):
            out=s.ingest(row(i),BASE+i*20_000_000)
            self.assertEqual(out['valid'],i==19)
    def test_invalid_stale_future_and_retirement(self):
        for field,value in [('audioengine_delay_s',float('nan')),('source_epoch',True),('failed',0)]:
            s=self.session(); r=row(0); r[field]=value
            self.assertIn('invalid_schema',s.ingest(r,BASE)['reasons'])
        for now in (BASE-1,BASE+100_000_001):
            self.assertFalse(self.session().ingest(row(0),now)['valid'])
        for mutate in ('epoch','rewind','stop','route'):
            s=self.session(); s.ingest(row(0),BASE)
            r=row(1)
            if mutate=='epoch': r['source_epoch']=1
            if mutate=='rewind': r['accepted_adddata_frames']=0
            if mutate=='stop': s.control('stop',BASE+1)
            if mutate=='route': s.route('other',BASE+1)
            s.ingest(r,BASE+20_000_000)
            self.assertTrue(s.retired)
    def test_resume_boundary_and_invalid_query(self):
        s=self.session()
        s.control('resume',BASE)
        self.assertIn('before_control_boundary',s.ingest(row(0),BASE)['reasons'])
        for start,end in [(BASE+1,BASE),(BASE,BASE+6_000_000)]:
            r=row(0); r['delay_query_start_steady_ns']=start
            r['delay_query_end_steady_ns']=end
            self.assertFalse(self.session().ingest(r,BASE+10_000_000)['valid'])
    def test_batch_stale_rows_cannot_prime_fresh_acquisition(self):
        s=self.session()
        for i in range(10):
            self.assertFalse(s.ingest(row(i),BASE+1_000_000_000)['valid'])
        for i in range(50,59):
            out=s.ingest(row(i),BASE+i*20_000_000)
            self.assertEqual(out['valid'],i==58)
    def test_reader_batch_bound(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'ledger'; r=LedgerReader(p,max_batch=8)
            p.write_text('x'*9)
            with self.assertRaises(LedgerError): r.read()
            r.close()
    def test_terminal_missing_and_huge_schema(self):
        s=self.session(); r=row(0); r['drained']=True
        s.ingest(r,BASE); self.assertTrue(s.retired)
        r=row(0); del r['failed']
        self.assertIn('invalid_schema',self.session().ingest(r,BASE)['reasons'])
        r=row(0); r['mach_absolute_ns']=10**1000
        self.assertIn('invalid_schema',self.session().ingest(r,BASE)['reasons'])
    def test_duplicate_out_of_order(self):
        s=self.session()
        s.ingest(row(1),BASE+20_000_000)
        self.assertFalse(s.ingest(row(1),BASE+20_000_000)['valid'])
        self.assertFalse(s.ingest(row(0),BASE+20_000_000)['valid'])
    def test_monitor_callbacks_preserve_native_controls(self):
        tree=ast.parse(Path(__file__).with_name('monitor.py').read_text())
        klass=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        calls=[]
        session=types.SimpleNamespace(retire=lambda value:calls.append(('retire',value)),
                                      route=lambda value:None)
        scope=dict(xbmc=types.SimpleNamespace(Player=object),session=session,
                   state=lambda value:calls.append(('control',value)))
        exec(compile(ast.Module(body=[klass],type_ignores=[]),'monitor_callbacks','exec'),scope)
        player=scope['Player']()
        player.onPlayBackPaused(); player.onPlayBackSpeedChanged(0)
        self.assertEqual(calls,[('control','pause')])
        player.onPlayBackResumed(); player.onPlayBackSpeedChanged(2)
        player.onPlayBackSeek(1,2); player.onPlayBackError()
        self.assertEqual(calls[1:], [('control','resume'),('retire','non_unit_speed'),
                                    ('retire','seek'),('retire','playback_error')])
    def test_evaluation_freshness_crossing(self):
        ticks=iter([BASE+99_000_000,BASE+101_000_000])
        s=RuntimeClockSession(URL,BASE-1,now_ns=lambda:next(ticks))
        s.control('play',BASE-1)
        r=row(0)
        out=s.ingest(r)
        self.assertIn('evaluation_freshness',out['reasons'])
        self.assertEqual(out['received_ns'],BASE+99_000_000)
        self.assertEqual(out['evaluated_ns'],BASE+101_000_000)
    def test_queued_result_revoked_before_publication(self):
        s=self.session()
        for i in range(10): out=s.ingest(row(i),BASE+i*20_000_000)
        self.assertTrue(out['valid'])
        s.control('pause',BASE+190_000_000)
        published=s.for_publication(out,BASE+190_000_000)
        self.assertFalse(published['valid'])
        self.assertTrue(published['evaluated_valid'])
        self.assertIn('superseded_generation',published['reasons'])
    def test_blocked_output_does_not_delay_pause_control(self):
        tree=ast.parse(Path(__file__).with_name('monitor.py').read_text())
        funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('state','enqueue','flush_pending')]
        s=self.session()
        for i in range(10): s.ingest(row(i),BASE+i*20_000_000)
        entered=threading.Event(); release=threading.Event(); paused=threading.Event()
        def blocked_emit(row):
            entered.set(); release.wait(2)
        with tempfile.TemporaryDirectory() as d:
            pending=queue.Queue(maxsize=100)
            pending.put(s.snapshot(BASE+180_000_000))
            scope=dict(session=s,ROOT=Path(d),time=types.SimpleNamespace(),
                       clock=types.SimpleNamespace(mach_host_ns=lambda:BASE+190_000_000),
                       control_lock=threading.Lock(),pending=pending,queue=queue,
                       json=json,os=os,emit=blocked_emit)
            exec(compile(ast.Module(body=funcs,type_ignores=[]),'monitor_functions','exec'),scope)
            writer=threading.Thread(target=scope['flush_pending']); writer.start()
            self.assertTrue(entered.wait(1))
            def pause():
                scope['state']('pause'); paused.set()
            callback=threading.Thread(target=pause); callback.start()
            try:
                self.assertTrue(paused.wait(.5))
                self.assertEqual((Path(d)/'control').read_text(),'pause')
                self.assertFalse(s.snapshot(BASE+190_000_000)['valid'])
            finally:
                release.set(); writer.join(2); callback.join(2)
    def test_retirement_keeps_first_reason_and_generation(self):
        s=self.session()
        s.retire('ledger_failure')
        generation=s.generation
        for reason in ('ledger_failure','route_mismatch','monitor_exit'):
            s.retire(reason)
        for state in ('pause','resume','play','stop'):
            s.control(state,BASE)
        self.assertTrue(s.retired)
        self.assertEqual(s.reason,'ledger_failure')
        self.assertEqual(s.generation,generation)
        self.assertFalse(s.snapshot(BASE)['valid'])
        s.ingest(row(0),BASE)
        self.assertEqual(s.reason,'ledger_failure')
        self.assertEqual(s.generation,generation)
    def test_terminal_callback_retirement_is_idempotent(self):
        s=self.session()
        s.control('stop',BASE)
        generation=s.generation
        s.control('resume',BASE+1)
        s.retire('monitor_exit')
        self.assertEqual(s.reason,'callback_stop')
        self.assertEqual(s.generation,generation)
        self.assertTrue(s.retired)
    def test_reader(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'ledger'; reader=LedgerReader(p)
            self.assertEqual(reader.read(),[])
            p.write_bytes(b'{"kind":"event"}')
            self.assertEqual(reader.read(),[])
            with p.open('ab') as f: f.write(b'\n')
            self.assertEqual(reader.read()[0]['row'],{'kind':'event'})
            p.write_bytes(b'')
            with self.assertRaises(LedgerError): reader.read()
            reader.close()
    def test_reader_preexisting_replaced_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'ledger'; p.write_text('x\n')
            r=LedgerReader(p)
            with self.assertRaises(LedgerError): r.read()
            r.close()
            p.unlink(); r=LedgerReader(p,max_line=8,max_batch=16)
            p.write_text('x'*9)
            with self.assertRaises(LedgerError): r.read()
            r.close()
            p.unlink(); r=LedgerReader(p); p.touch(); r.read()
            q=Path(d)/'q'; q.touch(); q.replace(p)
            with self.assertRaises(LedgerError): r.read()
            r.close()

if __name__=='__main__': unittest.main()
