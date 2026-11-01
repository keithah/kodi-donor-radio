from pathlib import Path
import sys
import unittest
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest.importorskip('numpy', reason='NumPy-enabled analysis runtime required')
from analyze_hls_capture import endpoint_anchors, heldout_clock_indices, validate_capture_integrity


def test_capture_integrity_checks_remain_explicit():
    validate_capture_integrity({'status': 'completed', 'total_frames': 12}, 12)
    with pytest.raises(ValueError, match='capture_not_completed'):
        validate_capture_integrity({'status': 'failed', 'total_frames': 12}, 12)
    for frames in (11, 13):
        with pytest.raises(ValueError, match='capture_frame_count_mismatch'):
            validate_capture_integrity({'status': 'completed', 'total_frames': 12}, frames)
    with pytest.raises(ValueError, match='capture_frame_count_mismatch'):
        validate_capture_integrity({'status': 'completed', 'total_frames': True}, 1)


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

    def test_empty_clock_ledger_has_no_heldout_rows(self):
        self.assertEqual(heldout_clock_indices([], 0, 48000), [])


if __name__ == '__main__':
    unittest.main()
