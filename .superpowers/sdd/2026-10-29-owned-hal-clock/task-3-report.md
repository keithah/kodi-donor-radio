# Task 3 implementation report

Implemented the distinct `hal_callback_v1` clock phase and its staged native authorization boundary. The AE provider and frozen v2 estimator remain unchanged. The phase remains loopback-fixture only with authority, beacons, and production eligibility false.

Native additions are `OwnedHalClock.h` and `OwnedHalAuthorization.h`, appended to the existing additive native patch. Historical positions derive only from acquired callback segments at the native query midpoint. Startup source delivery waits for nine zero-output readiness observations and preserves the strict S0/frame/deadline checks. Tracker, queue, and dispatch each require their own exact-next native authorization proof.

The maximum-populated benchmark exposed that the prior owner-loop gain scan could starve authorization when 6,000 retained gain records existed. `PollGainNext` now reads only the exact next immutable gain record. The old failed attempts remain under `OUT/hal-task3-1793300802259852000/benchmark-01` through `benchmark-10`.

Focused verification:

- 429 Python tests passed with the real native authorization harness, including staged tracker/queue/dispatch, pause, route, ABA and changed-intent rejection cases.
- 33 lab phase/manifest tests passed, with two predeclared localhost server tests deselected because the controller-owned fixture occupies port 18789.
- 48 numerical beacon-evaluator tests passed with the bundled NumPy runtime.
- Native clock and authorization boundary harnesses passed in debug and `-DNDEBUG -O3` builds.
- `benchmark-12/result.json` passed with 32,763 retained authorization requests, 6,000 gain records, 64 controls, five authorized final requests, a 9.333166 ms maximum request-to-action time, a 5.083 microsecond maximum native bracket, and one observed exact-next gain renewal.

The repository native patch is reproduced from the preserved Task 3 source snapshot. It is ready for the controller's required arm64 Release Xcode build, independent review, installation, and captured-audio acceptance. No Kodi core, recorder, device, job, or capture action was performed by this implementation task.

## Review-loop addendum

The activation gate now requires a private, owner-supplied native candidate manifest. It binds the freeze's exact `OwnedHalClock.h`, `OwnedHalAuthorization.h`, and `OwnedClockTransport.h` digests to the actual installed dylib and addon manifest hashes; absent, writable, symlinked, mismatched, or swapped artifacts fail closed. The final evaluator now audits each tracker, queue, and dispatch record against its native request/reply evidence and parent proof chain. Its segment coverage lookup uses ordered sequence neighbors rather than an O(N*M) scan.

All attempts remain under `OUT/hal-task3-1793300802259852000/benchmark-01` through `benchmark-15`: 01--10 are preserved setup/protocol failures; 11--12 are the earlier serial candidates; 13 found the new staged driver's missing response-file parent hash; 14 proved that correction but intentionally failed the fifth chain's freshness bound under the old 5ms fake-owner poll. Neither failure was retried unchanged.

Changed candidate `benchmark-15/result.json` passed the required maximum-population staged contention run: 32,753 retained requests plus five tracker/queue/dispatch chains (32,768 total), 6,000 gain records, 64 controls, and five concurrent workers. All 15 final native replies were authorized. Measured maxima were 3.531584 ms request-start-to-action, 22.333 microseconds native bracket, and 920.125 microseconds response-receipt-to-action; each is below its respective 20ms, 5ms, and 5ms frozen bound. The exact-next gain renewal was observed.

The regenerated native patch digest is `f9bb4339907f42b7bb602393fc52550c41dc425a9d2ed642e762a1415671ffe3`. It applies cleanly to a fresh 22.2.10 HEAD snapshot at `/private/tmp/hal-patch-reproduction.rmq4am`; the resulting source contains the final startup-readiness declarations, two-argument `WriteHalSegment` declaration, and three-argument/default-bounded `GainJson` declaration. The independently preserved Release dylib is not overwritten. This records source/harness reproduction only and makes no new Release-build claim.

## Follow-up accounting correction

`benchmark-16` replaces benchmark-15 as the timing-accounting candidate after a driver-only correction. It records the actual Mach receipt after reading immutable reply bytes, validates the complete echoed request/reply schema, then records the action timestamp. Its 15 final rows include per-stage tracker/queue/dispatch latency, query-to-action age, native-reply-to-receipt, receipt-to-action, and slot-wait time under two monitor and three worker threads. All frozen bounds passed: request-to-action 4.659458ms (20ms), native bracket 23.292us (5ms), reply-to-action 1.192792ms (5ms), and query-to-action 46.135709ms (100ms). The maximum observed slot wait was 19.259541ms; all 15 stages authorized while control/gain cadence ran and the exact-next gain renewal remained observed. Benchmark-15 remains retained as the prior, less complete accounting artifact and is not used for the final timing claim.

