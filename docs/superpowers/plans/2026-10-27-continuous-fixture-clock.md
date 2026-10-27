# Continuous controlled-audio clock investigation

The user requested removal of the startup interruption and stable absolute
position across extended live windows, with captured-audio testing and PR #4.
Production insertion/tracking remains out of scope.

## Evidence and selected experiment

Adaptive's elapsed time subtracts its moving first-retained-segment offset.
VideoPlayer's realtime startup deliberately sets its clock behind queued audio;
the measured initial correction is approximately 655 ms. A paced ADTS prototype
through PAPlayer removes the startup interruption but still freezes during a
held-open source stall. A buffered inputstream must be tested with explicit
VideoPlayer selection before deciding whether a native change is necessary.

The next addon-side candidate retains the existing strict HLS composition and
extracts already-validated AAC frames into one continuous ADTS response. Output
sample zero is the first emitted frame, independent of HLS window retirement.
No transcoding, inferred wall-clock position, or download-based success beacon
is introduced. Initially keep live tracking disabled until the exact installed
player path passes captured tests. Native patches, if needed, stay separate.

## Bounded implementation

1. Extend developer fixtures beyond the 64-output retention limit; keep the
   original stalled-window fixture. Generate and independently validate tones,
   PTS and sample counts.
2. Reuse the strict TS/PES parser for immutable ADTS extraction, with identical
   malformed-media rejection and exact frame/sample accounting.
3. Offer a single-consumer AAC representation only for opted-in controlled
   compositions. Serialize source reloads with the root playlist lock. Copy one
   validated output segment at a time, require consecutive output sequence and
   sample positions, and close rather than skip when media has retired or changed.
   HEAD must not consume; cancellation and socket failures release the session.
4. Select only a supported, explicitly configured player path after validating
   the same-session URL. Preserve native/Adaptive fallback where applicable.
5. Establish a transport-specific sample-zero clock only from that path, never
   from segment requests. Reject seeks/resets, unsupported player paths and
   positions beyond delivered media. Do not infer missing playback time.
6. Capture startup, extended live retirement, pause/resume, seek and starvation.
   Compare raw/mapped clocks with independent tone boundaries and capture timing.
   Keep limitations explicit. Run full tests, package checks, Steward and
   CodeRabbit before pushing the completed change to PR #4.

This is the agent's proposed implementation choice under the user's instruction
to complete these fixes; it is not a claim that the candidate has passed acceptance.
