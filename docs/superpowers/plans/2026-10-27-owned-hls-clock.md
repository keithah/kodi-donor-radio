# Controlled HLS and captured-clock continuation

Continue the user's approved addon-only investigation: normal Kodi Pause,
no missing/replayed programme audio, no Kodi-core changes, and no PR push
until Steward and CodeRabbit clear. Retain A04 as the measured AAC baseline;
do not reuse the rejected 100 ms prefeed cap.

1. Build a uniquely identifiable 180-second AAC HLS fixture with two-second
   MPEG-TS segments, a six-segment sliding window, explicit programme-date-time
   and a finite ENDLIST. Keep the decoded served-media reference and request
   timeline. Include a pause longer than the visible playlist window.
2. Extend only the opt-in native inputstream lab route to that exact loopback
   HLS URL. Decode raw AAC access units using codec parameters as well as ADTS;
   validate frame shape and fail closed for unsupported inputs. Preserve the
   source FIFO and normal Kodi AVStarted/Pause/Resume callbacks. Record bounded
   host-time-bracketed accepted-frame, decoded-frame, source-PTS, delay and
   cache telemetry throughout playback, pause and drain.
3. Capture the unchanged Kodi build with the frozen signed recorder. Compare
   reference source order, prefix, resume seams, tail, Stop and natural EOF.
   Fit source positions from captured audio independently of the clock under
   test. Compare candidate clock estimates on held-out intervals, including
   window advances and pause/resume. Report errors and confidence states;
   require a predeclared one-AAC-frame (21.333 ms at 48 kHz) maximum error in
   qualified intervals rather than selecting a tolerance after seeing results.
   Startup, pause transitions, starvation, epoch changes and drain must be
   explicitly qualified or invalidated. No success-beacon enablement based on
   a packet count, RPC success, or a recording having completed.
4. Preserve reproducible patches and evidence, run relevant regression checks,
   independently review the native path and clock proof, then complete both
   Steward and CodeRabbit against the final committed state. Resolve actual
   findings, including live EOF behavior, without blind reader resets that can
   skip or replay source. Push to existing PR #4 only after both gates clear.

Review focus: raw AAC versus ADTS framing; probe/Seek(0) startup ordering;
source identity and PTS-to-frame mapping; a pause beyond the HLS window;
clock overstatement while queued audio drains; EOF retries versus true end;
control-session lifetime and initialization rejection. General codecs,
multiple sessions and arbitrary-station release qualification are not inferred
from this controlled-fixture result.

## First HLS capture: rejected

The 230-second process-tap capture completed with zero timestamp discontinuities.
The source ledger delivered 8,439 ADTS packets / 8,641,536 frames (180.032 s),
including two ring wraparounds and natural drain. That is not an audible pass:
waveform analysis found 425–485 ms early uncommanded silences, and smaller
later gaps. All source packets were ADTS; raw AAC remains unexercised.

The proposed owned-output prebuffer was rejected before implementation because
the owned queue and AudioEngine cache remained nonempty during the gaps. Kodi's
read-only ActiveAE source explains the mechanism: RunStages waits for every
active stream to have output before mixing. HLS segment fetches line up with
the ends of the gaps in the captured waveform. The silent host stream is
starving while the independent programme stream remains queued.

Next controlled change: separate host-facing synthetic silence cadence from
blocking HLS packet ingestion in the native addon. Preserve the real source
packet timeline and a bounded PCM queue, arm ingestion only after pre-play
Seek(0), and make stop/EOF ownership and cancellation explicit. This changes
no Kodi core source. Re-capture before claiming continuity or clock accuracy.

The private FFmpeg HLS EOF recovery patch separately passed packet/PCM identity
checks after exhausted holds; expired-window skipping remains an unchanged
upstream behavior and is not qualified by those append-only recovery tests.

A read-only PAPlayer trace also explains the first Pause callback arriving late:
SetSpeed updates cached speed, then SoftStop waits for the stream lock before
emitting the callback. The read/decode path can hold that lock while waiting
for media. PAPlayer does not forward ordinary pause as DemuxSetSpeed(0), so
that inputstream callback cannot substitute for the normal pause signal.
Separating host cadence from HLS input addresses the observed wait without
changing Kodi or introducing a separate user control.

