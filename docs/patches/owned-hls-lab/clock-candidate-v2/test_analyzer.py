import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import analyze_hls_capture as analyzer


class AnalyzerInputTests(unittest.TestCase):
    def test_invalid_delay_is_unqualified_without_arithmetic(self):
        for delay in [None,float('nan'),float('inf'),-float('inf'),'missing']:
            with self.subTest(delay=delay):
                row={'accepted_adddata_frames':48000}
                if delay!='missing':
                    row['audioengine_delay_s']=delay
                reasons=[]
                self.assertIsNone(analyzer.clock_candidate(row,reasons))
                self.assertEqual(reasons,['invalid_clock_delay'])

    def test_finite_formula_is_unchanged(self):
        reasons=[]
        self.assertEqual(analyzer.clock_candidate({'accepted_adddata_frames':123456,'audioengine_delay_s':.456},reasons),123456/48000-.456)
        self.assertEqual(reasons,[])

    def test_json_is_strict_for_nested_raw_evidence(self):
        encoded=analyzer.strict_json({'last_clock':{'audioengine_delay_s':float('nan')}})
        self.assertEqual(json.loads(encoded),{'last_clock':{'audioengine_delay_s':None}})
        self.assertNotIn('NaN',encoded)


if __name__=='__main__':
    unittest.main()
