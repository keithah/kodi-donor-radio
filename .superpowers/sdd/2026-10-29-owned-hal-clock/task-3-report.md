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
