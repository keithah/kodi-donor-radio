"""Owned fixture contracts; HTTP exercises use the real bounded VAST client."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]/'plugin.audio.kodi_donor_radio/resources/lib'))
import fixture_beacons as f

class FixtureBeaconTests(unittest.TestCase):
    @unittest.skipUnless(f.DEFAULT_CONTRACT.exists(),'preserved frozen acceptance contract required')
    def test_fixed_intervals_and_contract_reject_edit(self):
        path = Path('/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/acceptance-contract.json')
        contract = f.load_contract(path)
        self.assertEqual(contract['placements_aac_units']['b_paused'], [1536,2304])
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/'contract.json'
            bad.write_bytes(path.read_bytes()+b' ')
            with self.assertRaises(ValueError): f.load_contract(bad)

    def test_ready_requires_exact_binding(self):
        binding = dict(nonce='a'*32,timeline_sha256='b'*64,native_pid=123)
        ready = dict(binding,kind='owned_monitor_ready')
        self.assertTrue(f.validate_ready(ready,binding))
        for key,value in [('nonce','c'*32),('timeline_sha256','c'*64),('native_pid',124),('kind','native_hello')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                f.validate_ready(dict(ready,**{key:value}),binding)

    def test_video_queue_ready_binding(self):
        binding=dict(nonce='a'*32,timeline_sha256='b'*64,native_pid=123,
                     player_mode='videoplayer_video_v1',requested_core='VideoPlayer',video_queue_tenths=40,video_extradata_hex='00')
        ready=dict(binding,kind='owned_monitor_ready')
        self.assertTrue(f.validate_ready(ready,binding))
        for value in (None,39,41,40.0,"40",True,False,[],{}):
            with self.assertRaises(ValueError):f.validate_ready(dict(ready,video_queue_tenths=value),binding)
        for value in (None,'','01','0000',0,False):
            with self.assertRaises(ValueError):f.validate_ready(dict(ready,video_extradata_hex=value),binding)
        no_marker=dict(ready);del no_marker['video_extradata_hex']
        with self.assertRaises(ValueError):f.validate_ready(no_marker,binding)
        del ready['video_queue_tenths']
        with self.assertRaises(ValueError):f.validate_ready(ready,binding)

    def test_ready_publication_is_complete_and_exclusive(self):
        import os
        binding=dict(nonce='a'*32,timeline_sha256='b'*64,native_pid=123)
        with tempfile.TemporaryDirectory() as directory:
            fd=os.open(directory,os.O_RDONLY)
            try:
                f.publish_ready(fd,binding)
                ready=Path(directory)/'monitor-ready.json'
                original=ready.read_bytes();f.validate_ready(json.loads(original),binding)
                self.assertEqual(ready.stat().st_mode&0o777,0o600)
                with self.assertRaises(OSError):f.publish_ready(fd,dict(binding,nonce='c'*32))
                self.assertEqual(ready.read_bytes(),original)
            finally:os.close(fd)

    def test_profile_fail_closed(self):
        good = {'audiooutput.audiodevice':'Default','audiooutput.lowlatency':False,
                'audiooutput.channels':1,'audiooutput.config':2,'audiooutput.processquality':30,
                'audiooutput.stereoupmix':False,'audiooutput.passthrough':False}
        f.validate_profile(good)
        for changed in ({'audiooutput.lowlatency':True},{'audiooutput.audiodevice':'DARWIN:other'},{'audiooutput.lowlatency':0}):
            with self.assertRaises(ValueError): f.validate_profile(dict(good,**changed))

    def test_source_proof_rejects_changed_identity(self):
        with self.assertRaises(ValueError): f.validate_proof({'protocol':1}, {})

    def test_http_vast_real_resolver_and_retained_bad_receipts(self):
        from vast import VastResolver
        from http_client import HttpClient
        from url_policy import UrlPolicy
        nonce='a'*32
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            service=f.BeaconEndpoints(root, {'proof':'test'}, now_ns=lambda:987654321)
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    status,mime,body=service.get(self.path,False)
                    self.send_response(status);self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                def log_message(self,*args):pass
            # Origin is deliberately fixed in owned metadata and resolver policy.
            server=ThreadingHTTPServer(('127.0.0.1',18789),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                resolver=VastResolver(HttpClient(UrlPolicy({f.ORIGIN})),f.ORIGIN)
                for name,(start,end) in f.PLACEMENTS.items():
                    ad=resolver.resolve(f.ORIGIN+'/vast/'+name+'.xml?launch='+nonce,threading.Event())
                    self.assertEqual(ad.id,name)
                    self.assertAlmostEqual(ad.duration_seconds,(end-start)*1024/48000)
                    self.assertEqual({t.event for t in ad.tracking},set(f.EVENTS))
                    self.assertEqual(ad.media_url,f.ORIGIN+'/creative/'+name+'/index.m3u8')
                route=f.ORIGIN+'/track/'+nonce+'/a/start'
                with urlopen(route) as response:self.assertEqual(response.status,204)
                for bad,status in [(route,409),(f.ORIGIN+'/track/'+nonce+'/a/bogus',400),
                                   (f.ORIGIN+'/track/'+'b'*32+'/a/start',400)]:
                    with self.assertRaises(HTTPError) as error:urlopen(bad)
                    self.assertEqual(error.exception.code,status)
                rows=[json.loads(x) for x in (root/'beacon-receipts.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows),4)
                self.assertEqual([x['accepted'] for x in rows],[True,False,False,False])
                self.assertTrue(rows[1]['duplicate'])
                self.assertTrue(all(x['receipt_mach_ns']==987654321 for x in rows))
            finally:
                server.shutdown();server.server_close();thread.join();service.close()

class PreservedFixtureIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(Path('/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture/owned-beacons/proof.json').exists(),'preserved controlled source fixture required')
    def test_actual_server_source_and_creative_resolution_then_proof_mutation(self):
        import shutil,subprocess,time
        from urllib.error import URLError
        from owned_ad_playback import canonical_timeline
        source=Path('/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'fixture';shutil.copytree(source,root)
            repository=HERE.parents[2]
            process=subprocess.Popen([sys.executable,str(HERE/'serve_fixture.py'),'--root',str(root),
                                     '--repo',str(repository),'--owned-beacons'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+10
                while True:
                    try:
                        with urlopen(f.ORIGIN+'/stats',timeout=.5) as response:json.load(response)
                        break
                    except URLError:
                        if process.poll() is not None or time.monotonic()>deadline:self.fail('Owned server did not start')
                        time.sleep(.02)
                presentation,proof=f.resolve_presentation(root,'d'*32)
                self.assertEqual(len(presentation.placements),6)
                self.assertEqual(sum(s.samples for s in presentation.segments),8641536)
                self.assertEqual(presentation,f.presentation_from_manifest(json.loads(canonical_timeline(presentation,f.FIRST_PACKET))))
                with urlopen(f.ORIGIN+'/stats') as response:
                    stats=json.load(response);self.assertFalse(stats['window']['started'])
                    self.assertEqual(stats['beacons']['attempts'],0)
                original=(root/'owned-beacons/proof.json').read_bytes()
                altered=json.loads(original);altered['first_packet_sha256']='0'*64
                (root/'owned-beacons/proof.json').write_text(json.dumps(altered))
                with self.assertRaises(ValueError):f.verify_fixture(root)
                (root/'owned-beacons/proof.json').write_bytes(original)
                asset=root/'assets/segment000.m4s';asset.write_bytes(asset.read_bytes()+b'x')
                with self.assertRaises(ValueError):f.verify_fixture(root)
            finally:
                process.terminate();process.communicate(timeout=5)

if __name__=='__main__': unittest.main()


def test_hal_readiness_discloses_backend_format_and_retention():
    import owned_hal_gain as h
    binding=dict(nonce='a'*32,timeline_sha256='b'*64,native_pid=123,
        output_backend='hal_callback_v1',hal_contract_version=1,hal_phase='controls',device_uid='BuiltInSpeakerDevice',
        device_format=h.HAL_FORMAT,lifecycle_capability='retained_exposed_context_until_process_exit')
    ready=dict(binding,kind='owned_monitor_ready')
    assert f.validate_ready(ready,binding)
    for field in ('output_backend','hal_contract_version','device_uid','device_format','lifecycle_capability'):
        changed=dict(ready);changed.pop(field)
        with __import__('pytest').raises(ValueError):f.validate_ready(changed,binding)


def test_ready_nested_format_integer_types_are_strict():
    import owned_hal_gain as h
    binding=dict(nonce='a'*32,timeline_sha256='b'*64,native_pid=123,output_backend='hal_callback_v1',
        hal_contract_version=1,hal_phase='controls',device_uid='BuiltInSpeakerDevice',device_format=h.HAL_FORMAT,
        lifecycle_capability='retained_exposed_context_until_process_exit')
    ready=dict(binding,kind='owned_monitor_ready',device_format=dict(h.HAL_FORMAT,buffers=True))
    with __import__('pytest').raises(ValueError):f.validate_ready(ready,binding)
