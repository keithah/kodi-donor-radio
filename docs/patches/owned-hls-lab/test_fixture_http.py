"""Exercise actual loopback initialization, range and clock behavior."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FragmentedFixtureHttpTests(unittest.TestCase):
    def test_init_and_ranges_do_not_start_media_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / 'assets'
            assets.mkdir()
            (assets / 'init.mp4').write_bytes(b'initialization')
            (assets / 'segment000.m4s').write_bytes(b'fragment-data')
            metadata = {'sample_rate':48000, 'first_pts':0, 'pts_timebase':[1,48000],
                        'metadata_identity':'test', 'init_segment':'init.mp4',
                        'segments':[{'sequence':0, 'filename':'segment000.m4s',
                                     'start_sample':0, 'end_sample':96000, 'samples':96000}]}
            (root / 'metadata.json').write_text(json.dumps(metadata))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('serve_fixture.py')),
                                        '--root', str(root), '--port', str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            base = 'http://127.0.0.1:%d' % port
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with urlopen(base+'/stats', timeout=.5) as response:
                            self.assertFalse(json.load(response)['window']['started'])
                        break
                    except URLError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('Fixture server failed to start')
                        time.sleep(.02)
                with urlopen(Request(base+'/live/init.mp4', method='HEAD'), timeout=1) as response:
                    self.assertEqual(response.headers['Content-Length'], '14')
                with urlopen(Request(base+'/live/init.mp4', headers={'Range':'bytes=0-3'}), timeout=1) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b'init')
                    self.assertEqual(response.headers['Content-Range'], 'bytes 0-3/14')
                with urlopen(base+'/stats', timeout=1) as response:
                    self.assertFalse(json.load(response)['window']['started'])
                with urlopen(base+'/live/index.m3u8', timeout=1) as response:
                    self.assertIn(b'#EXT-X-MAP:URI="init.mp4"', response.read())
                with urlopen(Request(base+'/live/segment000.m4s', headers={'Range':'bytes=4-7'}), timeout=1) as response:
                    self.assertEqual(response.read(), b'ment')
                    self.assertEqual(response.headers['Content-Type'], 'audio/mp4')
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/live/init.mp4', headers={'Range':'bytes=99-'}), timeout=1)
                self.assertEqual(error.exception.code,416)
                with urlopen(base+'/stats', timeout=1) as response:
                    self.assertTrue(json.load(response)['window']['started'])
            finally:
                process.terminate()
                process.communicate(timeout=5)

if __name__ == '__main__':
    unittest.main()
