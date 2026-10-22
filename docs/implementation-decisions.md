# Implementation decisions

Recorded during subagent-driven implementation on 2026-10-21.

- Use an app-managed isolated worktree for the authorized implementation — keeps main unchanged — cost if wrong: user must inspect a separate checkout.

- Task 7 may create docs/testing.md before Task 8 expands it — its verification requires recording runtime selection — cost if wrong: documentation rearrangement only.

- An explicit OUT boundary in the first manifest can emit a signaled break_start, without claiming audible playback; CONT or proven ongoing ranges emit in-progress observations — first fetch alone cannot establish listener join position — cost if wrong: downstream consumers need a classification/timing adjustment.

- Include narrow ISO8601 basic-offset compatibility fix in Task 8 — live supported HLS uses +0000 which Python 3.9 fromisoformat rejects — cost if wrong: parser accepts a timestamp variant too broadly; cover strict invalid inputs and review the diff.

- Permit an initial bytes=0- request on non-seekable continuous streams and rewritten manifests by serving full 200 response — actual FFmpeg playback is blocked by the planned 416 behavior — cost if wrong: seek semantics require revisiting; nonzero byte ranges remain explicitly constrained.

- Add allowlisted media extensions to opaque HLS resource routes — real FFmpeg rejects extensionless segment URLs — cost if wrong: extensionless upstreams may need explicit support; do not expose upstream path or guess arbitrary types.

- Defer investigation of the isolated test-server banner — intentional disconnect fixtures plausibly explain it and all tests/decodes passed; exact cause remains unconfirmed — cost if wrong: an asynchronous issue needs follow-up.

## Cue discontinuity policy refinement

RFC 8216 [§4.3.2.7](https://www.rfc-editor.org/rfc/rfc8216.html#section-4.3.2.7)
defines DATERANGE identity within the playlist; §4.3.2.7.1 pairs SCTE35 OUT/IN
using that ID. DATERANGE records therefore use rendition/epoch/ID identity and
are not retired merely because segment discontinuity changes.

Generic CUE tags have no corresponding RFC identity guarantee. Preserve their
single active association across the next consecutive discontinuity only when
a valid CONT or IN is the first generic cue at its first known segment boundary
(or cue-only boundary). An explicit different ID, malformed cue, skipped
boundary, uncued transition or later-segment cue cannot bridge. A new OUT is a
new break even if it repeats an ID. Epoch changes, supersession and timeline
gaps still retire records. Stale cues cannot modify newer records.

This refines the initial blanket discontinuity retirement because real supported
OUT/CONT/IN placements and the repository media fixture span discontinuities.
It preserves signaled identity, not a new inferred start or audible boundary.
Crossing clears the inferred expiry origin; only explicit continuation elapsed
time with a known media position can establish it again. DATERANGE media timing
remains absent. Real Kodi playback remains unverified.

## Remaining validation

Actual Kodi playback is pending because Kodi is not installed. Unit/integration tests, real FFmpeg relay decoding, bounded live discovery checks and package/checker results are recorded in [testing.md](testing.md). The checker environment emits a urllib3/LibreSSL compatibility warning; the addon checker itself reports no addon problems or warnings.
