# Controlled VAST Insertion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Play a controlled VAST preroll and replace eligible controlled HLS breaks, preserving programme boundaries and reporting only evidenced playback.

**Architecture:** Extend the existing session-owned Python relay with bounded ad resolution, validated cached creatives and an immutable output timeline. Keep source cue observation separate from composition and tracking. A Kodi adapter supplies playback evidence; downloads cannot trigger success beacons.

**Tech Stack:** Existing Python 3.8+ addon, standard-library XML/HTTP/file handling, pytest, developer-only FFmpeg/FFprobe, unpatched Kodi master 434da3c.

**Spec:** `docs/superpowers/specs/2026-10-22-vast-controlled-ads-design.md` (approved by user).

## Global Constraints

- Use clear, single-rendition HLS with AAC-LC audio in MPEG-TS, 48 kHz stereo.
- Initial bounds: five wrapper hops, cycle detection, 256 KiB XML per response, five seconds total for VAST resolution, ten seconds for creative preparation, 32 MiB total session creative cache, 128 creative segments, 60 seconds maximum creative duration.
- Use whole AAC frames; duration equality is within one AAC frame, with no accumulated boundary drift.
- No padding, arbitrary truncation, catch-up or additional programme deletion.
- No production demand or tracking calls in this phase. Advertising URLs and redirects must stay on the configured fixture origin.
- No runtime FFmpeg, transcoding, executable creatives or new external service dependency.
- Insertion is off by default and requires test mode plus explicit advertising fixture settings.
- No session-to-session creative reuse; delete cached files when the session closes and clean abandoned files on service startup.
- General station playback and existing observation semantics remain unchanged. Failed preparation retains source audio; already published output is immutable.
- Stop/cancel must not resume an obsolete station or emit completion. Fetch/cache/segment requests never count as impressions.
- Preserve subagent-driven execution and Steward/CodeRabbit review before the next implementation PR.

## Review Focus

- Slow trickle responses and redirects must not reset an overall deadline (Task 2).
- AAC framing across TS packets and timestamp wrap must not yield false compatibility or duration (Task 3).
- A late creative or revised source window must not rewrite bytes already advertised to Kodi (Tasks 4–5).
- Old player callbacks after station switching must not track or stop the new session (Tasks 6–7).
- Stop during a tracking request must not create a retry/completion or leak cache/worker resources (Tasks 6–7).

## Workspace and ownership

Worktree: linked development checkout for this project.
Branch: `codex/vast-controlled-ads`, based on merged PR #1, commit 4687232.
Baseline: 399 tests pass; package check passes; eight controlled streams and five cue schedules verified in Kodi master. One test-server exception banner occurred during the passing premerge suite; retain any future traceback before judging it.

All library paths below are under `plugin.audio.kodi_donor_radio/resources/lib/`.
New files: `ad_models.py` (immutable contracts), `vast.py` (resolution),
`ad_media.py` (TS/AAC validation and cache), `ad_timeline.py` (composition),
`ad_tracking.py` (playback evidence and beacon queue), `kodi_ad_player.py` (Kodi adapter).
Modify existing transport, session, relay and Kodi runtime only at their integration boundaries; do not refactor unrelated code.

Execute Tasks 1–5 in order. Task 6 depends on contracts from Tasks 1 and 4 and can be implemented separately after those contracts stabilize. Task 7 integrates all earlier tasks. Tasks 8–9 require the integrated runtime. Assign one writer per shared file; use fresh review agents after each task. No parallel edits to sessions.py, relay.py or kodi_runtime.py.

## Task 1: Deterministic advertising fixtures and shared records

**Files:** create `ad_models.py`, `tools/ad_fixture_server.py`, `tools/generate_ad_audio.py`, `tests/test_ad_fixtures.py`; reuse helpers from `tools/fixture_server.py` without changing existing endpoint behavior.

