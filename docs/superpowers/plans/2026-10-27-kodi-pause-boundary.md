# Kodi pause-boundary design proposal

Status: rejected by the user on 2026-10-27: "no kodi patch". The experimental
resume changes have been removed from the Kodi source checkout. The existing
USB initialization fix remains intact. Experimental builds and captures are
retained only as diagnostic evidence, not as a supported addon requirement.
Further resume work must stay outside Kodi core.

The tested playback paths lose audio around pause. No supported addon-level
remedy has yet been verified for preserving the queued samples.
Captured speech alignment shows about 214 ms of missing content; ActiveAE's
single-stream pause flush releases pending output and then inserts synchronization
silence on resume. Removing the flush or using the existing stream drain alone
would not coordinate the already-frozen player clock with hardware output.

## Proposed scope

A separate Kodi-core change, not part of the radio addon or its ZIP:

1. Define a bounded pause request/acknowledgement between VideoPlayer's audio
   worker, its sink adapter and ActiveAE. Stop admitting new audio at a known
   boundary while preserving queued samples.
2. Let the clock advance with already-admitted audio until the sink confirms
   the last audible boundary; then freeze clock and demux. The existing stream
   drain is not a device-output acknowledgement and cannot be substituted alone.
3. Resume from that exact boundary. Seek, stop, device changes and failed sinks
   must cancel the pending pause safely. Concurrent GUI sounds must remain valid.
4. Keep platforms/backends without the new contract on their existing behavior
   until their own sink semantics are tested. Do not claim preservation there.

This can add a small bounded pause response delay equal to already queued audio,
while preventing lost or duplicated content. The API shape should be reviewed
against Kodi's threading and sink lifetime rules before coding.

## Acceptance

First add a deterministic fake sink consuming numbered 48 kHz PCM samples.
Check that pause acknowledgement identifies the audible tail, each sample appears
exactly once after resume, no silence is synthesized, and the clock freezes at
that same boundary. Cover empty/nonempty queues, repeated pauses, seek/stop during
pending pause and GUI audio. Then build isolated Kodi and capture 1/3/10-second
pauses on the same Mac; correlate decoded speech and sample markers with source,
clock and pause acknowledgements. Keep USB work and the approved recorder intact.

A full configured core build is needed; the current tested Kodi application has
no available reusable object cache. No core implementation is included in this
proposal, and no resume fix is claimed yet. The independent addon work can enable
qualified clean-break fixture beacons while suppressing pause-interrupted ads.

## Follow-up evidence and narrower next step

The 2026-10-27 investigation tested a small existing-interface experiment in an
isolated Kodi build, retaining the USB patch. It preserves already queued PCM
on a steady single-stream pause and avoids startup muting in both resume
handlers. A same-toolchain baseline still omitted approximately 241 ms. The
corrected experiment's 1/3/10-second PCM captures showed residual source
advancement of -8.69/-8.75/-5.0 ms, inside the approximately 10 ms analysis edge
uncertainty, but audible pause tails remained approximately 244–353 ms.
The first stock-FFmpegDirect HLS run paused before audible startup and used
resampling, so it did not activate the experimental path and is not HLS pause
acceptance. A paired HLS comparison with the previously prepared realtime override then
used the same raw-clock >= 2.5 s pause gate on both builds. The baseline omitted
approximately 248.3 ms; the candidate residual was -6.0 ms, within the 10 ms
edge uncertainty. Its pause tail increased from 120–122 ms to 244–274 ms.
Both captures had zero reported timing discontinuities. This confirms the
content-preservation improvement and the pause-latency tradeoff on controlled
HLS; it does not establish a production-ready pause or clock contract.

This is diagnostic evidence, not an accepted production fix. Code review flags
continued output after pause and possible loss of sync if the sink reopens.

A narrower alternative to the original full player-clock handshake would add
capability-gated sink pause/resume, initially for macOS PCM:

- An optional sink operation reports whether it can preserve unread queued
  frames while paused; unsupported backends retain their current behavior.
- ActiveAESink stops dequeuing output samples while paused but continues
  processing control messages, including resume, stop, seek and device changes.
  It must not block resume behind a full-ring write or invoke an idle drain that
  consumes/discards the retained queue.
- The macOS render callback supplies silence without advancing its PCM ring's
  read position. Frames already submitted to CoreAudio remain an unavoidable
  hardware-latency tail and must be measured rather than called instantaneous.
- Queue delay accounting remains valid while paused. Resume eligibility records
  the actual pause decision; a sink reopen or flush invalidates that decision.
