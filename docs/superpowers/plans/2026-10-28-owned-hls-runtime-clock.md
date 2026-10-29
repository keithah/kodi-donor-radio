# Owned HLS runtime clock implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Continue the user's approved clock work without a new approval loop.

**Goal:** Evaluate the frozen clock during actual controlled playback, with a per-launch owner and explicit revocation, then compare the emitted results with captured audio.

**Architecture:** The lab monitor owns one clock session and a bounded reader of the native telemetry file created after launch. It feeds every complete native clock observation to the unchanged V2 estimator. This is a diagnostic integration, not a production Session or beacon integration: the existing native ledger lacks authenticated incarnation and control acknowledgment.

**Tech stack:** Python 3.8+, existing Kodi callbacks, unchanged native addon and Kodi core, frozen signed recorder.

**Spec:** Existing approved `2026-10-27-owned-hls-clock.md` requirements, extended by this bounded runtime design.

## Global constraints

- Normal Kodi pause; no source seeks, flushes, replay or Kodi core changes.
- Frozen V2 estimator and constants remain byte-identical.
- Exact controlled loopback URL only. Production success beacons remain disabled.
- Keep all recordings and reports, including failures. Never rebuild/resign recorder.
- Clear Steward and CodeRabbit before pushing PR #4.

## Review focus

- Old, replaced, truncated or oversized telemetry cannot resurrect eligibility.
- Pause callback revokes immediately; buffered pre-resume observations cannot qualify resume.
- Invalid/missing/NaN fields and stale/future query timestamps fail closed.
- Epoch changes, counter rewinds, stop and route changes permanently retire the launch.
- Processing a batch must preserve all estimator observations while publishing only fresh results.

### Task 1: Runtime session and monitor bridge

Files: create `docs/patches/owned-hls-lab/runtime_clock.py` and `test_runtime_clock.py`; modify `monitor.py` and lab README only.

Interfaces: RuntimeClockSession owns a frozen Estimator, a random launch ID, launch Mach host timestamp and exact selected URL. A bounded native-ledger reader records file device/inode on first fresh open, rejects preexisting nonempty files, replacement/truncation, and excessive line/batch size. Monitor writes a separate bounded `runtime-clock.jsonl` containing input row identity, query timestamp, receive/evaluation timestamps, frozen estimate and wrapper validity/reasons. No production mapper or tracker changes.

- [x] Write failing tests for acquisition, frozen finite-row equivalence, pause immediate revocation, resume nine-row reacquisition, stale/future observations (100 ms freshness), invalid schema, epoch change/counter rewind, stop/route mismatch, partial lines, replacement/truncation and reader bounds.
- [x] Implement strict schema around immutable dynamically loaded V2 module with verified SHA256 `7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87`. Reject nonfinite/bool numeric fields before estimator. Read native query, launch, callback, receive and evaluation timestamps in the explicit Mach absolute nanosecond domain using `mach_absolute_time` and `mach_timebase_info`; Python monotonic origins vary by interpreter and must not be assumed equal; reject future or more than 100 ms old midpoint. Never extrapolate a snapshot.
- [x] Process all complete clock rows; callbacks invalidate synchronously under a lock. Resume clears acquisition and requires row query timestamps after callback, plus play/resume native state. Terminal changes retire session. Sample loop at 20 ms, bounded IO, ledger writer separate from original telemetry. Preserve original control-file callback behavior and callback ledger.
- [x] Tests pass and independent task review clears. Commit changes; do not push.

### Task 2: Captured runtime validation and final review

- [x] Reinstall unchanged native binary and diagnostic monitor into isolated test profile, preserving originals. Capture raw AAC/fMP4 with four normal pauses, including 20 seconds beyond rolling window, same buffered setting.
- [x] Archive native/runtime ledgers, callbacks, fixture requests and capture. Restore profile/files and quit test Kodi.
- [x] Compare contemporaneously emitted estimator points against independent source waveform, report invalid intervals and freshness/processing age. Frozen offline equivalence is a regression check, not a substitute for runtime result. Require existing 21.333 ms bound on qualified points and no detected gaps/replay in capture. Preserve failure and fix before claiming pass.
- [ ] Run applicable tests, Steward exact-head primary/adversarial review and CodeRabbit. Resolve findings, then push to PR #4 and update evidence/remaining production boundary.