## Pre-lock deadline correction: gate failed

The production authorization transport starts its 20ms deadline before acquiring its exact-next I/O lock. Benchmark-16 incorrectly placed its synthetic request start after its equivalent slot acquisition; its raw tracker 32760 was actually 21.741416ms from slot attempt to action, so benchmark-16 is retained but invalid as a pass. The driver now anchors request start and deadline before the slot, rejects an over-budget acquisition before publishing, and has a regression for that delayed-lock path. It also reports immutable read receipt and validated action time separately.

Changed candidate `benchmark-17` ran the same two-monitor/three-worker pressure with the fake owner poll reduced to 50 microseconds. It failed rather than relaxing the gate: tracker request 32760 reached its native check at 20.941917ms from pre-lock request start, beyond the 20ms frozen budget; three competing workers then correctly hit local slot-timeout rejection. The source remains fail-closed and benchmark-17 is preserved. This is a current architecture/release blocker: the single exact-next transport lock can queue enough simultaneous tracker/queue/dispatch work to exhaust a 20ms request lifetime. A future fix must reduce actual per-request owner service cost or change the product scheduling architecture while retaining the pre-lock deadline; this task does not claim that serializing the benchmark or redefining the start point resolves it.

Separately, the native ledger reader now treats non-string `kind` fields safely without invoking `startswith` on arbitrary JSON values; regression coverage verifies `None`, booleans, integers, and objects do not crash or retire a valid session.

## Fair exact-next admission: benchmark-18 passed

The authorization transport now uses a bounded fair FIFO slot rather than opportunistic lock acquisition. The native request start/deadline remains sampled before the caller joins that slot, so all queue wait remains inside the frozen 20ms native action budget. Tests cover FIFO order, timed-waiter removal, cancellation removal, close while cancellation-draining waiters exist, and the existing active-holder close path. No waits are taken under provider/tracker/gain/worker locks.

The benchmark imports this production slot implementation directly. Changed candidate `benchmark-18` is retained with driver SHA-256 `32a6cf6a711980c8769fd356f9615e618c6a6f8440ae7e5ced2a1db45b163d58`, authorization-provider SHA-256 `29284c8490f3fb4c068d7c60bd28e25b242031ced5030a9dc8d097909b253cb1`, and harness SHA-256 `ba9ac4165393341e14cdc075e9bdd82cff2c03004ae1cc89b0a31a229b926d48`. Under two monitor and three worker roles, 32,753 retained requests plus five tracker/queue/dispatch chains filled the 32,768 request limit; all 15 final stages authorized. Maximum pre-slot request-to-action was 19.476875ms (20ms bound), native bracket 24.917 microseconds (5ms), reply-to-action 1.018ms (5ms), query-to-action 64.911584ms (100ms), and slot wait 16.446792ms. Control/gain cadence continued and exact-next gain renewal was observed. Benchmark-17 remains preserved as the prior failing non-FIFO candidate.

## Pre-publication timeout handling: benchmark-19 passed

The transport previously retired itself if a fair-slot acquisition timed out
before any request file was published. That left native exact-next sequencing
untouched but prevented later authorization attempts. The provider now drops
only that unpublished stage; failures after request publication remain
terminal. A regression forces one slot-acquisition timeout, verifies zero
published requests/count and a live transport, then completes the following
probe successfully.

The updated provider passed benchmark-19 using the same maximum-population
32,768-request run, two monitor and three worker roles, 6,000 gain records and
64 controls. All 15 tracker/queue/dispatch stages authorized. The maximum
pre-slot request-to-action time was 16.076458ms (20ms bound), native bracket
43.417us (5ms), reply-to-action 1.013041ms (5ms), and query-to-action
47.324833ms (100ms). Control/gain cadence and exact-next gain renewal were
observed. Result: `/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/hal-task3-1793300802259852000/benchmark-19/result.json`.

Benchmark-19 used provider SHA-256
`9fc49887d811cdbb0bb1956e54889cf941ce337207612380011c5a42f981cf6a`, driver
SHA-256 `fbd0c0ff2bf1bca92d1a566d1378f4335d04176990f0ed791933f131bfe68116`, and
harness SHA-256 `d2ab00e058ad502d183a82413a77ca3f865f6ae590fed26b7aeff51e6f9726d0`.
The HAL freeze now records the current Python provider bindings and benchmark
provenance; authority, beacon and production flags remain false.