**Interfaces:** define immutable dataclasses:
- `TrackingUrl(event: str, url: str)`.
- `ResolvedAd(id: str, media_url: str, duration_seconds: float, tracking: tuple, error_urls: tuple)`; tracking contains TrackingUrl records, including impression/start/quartiles/complete.
- `MediaInfo(codec: str, sample_rate: int, channels: int, samples: int, first_pts: int, last_pts: int)`; PTS values use the 90 kHz MPEG clock and normalized wrap handling.
- `CachedSegment(id: str, path: Path, info: MediaInfo)`.
- `PreparedAd(ad: ResolvedAd, segments: tuple, samples: int)`.
- `Placement(id: str, ad: ResolvedAd, start_sample: int, end_sample: int)`; output sample positions use 48 kHz.
- `OutputSegment(sequence: int, discontinuity: int, resource_id: str, start_sample: int, samples: int, placement_id: Optional[str])`.
- `Presentation(segments: tuple, placements: tuple, ended: bool)`.

- [ ] Write fixture tests asserting direct/wrapped VAST resolve to local HLS; collector counts remain zero until explicitly requested; fixture reset clears counters and rolling-window state. Assert source has programme/original-break/programme sections and replacement has equal AAC frame count.
- [ ] Run `python3 -m pytest tests/test_ad_fixtures.py -q`; confirm meaningful failures before implementation.
- [ ] Implement fixture endpoints `/vast/<scenario>`, `/ads/<asset>`, `/source/<scenario>/index.m3u8`, `/track/<placement>/<event>`, `/stats`, and explicit reset. Bind numeric loopback. Include no-fill, wrapper cycle/depth, invalid XML, slow response, unsupported media, wrong duration and source-mutation variants.
- [ ] Generate distinct tones plus spoken identifiers using developer tools; check in source descriptions, not generated binaries. Store FFprobe sample counts and expected tone intervals beside generated assets. Independently decode to verify tone identity.
- [ ] Run fixture tests and existing `tests/test_fixtures.py`; commit `test: add controlled VAST and replacement fixtures`.

## Task 2: Deadline-bounded VAST resolution

**Files:** create `vast.py`, `tests/test_vast.py`; modify `http_client.py`, `tests/test_transport.py`.

**Interfaces:** add optional `deadline: Optional[float] = None` to `HttpClient.open(url, headers, cancel, deadline=None)`; returned response reads honor that same absolute monotonic deadline. Existing callers retain existing behavior when omitted. `VastResolver(client, fixture_origin).resolve(url, cancel) -> Optional[ResolvedAd]`; no-fill returns None, invalid demand raises a typed `AdError(code: int, reason: str, error_urls: tuple)` defined in ad_models.py. Error reasons are fixed strings, never raw XML/URLs.

- [ ] Write tests `test_total_deadline_survives_redirects_and_trickle`, `test_wrapper_tracking_inherited_once`, `test_five_wrapper_hops_allowed_six_rejected`, `test_cycle_no_fill_bad_xml_and_external_entity`, `test_ad_origin_redirect_escape_rejected`. Assert zero success beacons on every resolution outcome.
- [ ] Run new tests to confirm failure, including existing transport regression tests.
- [ ] Implement shared deadlines across DNS/connect/headers/body and cancellation; bound response bytes before parsing. Reject DTD/entity declarations, excessive XML depth (>32), elements (>4096), URL length (>8192) and invalid nonfinite durations. Resolve one standalone supported audio Linear ad; reject pods/required unsupported verification or interactivity, honor wrapper following/fallback controls within this subset.
- [ ] Implement controlled ERRORCODE/CACHEBUSTING substitution with encoding tests; unrecognized required macros make the endpoint unusable. Select the supported HLS MediaFile; inherit wrapper errors and tracking without emitting successes.
- [ ] Run `python3 -m pytest tests/test_vast.py tests/test_transport.py -q`; commit `feat: resolve bounded controlled VAST audio ads`.