The transport trace refined the implementation: Timeshift already has a
network producer. Its consumer returns a temporary empty packet about every
10 ms; PAPlayer ReadPCM loops over those packets under its stream lock until
an audio packet arrives. The first candidate therefore replaces temporary
empty packets with valid silent AAC and maintains a separate monotonic host
PTS cursor for every host packet. Actual source packet PTS remain unchanged
in the source ledger. This avoids introducing a second reader thread and its
ownership/cancellation races. Capture determines whether this smaller change
is sufficient; the broader worker proposal is not the selected implementation.

## Minimal host-silence candidate: rejected

Candidate a5c61c447a59938c7051d5d98da3b5922dccf11c2e44b091d8a3148dc1c4cb9f
removed early cyclic silences and the delayed first Pause callback, but stalled
after 1,313 source access units (28.010667 s). Its host cursor also stopped,
at 1,421 access units: 2.304 s ahead of source. A no-pause debug repeat stalled
after 469 source units, with its host also stopped 117 units ahead. Meanwhile,
the Timeshift network writer continued buffering source segments. This rejects
a simple source timestamp alias or a fixed segment-boundary explanation.

The full source trace explains a circular wait: an empty owned programme stream
blocks ActiveAE mixing; the silent host's PCM queue fills; PAPlayer then stops
calling ReadPCM/DemuxRead, which is also the only route refilling owned PCM.
The selected next change must ingest/decode source independently of host reads,
using a bounded producer and explicit pre-play/stop/EOF ownership. The minimal
candidate is retained as negative evidence, not release acceptance.

First-capture final clock analysis also fails the predeclared gate: one of 114
independently matched held-out points is -26.331 ms, beyond 21.333 ms. No fitted
offset or post-hoc exclusion was used. Success beacons remain disabled.

Ledger `source_ended` belongs to the owned decoder, not the upstream Timeshift
producer. A false value alone cannot rule out producer EOF; the frozen host
counter and the independently traced queue/read dependency establish the
consumer stall. The later independent-reader test must observe actual EOF
and completed capture, not infer it from this field in isolation.

## Independent reader: clock progress, audio still rejected

Candidate 9f5e3e98336c5ca15193f5885aa7c841a703391b0211697fe08ba03601f5b73f
uses an exclusive owned source worker after the first anchored AAC packet,
with four seconds of initial PCM headroom, bounded backpressure and join before
teardown. It progresses beyond the circular stall. The test driver lost its
scheduled pauses after a startup status-query timeout, so that run cannot be
used as scheduled pause/EOF acceptance. The driver now retries failed status
queries without replaying uncertain toggle requests.

Independent waveform alignment on the new recording qualifies 116 steady
points. Raw accepted-count minus delay errors range -15.984 to +3.262 ms.
The previously frozen offline v1 estimator retains all 116 points, with errors
-7.255 to +0.372 ms and no 21.333 ms violations. This is process-tap steady
clock evidence only, not physical-speaker timing, pause-tail qualification,
production beacon activation or completed release acceptance.

Audio still fails: a roughly 10.667 ms source delay near capture second 78 and
a split late manual-pause tail need resolution. The unchanged candidate is
being repeated with the corrected four-pause schedule. No addon PR push is
permitted until the requested acceptance and both review gates clear.

## Reviewed TS baseline completed; raw-AAC coverage next

The final reviewed native build `38d3f58` passed the 180.032-second controlled
MPEG-TS capture with four normal pauses, retained audio beyond the visible HLS
window and natural EOF. Independent waveform checks detected no extra gaps or
repeated source blocks. The frozen offline estimator retained all 171 qualified
points within the predeclared threshold. This requires the isolated buffered
profile (`audiooutput.lowlatency=false`); it does not qualify low-latency mode,
physical speaker timing, pause tails or production success beacons. Evidence
and review are preserved in PR #4 at `c82d2ac`.

Continue the already planned raw-AAC validation by copy-muxing the same unique
AAC access units into fragmented MP4. Verify packet payload/order and decode the
actual served initialization segment/fragments for an independent reference.
Extend only the loopback fixture server with a fixed initialization-map asset,
retain the six-segment timing/window contract, and test both old TS and new MP4
routes. Do not alter the frozen estimator or native implementation merely to
make the new capture pass. Reuse the normal four-pause capture schedule, verify
that native telemetry actually selected raw AAC, and preserve any format/priming
or startup failure rather than broadening production eligibility.
