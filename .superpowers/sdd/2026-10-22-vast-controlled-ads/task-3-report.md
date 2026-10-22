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

The inspector intentionally accepts only the controlled single-program AAC-LC TS shape. It does not support encrypted, variant, discontinuous, or alternate-rendition HLS. The publishing layer must retain and eventually release the pin transferred by `prepare()` for every output reference; unpinned entries may be evicted to honor the session byte cap. Runtime code uses only Python standard-library parsing and does not invoke FFmpeg.

## Review follow-up (round 1)

The review found a handoff race and missing admission accounting. A successful `prepare()` now registers all of its resource IDs with one pin each inside the same cache lock that publishes them. The caller owns that pin and must call `unpin()` once per segment after its presentation lifetime; additional `pin()` calls remain reference counted. This is the contract for the publishing layer in Task 5. The caller must also release these pins if it decides not to publish the prepared ad.

Creative bytes are reserved under the same lock before entering the staging buffer or being written to private files. Admission counts published bytes plus all active preparations' reservations, evicting only unpinned published entries. Successful publication transfers the reservation to published bytes without double counting; cancellation and every failure path release it.

The new RED run had three failures: the handoff test observed unexpected eviction, the concurrent admission test let both downloads reach inspection, and the real HTTP deadline test found no configurable preparation-duration constant. The GREEN focused run passed 21 tests. A local slow HTTP server now verifies that the absolute shared deadline aborts a response without publication, and a slow validation test verifies parsing stays in that budget. The final full suite passed 444 tests in 68.00 seconds.