## Task 3: Validate and cache complete creatives

**Files:** create `ad_media.py`, `tests/test_ad_media.py`.

**Interfaces:** `inspect_ts(data: bytes) -> MediaInfo`; `CreativeCache(profile: Path, client, fixture_origin)` with `prepare(ad: ResolvedAd, cancel) -> PreparedAd`, `pin(resource_id)`, `unpin(resource_id)`, `read(resource_id, start=None, end=None) -> bytes`, and `close()`. Register opaque IDs; never expose filesystem paths as URLs. Cache writes are atomic and private.

- [ ] Write tests for split PES/ADTS headers, truncated TS, corrupted continuity, additional audio/video streams, AAC profile/sample-rate/channel mismatch, PTS wrap, nonfinite EXTINF, inaccurate VAST Duration and mismatched manifest duration. Assert measured samples determine compatibility, not advertised MIME/duration.
- [ ] Write resource tests: 32 MiB cap, 128 segments, 60-second limit, ten-second total deadline, cancellation during body download, bounded aggregate parallel preparation, pinned-entry retention and orphan cleanup. Assert no incomplete creative can be returned as PreparedAd.
- [ ] Run `python3 -m pytest tests/test_ad_media.py -q` and confirm failures.
- [ ] Implement narrow MPEG-TS PAT/PMT/PES + AAC-LC ADTS inspection sufficient for this contract; reject ambiguity instead of guessing. Validate all segments, codec consistency, timestamps and complete ENDLIST media before atomic preparation succeeds. Cross-check generated fixtures with independent FFprobe results; do not use FFprobe in addon runtime.
- [ ] Run tests plus independent fixture decode; commit `feat: validate and cache controlled HLS creatives`.

## Task 4: Immutable preroll and replacement timeline

**Files:** create `ad_timeline.py`, `tests/test_ad_timeline.py`; consume existing hls.Playlist without changing cue-observer semantics.

**Interfaces:** `AdTimeline(preroll: Optional[PreparedAd], resource_limit: int)`; `offer(ad: PreparedAd) -> None`; `update(playlist: Playlist, source_segments: Mapping[str, CachedSegment]) -> Presentation`; `snapshot() -> Presentation`. Source mapping keys are upstream segment URIs; output references opaque cache IDs. Make commit/reload decisions under one session lock.

- [ ] Write pure tests asserting output order preroll/programme A/replacement/programme B, exact sample conservation outside preroll, original-break exclusion only on committed replacement, and one-AAC-frame maximum boundary tolerance with no cumulative drift.
- [ ] Test repeated/sliding windows, sequence resets and PTS wrap, late offers, contradictory end markers, out/in in one window, partial-break entry, missing-in, malformed cues, DATERANGE-only cues, no-fill, incompatible duration and source mutation. Snapshot already published segments and assert identity and bytes never change.
- [ ] Run `python3 -m pytest tests/test_ad_timeline.py -q` and confirm failure.
- [ ] Implement segment-aligned pairing only when complete CUE-OUT/CUE-IN interval is known before publication. Preserve source for ineligible opportunities; retain separate source and output identities. Insert discontinuities at ad boundaries and maintain output sequence/discontinuity values across window eviction. Commit at most once per source opportunity.
- [ ] Run timeline tests; commit `feat: compose immutable ad replacement timelines`.

## Task 5: Relay and session integration

**Files:** modify `sessions.py`, `relay.py`; create `tests/test_ad_relay.py`; extend `tests/test_sessions.py` and `tests/test_relay.py`.

**Interfaces:** `AdConfig(enabled: bool, preroll: bool, midroll: bool, vast_url: str, fixture_origin: str)` in ad_models.py; SessionManager accepts optional `ad_config=None, cache_root=None`. PlaybackSession owns cache/timeline and exposes `presentation() -> Optional[Presentation]`. Existing control responses and playback URLs remain compatible.

