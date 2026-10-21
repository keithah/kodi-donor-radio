# Implementation decisions

Recorded during subagent-driven implementation on 2026-10-21.

- Use an app-managed isolated worktree for the authorized implementation — keeps main unchanged — cost if wrong: user must inspect a separate checkout.

- Task 7 may create docs/testing.md before Task 8 expands it — its verification requires recording runtime selection — cost if wrong: documentation rearrangement only.

- An explicit OUT boundary in the first manifest can emit a signaled break_start, without claiming audible playback; CONT or proven ongoing ranges emit in-progress observations — first fetch alone cannot establish listener join position — cost if wrong: downstream consumers need a classification/timing adjustment.

- Include narrow ISO8601 basic-offset compatibility fix in Task 8 — live supported HLS uses +0000 which Python 3.9 fromisoformat rejects — cost if wrong: parser accepts a timestamp variant too broadly; cover strict invalid inputs and review the diff.

- Permit an initial bytes=0- request on non-seekable continuous streams and rewritten manifests by serving full 200 response — actual FFmpeg playback is blocked by the planned 416 behavior — cost if wrong: seek semantics require revisiting; nonzero byte ranges remain explicitly constrained.

- Add allowlisted media extensions to opaque HLS resource routes — real FFmpeg rejects extensionless segment URLs — cost if wrong: extensionless upstreams may need explicit support; do not expose upstream path or guess arbitrary types.

- Defer investigation of the isolated test-server banner — intentional disconnect fixtures plausibly explain it and all tests/decodes passed; exact cause remains unconfirmed — cost if wrong: an asynchronous issue needs follow-up.

## Remaining validation

Actual Kodi playback is pending because Kodi is not installed. Unit/integration tests, real FFmpeg relay decoding, bounded live discovery checks and package/checker results are recorded in [testing.md](testing.md). The checker environment emits a urllib3/LibreSSL compatibility warning; the addon checker itself reports no addon problems or warnings.