- Stop/seek/device-loss paths explicitly release the paused state and discard
  stale media as appropriate; failure restores the existing fallback safely.

This crosses ActiveAE, ActiveAESink, the sink interface and the macOS backend.
It was a separate scope decision from the small diagnostic experiment; the user declined
this API work. Exact audible-boundary/player-clock alignment and
post-interruption success beacons remain outside this narrower proposal.

Acceptance would first use a numbered-PCM fake sink to verify preservation,
control responsiveness, repeated pause/resume, stop/seek while paused, unsupported
backends and failed/reopened sinks. Then repeat the same recorded PCM and
controlled HLS comparisons, measure command-to-silence latency and source
continuity, and reject any missing/repeated source interval or new hang. Keep the
original build and approved recorder unchanged for comparison.

## Addon-only seek recovery — rejected because it replays audio

Further testing used Kodi with only the existing USB initialization fix. No
resume-related core changes were present. Supported JSON-RPC `Player.Seek`
repositioned playback while paused, before normal resume.

Seeking to the exact paused clock is insufficient evidence of recovery: local
PCM showed an additional 50 ms settling gap, and net apparent replay can hide
an omitted interval elsewhere. A deliberate one-second seek-back gave stronger
bounded evidence on both stock FFmpegDirect 22.2.10 and the existing optional
realtime-override build:

- Stock HLS recovered source speech windows around 2.60–2.88 s after resume at
  correlation >= 0.9972, spanning the estimated previously audible endpoint near
  2.79 s. No subsequent quiet gap occurred before that recovered boundary.
- Corrected-inputstream HLS recovered windows around 2.60–2.92 s at correlation
  >= 0.9915, spanning the previous endpoint near 2.742 s. Its 5 ms settling gap
  occurred while replaying earlier content, approximately 0.97 s before reaching
  that endpoint.
- Local PCM also replayed the speech across the old endpoint after settling.
  A late plain reseek at 140.066 s remained within the available live buffer;
  that test is not equivalent to a late one-second-recovery acceptance run.

The captures establish recovery of the measured boundary speech, not universal
sample preservation. Stock inputstream startup truncation and resampling drift
remain separate limitations. Interrupted-ad success beacons stay suppressed.

Rejected proposal (do not implement): an opt-in, initially
controlled-fixture-only recovery mode makes one backward seek while playback is
still paused. Bind the operation to the session, exact relay route and playback
generation. Require an established, positive, stable clock and available seeking.
Consume the operation before issuing the seek; do not retry automatically.
Cancel on resume, stop, station change, user seek, stale callbacks or buffering.
Short pauses that end before these checks pass retain ordinary Kodi behavior.
Never seek later into a replacement station, and never treat a seek response as
proof of rendered audio or restored advertising qualification.

The production addon has not been changed to perform this recovery. The user
explicitly rejected deliberate replay: resume must avoid both gaps and replay. Captured evidence is in
`/private/tmp/kodi-audio-validation-20261027/` under the `addon-reseek-*` and
`addon-rewind-one-second-*` directories. The completed full-HLS comparison and
source-coverage report should be retained with those recordings.


## Addon-only delivery-hold investigation

The user requires both no missing audio and no deliberate replay. Native Kodi
pause remains outside the addon's interception boundary: Python callbacks arrive
after the pause operation. The following tests therefore investigate a separate
addon-owned hold control, not a repair to Kodi's ordinary Pause button.

All live runs used the original isolated USB-only Kodi binary SHA-256
`b4375fd9aa7c4fc0f250d793ac32e08beea6a2427ad81f1b35e5f8fef15d6f51`.
The Kodi core checkout remained clean. The approved recorder was not rebuilt.
Evidence is retained in
`/private/tmp/kodi-audio-validation-20261027/relay-hold-probe/`.

- AAC silence substitution failed an offline decoded comparison: inserting 141
  silence frames at source frame 120 changed 1024 source PCM samples after the
  intended silence was removed. Maximum error was 0.1263. Unchanged compressed
  source frames do not imply an unchanged audible boundary.
- Holding unchanged AAC delivery for 12 seconds after frame 375 failed in
  VideoPlayer. The clean captured run omitted the entire known 550 Hz programme
  block at source 8.021333–12.032 s, and added a 345 ms internal quiet interval
  after returning. Capture timing itself reported no discontinuities.
