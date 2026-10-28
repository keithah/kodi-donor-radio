"""Evidence survives unavailable Kodi and fixture teardown endpoints."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hls_observer', Path(__file__).with_name('observe-hls.py'))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)

class ObservationPersistenceTests(unittest.TestCase):
    def test_teardown_failures_preserve_completed_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory)
            report = {'timeline': [{'playing': False}], 'actions': [{'action': 'pause'}]}
            def failed_rpc(*args, **kwargs):
                saved = json.loads((capture / 'kodi-clock-observation.json').read_text())
                self.assertEqual(saved, report)
                raise RuntimeError('Kodi gone')
            with patch.object(observer, 'rpc', side_effect=failed_rpc), patch.object(observer, 'urlopen', side_effect=OSError('fixture gone')):
                observer.save_observation(capture, report, 19092)
            saved = json.loads((capture / 'kodi-clock-observation.json').read_text())
            self.assertEqual(saved['timeline'], [{'playing': False}])
            self.assertEqual(saved['actions'], [{'action': 'pause'}])
            self.assertEqual(saved['stop_error'], 'RuntimeError')
            self.assertEqual(saved['fixture'], {'error': 'OSError'})

if __name__ == '__main__':
    unittest.main()
