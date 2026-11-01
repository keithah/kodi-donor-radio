# Qualified fixture beacons and resume investigation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement these bounded tasks with failing tests before behavior changes.

**Goal:** Remove the observed resume interruption where supported and enable local success beacons only on a captured, explicitly selected continuous playback path.

**Architecture:** The existing authenticated service binds a short-lived, single-use launch capability to one controlled fixture. A launcher explicitly requests Kodi's built-in VideoPlayer; the resolver can qualify only its exact continuous AAC representation with the tested inputstream. The adapter waits for owned AV startup and stable progress, brackets clock reads with session/URL checks, and suppresses ambiguous placements. Ordinary stations, fallback transports, direct clients and unqualified launches remain diagnostic.

**Tech stack:** Kodi Python API/JSON-RPC, existing loopback relay, strict AAC composition, Python tests, Core Audio process capture. No production VAST tag or production beacon activation.

- [x] Investigate resume using unchanged source, native output logs and captured PCM; compare stock and the optional realtime-policy patch. Do not equate shorter silence with complete sample preservation. Low latency was already enabled.
- [x] In control/session/relay/runtime launch code, implement bounded fixture capability issuance, consumption and exact transport qualification. Test expiry, replay, wrong fixture, fallback and failed launch.
- [x] In the playback adapter and RelayPlayer callbacks, activate only after first owned AV start; double-check URL and session identity around clock reads and reject slow/raced samples. Keep zero-start preroll suppression. Taint a placement interrupted by pause and require fresh progress after resume.
- [x] Add controlled late-break and recovery fixtures as needed to test timing beyond retained windows, without loosening production composition bounds.
- [x] Capture explicit launches: early/late clean breaks, pause during a break, seek, starvation and recovery. Compare audible markers, clock observations and ordered/deduplicated local beacons. Keep unsupported cases suppressed.
- [ ] Run appropriate focused/full checks, package verification, independent review, Steward and CodeRabbit. Commit/push and update PR4 with measured results and unresolved limits.

The proposed Kodi-core pause-boundary work is deferred: the user requested further supported-path investigation. Fresh PAPlayer and older Adaptive comparisons did not preserve all audio. No seamless-resume fix is claimed. Existing USB work and the approved audio capture application remain untouched.