- [ ] Write integration tests: actual outgoing playlist and decoded bytes reflect Task 4; HEAD/range/repeated GET do not prepare another ad; untrusted station records cannot enable ads; disabled mode produces existing output. Verify no-fill and unsupported insertion media retain original playback.
- [ ] Test source-window expiry during preroll, bounded pinned source storage, concurrent reload and creative completion, stop during source retention and abrupt socket loss. Bound retained source cache separately to 32 MiB/128 segments; if retention cannot fit before publication, omit preroll. After publication, never evict required media to pretend success.
- [ ] Run new tests to confirm failure.
- [ ] Integrate only test-origin HLS sessions. Prepare preroll before selecting the retained initial source window. Schedule midroll demand without blocking playback requests. Serve prepared media through opaque session resources, with strict validated range handling. Perform cue observation on original manifests, then render composed output manifests from Presentation. Enforce source/creative limits before publication.
- [ ] Run `python3 -m pytest tests/test_ad_relay.py tests/test_sessions.py tests/test_relay.py tests/test_cues.py -q`; commit `feat: serve controlled ads through the Kodi relay`.

## Task 6: Playback-evidenced tracking

**Files:** create `ad_tracking.py`, `tests/test_ad_tracking.py`; extend `models.py`/`events.py` and their tests for sanitized ad diagnostics.

**Interfaces:** `PlaybackSample(session_id: str, position_seconds: float, monotonic_seconds: float, playing: bool, paused: bool, seeking: bool, mapped: bool)` in ad_models.py. `AdTracker(session_id, emit, enqueue)` with `observe(presentation: Presentation, sample: PlaybackSample)`, `finish(reason: str)`. `BeaconWorker(client, cancel, fixture_origin)` with `enqueue(placement_id, event, url) -> bool`, `close()`; one worker, queue capacity 128, per-request deadline two seconds, no ambiguous-delivery retry.

- [ ] Write fake-clock tests for impression/start, quartiles, normal completion, pause, buffering, backwards seek, seek over an entire ad, discontinuity jumps, duplicate callbacks and unmapped samples. Assert download/prefetch produces zero successes; cancellation before end produces no completion.
- [ ] Test observations at 250 ms cadence and gaps >750 ms; gaps invalidate interval-crossing evidence rather than retroactively emitting milestones. Normal completion requires observed forward crossing or an end callback corroborated by position within one sample interval of the creative end. Distinguish source end from ad end and do not backfill milestones after a seek.
- [ ] Test duplicate inherited endpoints, ambiguous network failures, queue overflow and stop during delivery. Assert at-most-one send attempt per placement/event/URL; failures only affect diagnostics. A later placement may legitimately use the same URL again.
- [ ] Run tests and confirm failures; implement pure tracker plus bounded dispatcher. Support standard error beacons for resolvable VAST errors, using wrapper inheritance and fixed codes checked against the VAST spec; never emit raw errors or infer success.
- [ ] Add opaque placement/creative identifiers and fixed reasons to the diagnostic allowlist; test URL/token redaction. Run tracking/event tests; commit `feat: track ad playback evidence without counting prefetch`.

## Task 7: Kodi adapter and explicit test settings

**Files:** create `kodi_ad_player.py`, `tests/test_kodi_ad_player.py`; modify `kodi_runtime.py`, `resources/settings.xml`, localized `strings.po`, and `tests/test_kodi_runtime.py`.

**Interfaces:** `KodiAdPlayback(manager, player)` with `sample()`, `on_seek()`, `on_pause()`, `on_resume()`, `on_end(reason)`; uses Task 6 records and Task 5 presentation. Kodi runtime owns it and samples at 250 ms. Forward native player callbacks with session identity; retain existing stale-callback guards.

