# Task 3 report: controlled creative validation and cache

## RED

Added `tests/test_ad_media.py` and ran `python3 -m pytest tests/test_ad_media.py -q`. Collection failed with `ModuleNotFoundError: No module named 'ad_media'`, establishing the missing implementation.

## GREEN

Implemented `inspect_ts` with strict single-program PAT/PMT, PES timestamp, continuity, and AAC-LC ADTS frame inspection. It measures whole 1024-sample frames and normalizes PTS wrap. Unsupported or ambiguous streams, malformed packets, and discontinuities fail preparation.

Implemented a session-owned `CreativeCache` with same-origin HTTP opens carrying the same absolute ten-second deadline. It accepts only complete, simple HLS media playlists, checks each EXTINF against measured frames within one AAC frame, and limits 128 segments, 60 seconds, and 32 MiB. Preparation publishes opaque resource IDs only after every segment validates and writes atomically. The shared byte budget and two preparation slots are bounded; unpinned entries can be evicted, pinned entries are retained. Cancellation and failed preparation remove unpublished files. Session close deletes cached media. Startup cleanup stays inside the owned cache namespace and does not follow a symlink cache root.

## Verification

- Focused: `python3 -m pytest tests/test_ad_media.py -q` — 17 passed.
- Full: `python3 -m pytest -q` — 440 passed in 68.16 seconds.
- Independent FFprobe inspection: `replacement000.ts` and `replacement001.ts` each report AAC, 48 kHz, two channels, and 94 read frames. The parser reports 96,256 samples each and continuous adjacent PTS.
- `git diff --check` passed.

## Files

- `plugin.audio.kodi_donor_radio/resources/lib/ad_media.py`
- `tests/test_ad_media.py`
- `.superpowers/sdd/2026-10-22-vast-controlled-ads/task-3-report.md`

## Self-review and concerns

The inspector intentionally accepts only the controlled single-program AAC-LC TS shape. It does not support encrypted, variant, discontinuous, or alternate-rendition HLS. Cache entries must be pinned by the publishing layer while output references them; unpinned entries may be evicted to honor the session byte cap. Runtime code uses only Python standard-library parsing and does not invoke FFmpeg.