- A PAPlayer run selected through an explicit audio MIME type and music-tagged
  Python ListItem is more promising. Its pre/post speech alignment shifted by
  5.984 seconds, matching the measured hold within the 10 ms edge uncertainty.
  Both channels recovered the measured speech after the boundary, without a
  secondary quiet interval. Repeated fixture sounds make this insufficient for
  exact boundary coverage; a second reference adds a deterministic quiet unique
  signal before a single AAC encoding, then uses the decoded final AAC as its
  comparison source.

Direct JSON-RPC URL launches rejected PAPlayer selection and are not playback
evidence. A temporary diagnostic entry point in the isolated installed addon
selected the music player successfully; it was restored byte-for-byte after
capture. The original VideoPlayer trial with repeated current-item polling is
superseded by `capture-clean`, because those queries opened extra metadata
connections. Failed/aborted recordings are retained and explicitly excluded.

No production recovery behavior or success-beacon qualification was enabled.
A delivery hold has different control semantics: already-buffered content can
continue before silence, and a long hold can hit network timeouts. Standard
Kodi Pause is still destructive. Any viable music-player hold therefore needs
its own interaction, latency, long-hold, stop/station-change and clock tests
before integration; a completed recording alone is not acceptance.


The unique-reference PAPlayer recording is
`capture-addon-pa-unique/Kodi-2026-10-28T03-54-45Z-959539CD-8836-433B-B877-F84E781D3962`.
The raw player clock froze at approximately 5.495 s during the hold although
source-aligned output reached approximately 6.016 s. Thus this alternative does
not itself establish an absolute rendered-clock anchor or authorize beacons.


Independent stereo analysis of the unique-reference recording recovered the
same offset on both channels: 48,851 samples before the hold and 335,571 after
it, a 286,720-sample / 5.973333 s delay. Unique anchors correlate >= 0.998;
nonlocal alternatives remain <= 0.957. All overlapping 40 ms post-seam windows
correlate >= 0.9943 with only 0–1 sample drift, and all 64-sample seam-adjacent
windows correlate >= 0.99. This supports continuous logical source coverage at
source 6.016 s without a macroscopic omitted or repeated interval. Filter
ringing and dither prevent a bit-exact-output claim.

The independent report is `unique-stereo-hold-coverage.json` inside that capture
directory. It also identifies a separate startup defect: material reference
energy at 30–50 ms is essentially absent at the established capture alignment,
with partial attenuation at 50–60 ms. Therefore full-source-prefix preservation
has not passed. The result is a promising bounded hold diagnostic, not a
finished fix, ordinary Pause support, or full playback acceptance.

The user rejected a separate addon Hold/Resume control. Kodi normal Pause must
work without omission or replay; the delivery-hold approach is not an accepted
solution. No production interaction has been changed to silently substitute
this behavior for Kodi Pause, and no further core changes are proposed here.


## Native-addon auxiliary-stream probe with normal Kodi Pause

A separate bounded experiment used the public C++ `kodi::audioengine::CAEStream`
API inside a temporary FFmpegDirect build. No Kodi-core source or binary was
changed. It tests the single-stream-only `FlushEngine` branch by retaining a
second stream for the loopback fixture session. This is not yet a production
solution: the behavior depends on an internal stream-count condition, needs a
native addon dependency, and can affect shared audio-engine configuration.

All pause/resume requests used `Input.ExecuteAction` with action `pause`, the
normal Kodi action. The experiment did not seek, replay audio, intercept the
button, or substitute a separate hold control. The initial native-addon build
allocated a paused empty stream; the second fed silence using the public API.

- VideoPlayer plus paused auxiliary stream still showed approximately 118 ms
  net source advancement across the pause, with approximately 10 ms edge
  uncertainty. Normal pause and helper allocation/release were verified.
- VideoPlayer plus actively fed silence still showed approximately 113 ms.
  ActiveAE logged approximately 142 ms positive sync error on resume; preventing
  the single-stream flush does not eliminate the separate resume-sync behavior.
- PAPlayer plus actively fed silence narrowed the repeated-fixture estimate to
  approximately 10.7 ms, near edge uncertainty. A separate unique-reference
  normal-Pause capture was then made to distinguish skipped source from the
  ordinary player fade. This path still has an approximately one-second
  startup omission with the controlled FFmpegDirect route.

The probe source and binaries, logs and captures are retained under
`/private/tmp/kodi-audio-validation-20261027/native-pause-probe/`. All temporary
installed Python/native addon files and inputstream build source/output were
restored after the captures. The diagnostic enable file was removed and the
isolated test application stopped. Success-beacon qualification is unchanged.


### Matched unique-reference native-Pause results

