import tempfile
from pathlib import Path
import unittest
import generate_fmp4_fixture as generator

class Fmp4VerificationTests(unittest.TestCase):
    def test_packet_timestamp_must_match_source_sample_position(self):
        generator.validate_packet_timestamp({'pts':1024,'dts':1024,'duration':1024},1)
        for packet in ({'pts':2048,'dts':1024,'duration':1024},
                       {'pts':1024,'dts':0,'duration':1024},
                       {'pts':1024,'dts':1024,'duration':960}):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                generator.validate_packet_timestamp(packet,1)

    def test_trim_metadata_is_not_accepted_as_untrimmed(self):
        for side in ({'side_data_type':'Skip Samples'}, {'skip_samples':1024}, {'discard_padding':1}):
            with self.subTest(side=side), self.assertRaises(ValueError):
                generator.assert_no_trim_side_data([{'side_data_list':[side]}],'fixture')

    def test_existing_fixture_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            saved=root/'audio.caf';saved.write_bytes(b'evidence')
            with self.assertRaises(FileExistsError):generator.empty_or_missing_target(root)
            self.assertEqual(saved.read_bytes(),b'evidence')

if __name__=='__main__':unittest.main()
