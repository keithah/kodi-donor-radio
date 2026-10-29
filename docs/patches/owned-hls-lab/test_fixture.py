import unittest
from pathlib import Path
try:
    from serve_fixture import DEFAULT_REPO, RollingTimeline, render_playlist
except ImportError:
    DEFAULT_REPO = RollingTimeline = render_playlist = None

class RollingFixtureTests(unittest.TestCase):
    def test_default_repository_is_derived_from_fixture_location(self):
        self.assertEqual(DEFAULT_REPO, Path(__file__).resolve().parents[3])

    def make(self):
        self.assertIsNotNone(RollingTimeline, 'rolling fixture is not implemented')
        ends = [96256,191488,287744,384000,479232,575488,671744,766976]
        segs=[]; start=0
        for i,end in enumerate(ends):
            segs.append({'sequence':i,'filename':'segment%03d.ts'%i,'start_sample':start,'end_sample':end,'samples':end-start})
            start=end
        return RollingTimeline({'sample_rate':48000,'segments':segs})
    def test_fragmented_mp4_keeps_init_map_when_window_retires(self):
        original=self.make()
        metadata={'sample_rate':48000,'segments':[dict(segment,filename=segment['filename'].replace('.ts','.m4s')) for segment in original.segments],'init_segment':'init.mp4'}
        timeline=RollingTimeline(metadata)
        timeline.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        playlist=render_playlist(timeline.snapshot(111_989_333_334,0))
        self.assertIn('#EXT-X-VERSION:7\n',playlist)
        self.assertEqual(playlist.count('#EXT-X-MAP:URI="init.mp4"'),1)
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:1\n',playlist)
        self.assertIn('segment001.m4s',playlist)
        self.assertNotIn('segment000.m4s',playlist)

    def test_fixture_rejects_unsafe_or_inconsistent_init_assets(self):
        original=self.make()
        for name in ['../init.mp4','https://example.com/init.mp4','bad".mp4','init.ts']:
            with self.subTest(name=name),self.assertRaises(ValueError):
                RollingTimeline({'sample_rate':48000,'segments':original.segments,'init_segment':name})
        with self.assertRaises(ValueError):
            RollingTimeline({'sample_rate':48000,'segments':original.segments,'init_segment':'init.mp4'})

    def test_head_does_not_start_clock_and_first_get_publishes_only_zero(self):
        t=self.make(); h=t.snapshot(100_000_000_000,1_800_000_000_000_000_000,activate=False)
        self.assertFalse(h['started']); self.assertIsNone(t.anchor)
        s=t.snapshot(110_000_000_000,1_800_000_010_000_000_000,activate=True)
        self.assertEqual([x['sequence'] for x in s['segments']],[0]);self.assertEqual(t.anchor['first_get_host_ns'],110_000_000_000)
    def test_publication_uses_actual_sample_end_not_rounded_two_seconds(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        self.assertEqual(t.snapshot(101_983_999_999,0)['published_count'],1)
        self.assertEqual(t.snapshot(101_984_000_000,0)['published_count'],2)
    def test_window_grows_then_retires_old_sequence(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        s=t.snapshot(109_984_000_000,0);self.assertEqual([x['sequence'] for x in s['segments']],[0,1,2,3,4,5])
        s=t.snapshot(111_989_333_334,0);self.assertEqual([x['sequence'] for x in s['segments']],[1,2,3,4,5,6]);self.assertFalse(s['endlist'])
    def test_endlist_only_after_final_segment_published(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        self.assertFalse(t.snapshot(113_973_333_333,0)['endlist'])
        s=t.snapshot(113_973_333_334,0);self.assertTrue(s['endlist']);self.assertEqual(s['first_sequence'],2)
        self.assertEqual(t.snapshot(900_000_000_000,0)['first_sequence'],2)
    def test_pdt_is_fixed_to_source_sample_not_current_poll_time(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        a=t.snapshot(102_000_000_000,0);b=t.snapshot(105_000_000_000,1_900_000_000_000_000_000)
        self.assertEqual(a['segments'][1]['program_date_time_ns'],1_800_000_000_000_000_000)
        self.assertEqual(a['segments'][1]['program_date_time_ns'],b['segments'][1]['program_date_time_ns'])
    def test_playlist_declares_sequence_pdt_actual_durations_and_endlist(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        body=render_playlist(t.snapshot(111_989_333_334,0))
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:1\n',body);self.assertIn('#EXTINF:1.984000000,\nsegment001.ts',body)
        self.assertEqual(body.count('#EXT-X-PROGRAM-DATE-TIME:'),6);self.assertNotIn('#EXT-X-ENDLIST',body)
        self.assertIn('#EXT-X-ENDLIST',render_playlist(t.snapshot(113_973_333_334,0)))
    def test_stats_snapshot_and_old_asset_metadata_remain_available(self):
        t=self.make();t.snapshot(100_000_000_000,1_800_000_000_000_000_000,True)
        t.snapshot(111_989_333_334,0)
        self.assertEqual(t.segments[0]['filename'],'segment000.ts');self.assertEqual(t.anchor['first_get_host_ns'],100_000_000_000)

if __name__=='__main__':unittest.main()