The matched PAPlayer baseline without the helper advanced across approximately
8,383 source samples (174.65 ms) at the quiet boundary. With the active helper,
the same reference and core/profile showed approximately 15–16 samples of edge
separation, below the 5 ms RMS edge resolution. This supports a causal
improvement, not a sample-perfect or zero-attenuation claim. Independent stereo
anchors distinguish the unique source from repeated tones.

Three normal pauses of 1, 3 and 10 seconds were then captured in one session.
With the active helper, all 1,408 settled 40 ms windows per channel correlated
>= 0.9952 with at most one sample drift; no later macroscopic omitted/repeated
block was found. Native fade-out/in remained approximately 80 ms, with fitted
near-zero source separations around 5.33 ms per pause. The paused-empty helper
was worse in the same three-pause experiment: fitted separations approximately
16, 27 and 16 ms. Do not substitute the dormant helper based on stream-count
reasoning alone.

These captures do not establish completely lossless audible seams. Startup
still omitted approximately one second. Active-helper audible pause tails
reached approximately 426 ms, with first resumed audio about 97–108 ms after
the resume request. Absolute rendered-clock mapping and beacons remain
unqualified. Review additionally requires explicit per-session gating, safe
native allocation failure handling, lifecycle/device/format coverage and
verified PAPlayer selection on every supported Kodi build. The stream-count
flush behavior is not promised by the public addon SDK.

A stop/EOF issue was also observed in the finite AAC timeshift diagnostic:
after Stop was accepted, the player remained active. A process sample placed
the application thread waiting in PAPlayer::SoftStop for the streams mutex,
while the playback thread was reading empty timeshift packets. This was
recorded before stopping the isolated application. Helper causality is not
established; do not count RPC acknowledgement or terminating the application
as successful playback cleanup. The static finite source differs from the
live HLS fixture, so EOF and live-starvation behavior need separate validation.

Detailed source alignment and both-channel timing checks are in
`native-pause-probe/unique-native-pause-comparison.json` and
`native-pause-probe/repeated-native-pause-comparison.json` under the evidence
root. `experiment-manifest.json` records exact binary/reference identities;
`dormant-stop-sample.txt` records the stop failure. The active and dormant probe
sources/binaries are preserved externally. Every temporary installed file was
restored byte-for-byte, the diagnostic marker removed, and the isolated
application and local diagnostic HTTP server stopped. No production fix,
beacon enablement, Kodi-core change or upstream PR is claimed by these probes.

## Native-addon startup and finite EOF repair (captured 2026-10-27)

The next isolated FFmpegDirect candidate fixes two independent bugs without a
Kodi-core change. `TimeshiftSegment` previously omitted the first seek index
when the first packet timestamp was zero. PAPlayer's initial Seek(1)/Seek(0)
therefore selected packet 47 at second one. Indexing packet zero and clamping
an earlier seek to packet zero removes that one-second startup omission.

At a finite upstream EOF, the timeshift producer previously kept retrying and
returned empty wait packets forever. The candidate records genuine
`AVERROR_EOF`, permits all buffered packets to drain, then returns terminal
EOF. Temporary empty reads remain nonterminal. The standalone patch is
`docs/patches/inputstream.ffmpegdirect-22.2.10-timeshift-startup-eof.patch`;
it excludes the experimental auxiliary audio stream. This is a native-addon
candidate, not a Python-only change or a qualified release dependency.

Candidate binary SHA-256:
`b4615d423d09e5295b33f49320c3dd5ed435eaef858d33b63dd62917932f3991`.
Evidence directory: `native-pause-repair/capture-startup-eof/` under the local
audio validation root, capture suffix `268661F7-6D7A-4720-A3D3-92EE9D9C0657`.
The 58-second capture contains 2,789,376 contiguous frames. Source 10–30 ms
matches both channels >= 0.99997, and the final source 20 ms matches both
channels >= 0.99918. The initial low-energy 0–20 ms is not bit-exact; these
results establish removal of the large startup loss, not sample-perfect onset.
Playback naturally becomes idle, and 11.94 seconds were captured after the
mapped source ending. The audio tail continues about 219 ms after the first
idle observation, so neither idle nor the player clock qualifies success
beacons.

Three additional Stop/reopen cycles each first reached active playback and a
raw clock of at least two seconds, then reached idle in 0.32–0.44 seconds.
One cycle stopped while normally paused. The diagnostic HTTP server was
corrected to support Range requests. Earlier reopen attempts against Python's
plain HTTP server encountered range rejection and did not establish playback;
`stop-reopen.json` is invalid as a Stop success test. Use
`stop-reopen-ranges.json` instead. Live starvation is still a separate test.