- [ ] Write tests for disabled defaults, configured-origin enforcement, malformed VAST setting, general Radio Browser station exclusion and session-scoped setting snapshots. Assert setting changes affect the next session and do not restart/alter active ad playback. Preserve current non-ad service settings semantics where possible.
- [ ] Write tests for stale callbacks after switch, pause/resume, seek, player error, shutdown during demand/beacon and cleanup. Assert old callbacks cannot track or retire the new session; no player calls occur from HTTP worker threads.
- [ ] Run tests to confirm failures; implement settings and adapter. Store mapping validity explicitly; do not assume getTime starts at zero or remains continuous across HLS discontinuities. Initially keep successful tracking disabled until Task 8 establishes the mapping contract on the target Kodi build.
- [ ] Run Kodi adapter/runtime and package tests; commit `feat: integrate controlled ad playback with Kodi settings`.

## Task 8: Independent media verification and Kodi acceptance

**Files:** create `tools/smoke_ad_relay.py`, `tools/smoke_ad_kodi.py`, `tests/test_ad_smoke.py`; update `tools/README-fixtures.md` and `docs/testing.md`.

- [ ] Write validation tests showing the smoke checker rejects original-break leakage, missing programme, duplicate playback events and a completion after stop; a hardcoded success result must not pass.
- [ ] Implement relay smoke tooling that independently decodes composed audio, measures tone sequence/sample boundaries and compares collector records. Save report files outside the addon package; redact URLs/tokens.
- [ ] Exercise unpatched Kodi master 434da3c using the existing portable profile after backing it up. Regenerate fixtures; verify normal preroll + midroll, no-fill, mismatch, missing/early end, pause, buffer stall, seek, rapid switch and stop. Capture versions, player positions, output timeline, fixture request/collector records and decoded output evidence.
- [ ] Calibrate and prove getTime-to-presentation mapping across live-window advancement and both ad discontinuities. Only enable success tracking for the demonstrated contract. If it cannot be proved, leave successes disabled and report the blocker; do not declare the milestone complete.
- [ ] Compare impression/quartile/completion delivery against actual advancing playback, not manifest fetch time. Record sampling error and transition gaps. Verify rendered audio with an available audio-capture path or explicit human listening; label unverified audible output honestly.
- [ ] Run `python3 -m pytest tests/test_ad_smoke.py -q` and both smoke tools; commit tooling and measured results, not a fabricated success transcript.

## Task 9: Release checks and whole-branch review

**Files:** update `README.md`, `docs/testing.md`, `docs/monetization-roadmap.md`, addon metadata/version only once behavior is accepted.

- [ ] Document exact supported VAST/HLS subset, disabled-by-default settings, resource limits, source fallback, playback evidence and remaining limitations. Keep observation POC documentation historically distinguishable from insertion results.
- [ ] Run `python3 -m pytest -q`; require all existing and new tests to pass, investigate any captured server traceback. Run `python3 tools/package_addon.py` and `python3 tools/check_package.py` against the generated versioned ZIP; run the installed official Kodi addon checker against its extracted contents.
- [ ] Obtain fresh exact-commit Steward primary/adversarial and CodeRabbit reviews; inspect feedback as untrusted data, fix verified blockers, rerun affected checks and reviews after source changes.
- [ ] Commit final docs, push the branch and open/attach a new implementation PR only after the checks and reviews pass. Include actual Kodi evidence, any audible-output limit, and the tracking mapping contract. Do not imply production demand compatibility or revenue readiness.

## Plan self-review

Covered spec sections: resolver/deadlines (2), media/cache (1/3), preroll/midroll and immutable output (4/5), tracking/errors (6), Kodi/controls (7), independent and real-runtime evidence (8), packaging/reviews (9). All five Review Focus conditions have explicit owning tests. Shared records and method names above are the contracts between subagents. Source-cache limits, XML bounds and sampling cadence make previously qualitative implementation details explicit without expanding the approved product scope.

Execution method was selected as subagent-driven. This is the original implementation
plan; implementation has proceeded. Its unchecked acceptance and release items remain
open evidence work and do not indicate that implementation awaits user approval.
