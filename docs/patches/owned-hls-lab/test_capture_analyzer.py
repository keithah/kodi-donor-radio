from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_hls_capture import endpoint_anchors


class EndpointAnchorTests(unittest.TestCase):
    def test_endpoint_anchors_skip_short_unanchored_spans(self):
        segments = [
            {'channels': [{'anchors': []}]},
            {'channels': [{'anchors': [{'id': 'first'}, {'id': 'inside'}]}]},
            {'channels': [{'anchors': []}]},
            {'channels': [{'anchors': [{'id': 'last'}]}]},
        ]

        first, last = endpoint_anchors(segments, 0)

        self.assertEqual(first, {'id': 'first'})
        self.assertEqual(last, {'id': 'last'})

    def test_endpoint_anchors_reports_no_usable_span(self):
        self.assertEqual(endpoint_anchors([], 0), (None, None))
        self.assertEqual(endpoint_anchors([{'channels': [{'anchors': []}]}], 0), (None, None))


if __name__ == '__main__':
    unittest.main()