## Captured progress and unresolved acceptance

The first runtime capture qualified all 140 independently matched points
(-8.481 to +0.547 ms) and preserved source continuity, but its test installation
omitted terminal_eof from addon.xml listitemprops. Kodi therefore remained active
until driver cleanup. That run is partial evidence, not natural-EOF acceptance.
The launcher now validates the actual installed manifest before playback.

The corrected repeat reached natural EOF and qualified all 129 matched runtime
points (-7.648 to +1.560 ms), with no missing/invalid matched point. It also
captured a real unrequested 512-frame (10.667 ms) output silence near 65.435 s,
accompanied by a Darwin sink-underflow warning. The source remained queued and
was delayed rather than skipped, but this fails the user's no-gap requirement.
Do not describe a passing clock gate as complete audio acceptance or push on
that basis. Preserve both runs. A subsequent terminal-schema fix explicitly
retires on native drained rows; it has unit coverage but no new capture yet.

Other jobs heavily load the Mac; this is a possible contributor, not proven
causality. Any experiment pausing unrelated jobs requires user authorization
and must restore them afterward. No core changes or recorder rebuilds are needed.

## Reduced-poll experiment: failed audio acceptance

The user declined pausing unrelated jobs; none were changed. The observer now
checks pause deadlines before action-status calls and supports 1 Hz routine
observations while retaining 100 ms action scheduling. This removed needless
pre-deadline requests (previously up to 40 RPC connections per second).

One attempt ended on the recorder's identity check at 148.843 seconds while the
same Kodi process remained alive. Its partial recording is preserved. The full
repeat completed natural EOF and explicitly retired the runtime clock on drain,
but captured six unrequested quiet intervals, including a roughly 100 ms gap.
Reduced polling therefore does not resolve the no-gap requirement. Of 154
independent audio points, 152 had valid runtime estimates within threshold; two
were invalidated by pause callback state. All are reported and the all-points
gate remains failed. Do not push or enable beacons on this result.

## Addon stall investigation

Continue under the user's explicit addon-only constraint; do not suspend or
reprioritize other jobs. Independent waveform reconstruction of the six previous
gaps found inserted sink-block silence while retaining source sequence. The
longer cluster also drained the owned AudioEngine cache despite four seconds of
decoded PCM, so a sink warning alone does not isolate the starving stage.

Measure the existing native addon without changing its playback behavior. Apply
`inputstream.ffmpegdirect-22.2.10-owned-hls-stall-diagnostics.patch` after the owned
HLS patch. It brackets control-file access, telemetry IO, shared-mutex waits and
decode holds, AddData/GetSpace, sleep overshoot and host-packet progress in Mach
time. Fixed storage and try-lock loss counters keep the instrumentation bounded;
extra JSON is appended to existing rows. Record requested feeder QoS without
changing it. Preserve original core and recorder hashes.

Correlate completed operation brackets against independently captured gaps and
queue depths before choosing a fix. Durations include descheduling; a long syscall
bracket is not proof of disk or lock causality. Missing diagnostics never prove
absence of a stall. Retest any actual fix with the same normal pause schedule,
source reference, natural EOF and frozen runtime clock; retain every failure.

## Controlled scheduling candidate: repeated capture passed

The diagnostic baseline localized a 56.081 ms feeder iteration almost entirely
to sleep overshoot, with one captured 512-frame silence block. Shared-lock and
file-write measurements did not explain that interval. The populated AE cache
and simultaneous host delivery delay leave downstream scheduling as an important
limit on causal attribution.

The addon-only current-thread USER_INITIATED QoS candidate then completed two
consecutive unchanged recordings with no extra quiet intervals, preserved source
seams and natural EOF. All 251 independently qualified recorded runtime-clock
points passed. The repeated controlled gate is now met; prior failures and all
production/physical-output limits remain documented. See the stall-investigation
report. Final Steward and CodeRabbit review follow the evidence commit before
any push; production mapping and success beacons are not enabled by this result.
