# Controlled owned clock and success beacons implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Continue the explicitly requested implementation; no new approval loop is needed.

**Goal:** Enable actual success beacons from a session-bound native clock on the captured controlled HLS route.

**Architecture:** Native nonce/incarnation/control receipts bind clock rows to a private launch. A Python provider validates ownership before the frozen estimator, then a fixture adapter passes fresh mapped observations to the existing tracker. An immutable precomposed fixture supplies real ad intervals and loopback endpoints; captures independently validate clock and beacon timing.

**Tech stack:** Existing Python 3.8+ addon, C++14 optional FFmpegDirect addon patch, macOS CoreAudio observation, unchanged Kodi core and signed recorder.

**Spec:** docs/superpowers/specs/2026-10-28-owned-clock-beacons.md

## Global Constraints

- Addon-only; no Kodi core changes. Normal Kodi Pause/Resume, no added gaps or replay, natural EOF.
- Leave unrelated jobs running and unchanged; never rebuild/resign the recorder.
- Frozen estimator SHA256 7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87 and constants remain unchanged.
- Exact controlled loopback route and loopback beacons only; unsupported production paths remain disabled.
- Keep all recordings, reports and failures. Clear Steward and CodeRabbit before pushing PR #4.

## Review Focus

- A stale same-URL native instance or queued command must never acquire a new session.
- Pause/resume faster than a native poll must preserve both control revisions; no acquisition using pre-ack rows.
- Changed output/source/terminal state must revoke even when numeric fields are invalid.
- Live manifest retirement cannot erase an ad ahead of the audible cursor.
- Tracker and network receipts cannot manufacture zero-start, uncertain-tail or EOF milestones.

### Task 1: Native owned-clock authority and device-change guard

**Files:** Create docs/patches/inputstream.ffmpegdirect-22.2.10-owned-clock-session.patch, docs/patches/owned-hls-lab/test_owned_clock_protocol.cpp; update lab README with protocol/build recipe. Applied native source is /private/tmp/kodi-audio-validation-20261027/inputstream.ffmpegdirect-22.2.10; modify there to build the additive patch, preserving a pre-task snapshot. Parent handles installs/captures. New patch applies after existing owned-HLS + stall + QoS patches; includes native helper header(s), StreamManager.cpp/h, addon.xml.in properties and CoreAudio link if required. No core source edits.

**Interfaces:** Protocol v1 on exact route. Registered properties owned_clock_nonce (32 lowercase hex), owned_clock_timeline (64 lowercase hex), owned_clock_first_packet (64 lowercase hex). Session root fixed at /private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer/clock-sessions/<nonce>, created by Python mode 0700. Validate owner/type/no symlink components; native hello and ledger files created exclusively mode 0600 using pinned directory. Retained diagnostic ledger is native.jsonl (same existing clock/source fields plus identity). hello.json is bounded immutable identity record. Native incarnation is fresh independent 32-hex randomness for each Open. Native source verifies first raw AAC payload SHA256, first PTS zero, 48 kHz stereo and exact route before source_zero_confirmed=true. Timeline digest is identity binding, not a claim it alone proves media contents.

Commands are immutable sequential files command-000001.txt etc, maximum 64 per launch, bounded 512 bytes, atomically published. Wire line: `v1 <nonce> <incarnation> <revision> <state> <issued_mach_ns>\n`. States waiting/play/pause/resume/stop; first command waiting binds the hello and uses the Python launch timestamp (before later callback issue times). Apply only exact next revision; identical repeated reads are idempotent, conflicts/gaps/malformed input retire clock eligibility. Drain at most 8 pending commands each renderer iteration, preserving transitions; callback thread never waits for ack. Retire only eligibility for ownership errors, preserve audio behavior as far as valid controls permit. No new native thread. Existing normal feed/prefill/source/EOF algorithm and QoS unchanged.