The three captured normal Pause cycles still have fitted near-zero source
separations of about 5, 16 and 5 ms. The native fade is NOT fixed, lossless
resume is NOT established, and success beacons remain unqualified. A further
lab-only public-audio-API renderer experiment is investigating whether an
addon-owned source queue can survive normal Pause without the player's fade
consuming programme samples. It must not be presented as a release fix until
source-aligned captures, shutdown and timing tests pass.

### Review corrections and owned-renderer proof of concept

Review found that an unfinished live HLS playlist may report `AVERROR_EOF`
after exhausting FFmpeg reload attempts. The startup/EOF patch now requires
explicit `inputstream.ffmpegdirect.terminal_eof=true`, defaulting off and
forwarded by the addon manifest. The isolated finite fixture sets this;
production radio playback does not. Terminal consumer reads retain a 10 ms
wait, avoiding a paused-EOF busy loop. These safeguards replace the earlier
unconditional EOF candidate. Live starvation/recovery and seek-after-EOF still
need their own acceptance tests.

A second lab-only native addon prototype owns programme rendering through
Kodi's public `CAEStream` API. For the exact loopback unique AAC fixture it
feeds the already decoded reference PCM into an active stream, substitutes
silent AAC packets for the host PAPlayer, and gates feeding on ordinary
`xbmc.Player` pause/resume callbacks. It neither pauses nor rewinds its own
source queue. The programme stream explicitly drains after its final sample;
without that drain the host could remain active at the end. Normal Kodi Pause
was exercised through `Input.ExecuteAction("pause")`; no alternate pause UI,
core edit, private function interception, or seek/rewind was used.

This is a bounded feasibility experiment, not a live-stream implementation:
it reads one fixed local PCM reference (maximum 64 seconds), uses fixed local
control files, and does not yet decode or queue arbitrary stations. The
production addon does not install or enable it. Session isolation, live
input/decoder integration, seek semantics, formats/devices, user volume and
clock mapping remain outstanding. In particular, these captures must NOT
qualify production success beacons or imply release readiness.

The final drain-enabled binary is SHA-256
`c486637f44084d8a236219198881a05f8834ae719435ca8356d9c1da0d37c614`.
Final evidence is under
`native-pause-repair/owned-renderer/capture-drain/`, capture suffix
`EEA020B7-6EC2-4E62-8D1E-D40A07969D1A`. It records normal pauses of 1, 3 and
10 seconds with all six pause/resume actions present. The capture contains
2,786,304 contiguous frames and no timing-sidecar errors. Both channels match
the reference opening 0–20 ms at correlation >= 0.999999417 and unit gain.
The final 20 ms is present at correlation >= 0.999266 and gain about 0.99918.

For each quiet seam, independently aligned source samples on both sides were
combined to test coverage. Every overlapping 1 ms window, stepped one sample
across a 200 ms boundary region, retained source gain between 0.94189 and
1.07338, with correlation >= 0.98573. No 1 ms missing or doubled source
interval and no old 80 ms fade envelope were observed. This is stronger than
the earlier 5 ms comparison but is NOT an exact zero-sample omission/replay
proof: the process tap includes filtered fractional-phase differences.

The mapped source ends at capture time 45.53629 seconds. Native logs show
programme submission, audio-engine drain, host stream completion and natural
`OnPlayBackEnded`; the first observed idle is at 46.30254 seconds, before
observer cleanup. Three subsequent reopen/Stop cycles first reached active
playback with advancing clocks and then idle in 0.33–0.44 seconds, including
Stop while normally paused (`stop-reopen-drain.json`). The earlier owned
no-drain capture is retained as a failed EOF baseline; its Stop acknowledgement
must not be mistaken for natural completion.

Kodi core remains the unchanged USB-only build SHA-256
`b5d4848af4a84a86c4a52eb03d03199e9773cef61dfc38f1b51d24848b3e8752`, and its
source tree is clean. The approved recorder binary remains SHA-256
`35eaed2cf477104f5459b5f0ac49c9bb1d0c9bd3361f075cc141e114854f9a58`.

After the final recording and Stop/reopen checks, the isolated Kodi instance
and local range fixture server were stopped. Its installed inputstream binary,
radio entry point and inputstream manifest were restored byte-for-byte to the
saved originals, and both opt-in markers were removed. Native experiment
sources, build outputs and captures remain preserved under the evidence root;
no lab renderer is installed into the production addon.