hello.json fields: protocol=1, nonce, incarnation, timeline_sha256, first_packet_sha256, native_pid, native_open_mach_ns, stream_generation=1, qualification_scope=controlled_fixture. Each clock row adds an `ownership` object: protocol, nonce, incarnation, timeline_sha256, observation_sequence (strictly increasing), control_revision, control_state, control_issued_mach_ns, control_applied_mach_ns, state_generation_before, state_generation_after, stream_generation, source_zero_confirmed, eligible, reason, output_guard_generation, output_guard_valid. Legacy timing/estimator fields unchanged. Generation changes before terminal/lifecycle work; sampling records before/after generations and never advertises eligible across a change. Control ack means feeder gate applied, not audible pause. Active seek/flush/abort/close/abnormal speed/error/drain revoke eligibility before blocking joins. Preserve the existing allowed initial pre-play Seek(0) and normal speed 1000 initialization; speed 0 invalidates until normal speed and an acknowledged resume, never qualifies a pause tail. Old lab mode without properties remains available but cannot get owned eligibility.

Native CoreAudio listeners observe default output/device list and pinned device alive/rate/stream configuration changes, installation race checked by reread/generation. Listener callbacks only atomically revoke. Sink format/rate/resample changes or failed checks revoke. No physical speaker or same-format internal Kodi restart guarantee. Add framework link through CMake only if needed. Qualification is live-reader-only; monitor restart gets a new nonce, with no reconnect/replay support, so no heartbeat is required for this fixture scope.

- [ ] Write failing standalone C++14 tests for strict record parsing, sequence/conflict/bounds, state-generation and irreversible revocation, including NDEBUG-active assertions.
- [ ] Implement helper and native integration; add exact first-packet/source-zero and output observation fields without altering frozen clock inputs.
- [ ] Verify additive patch application and standalone tests. Build native addon via XcodeBuildMCP with -jobs 2; do not install or run Kodi. Record binary/source hashes and compile results.
- [ ] Self-review, commit only repo patch/test/docs; report interface details, test evidence and any remaining limits to task report.

### Task 2: Session-owned Python mapping and real tracker adapter

**Files:** Create plugin.audio.kodi_donor_radio/resources/lib/owned_clock.py and owned_ad_playback.py, tests/test_owned_clock.py and tests/test_owned_ad_playback.py; reuse frozen module through injected Estimator factory so no copy/retune. Modify existing AdTracker/BeaconWorker only for a narrowly necessary guarded-enqueue seam if tests prove needed; preserve all legacy behavior. Existing lab runtime diagnostic remains a compatibility consumer.

**Interfaces:** OwnedClockBinding is immutable, holding nonce/incarnation, timeline SHA, exact URL, first packet SHA, launch Mach time, rate=48000 and source origin=0. OwnedClockProvider(binding, estimator_factory, now_ns) ingests every complete native row with exact row hash/sequence; validates hello/protocol/owner/sequence/control ack/coherent generations/source/output proof BEFORE any estimator update. Callback control(state, issued_ns) immediately clears eligibility and advances desired revision, returning the next command; a bounded command writer publishes it without blocking on native ack. Commands before hello queue until bind. Provider snapshot() returns immutable evidence with valid flag/reasons, seconds only when valid, query time, row sequence/hash, native and Python generation. No prior valid seconds on invalid evidence. Revalidate under provider lock immediately before tracker decisions, and preserve 100 ms/5 ms/nine-row bounds. Duplicate/stale/future rows cannot prime acquisition; ownership mismatch, epoch/counter rewind and terminal permanently retire. Pause invalidates and resume reacquires only rows whose query starts after matching native ack.

OwnedAdPlayback(binding, provider, presentation, client, cancel, emit) holds bounded immutable Presentation (<=128 segments, <=8 placements, total 8641536 samples), verifies segment/sample origin and identity, uses actual AdTracker/BeaconWorker, and observes at native query Mach seconds. No getTime fallback. Pause calls tracker.interrupt(); later clean ads may qualify after reacquisition, interrupted placement cannot backfill. Stop/error/route mismatch closes worker and retires. Only one fresh row consumed once; current generation rechecked before each authorization. Emit safe structured decision evidence with input row identity, generation, query/authorization time, mapped position, placement/event identity; no public capability/URL leakage. Qualified first sample <=.250 s may start owned-only preroll; legacy suppression unchanged.

- [ ] Add red tests for wrong/stale hello/owner/timeline/route/incarnation, sequence/command gaps, rapid pause/resume, poisoned stale rows, replacement/truncation/bounds, malformed terminal, rewind/epoch, output invalidation and stale publication.
- [ ] Implement session transport/provider using bounded O_NOFOLLOW regular-file reads and existing Mach domain abstraction; no playback/network work under callback lock.
- [ ] Add real tracker integration tests proving six events for clean ad, no paused-ad backfill, clean ad after pause/rollover, no EOF complete, bounded presentation, onset suppression when late, generation race no enqueue and no general production eligibility.
- [ ] Run focused and affected existing tests; commit and report.

### Task 3: Owned fixture launch, VAST endpoints and capture evaluator

**Files:** Modify docs/patches/owned-hls-lab/{default.py,monitor.py,serve_fixture.py}; create fixture_beacons.py and tests plus evaluate_owned_beacons.py/tests. Existing diagnostic mode remains explicit and unchanged by default; new opt-in owned-beacons mode uses Task 1 properties and Task 2 provider/adapter. Parent performs installation and captures.

**Interfaces:** Fixture timeline manifest hashes source metadata/packet/asset identities and fixed placements from spec. Build actual ResolvedAd/Placement/OutputSegment records from synthetic VAST and immutable metadata, creative AAC payload ranges identical to their precomposed source intervals. Existing source audio/PTS is unchanged. Expose /vast/<creative>.xml and bounded /track/<launch>/<creative>/<event> endpoints with actual receipt Mach timestamps and deduplicated/countable event ledger; /stats reports these separately from source requests. Never accept tracking from arbitrary origins. Capture logs retain all event attempts and no credentials.

Launcher creates unique private session dir, validates native manifest properties, known default device/buffered settings, immutable fixture timeline and first packet hash, then starts monitor with explicit binding and ready nonce. Monitor creates provider, ingests all 20 ms clock rows, drives callbacks/commands, and routes only qualified samples to tracker; route/source/output/session mismatch closes eligibility. Same-URL restart cannot rebind. Use exact Task 1 protocol; native readiness requires hello, not existence of old global monitor-ready. Trace every input/decision and callbacks with bounded logs; network beacon IO separate. No claim all player startup intervals are mapped.

Evaluator joins native row hashes to real contemporaneous decisions and actual server receipts, then independently matched capture positions; it must retain every emitted event and reject missing/repeated/unexpected ones. Timing bound: 100 ms maximum age + 20 ms poll + 21.333 ms clock tolerance for decision delay. Report network delay separately, all early emissions, uncertain/paused placement outcomes. No clock fit or event interpolation across pause uncertainty.

- [ ] Add failing fixture/launch/evaluator tests: real VAST endpoints and source identity, duplicate/unknown events, wrong ready nonce, wrong manifest/properties, malformed proof, missing/duplicate/early receipts and late-acquisition preroll.
- [ ] Implement bounded fixture/controller/monitor and evaluation path using real existing tracker/client; preserve old diagnostic path and fixture behavior without opt-in.
- [ ] Run focused integration/regression and packaging checks, document reproducible invocation, commit and report.

### Task 4: Captured acceptance and final reviews

**Files:** New docs/superpowers/reports/kodi-owned-clock-beacons-2026-10-28.json; update README and this plan with actual results.

- [ ] Parent installs final candidate in isolated Kodi profile with recorded backups and hashes, keeps signed recorder/core unchanged, and records two fresh 230 s captures with fixed pause schedule and other jobs running.
- [ ] Independent audit verifies dense full-source stereo continuity, prefix/tail/pause seams, no extra gaps/replay, native natural EOF, all qualified clock points within 21.333 ms, actual per-event native→decision→HTTP chain and fixed placement expectations. Failures remain failures and are retained; fix and recapture before activation claim.
- [ ] Restore original files/settings and quit only test Kodi. Run affected tests and patch reproduction. Whole-branch review plus exact-head Steward primary/adversarial and CodeRabbit; resolve findings then push to PR #4 and update its evidence and remaining scope.
