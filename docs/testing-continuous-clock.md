# Controlled continuous audio and absolute clock investigation

The controlled fixture path can now compose HLS segments into one continuous
AAC response. Strict MPEG-TS/PES validation extracts the original ADTS frames;
there is no transcoding or external encoder in the addon. Consecutive output
sample positions survive retirement of older HLS segments. This path is offered
only for loopback fixture compositions, never ordinary Radio Browser stations.

An already-enabled FFmpegDirect 22+ is selected with its timeshift buffer and
the video-default player. FFmpegDirect 22.2.10 is detected at runtime; it is not
declared as a package dependency. Kodi master
also needs an explicit music playlist hint when JSON-RPC retains a video tag on
the original plugin item. Without FFmpegDirect, the existing Adaptive/native
fallback remains. The service does not install inputstreams.

## Why another transport was needed

The prior Adaptive path played the extended source through approximately 190
seconds while its public player clock repeatedly jumped backward around
111–116 seconds as windows retired. The 200.053-second
Core Audio capture contained 9,602,560 frames and zero tap discontinuities. Its
unique source tones continued through 2820 Hz while the clock fell roughly
72 seconds behind. The final reported clock was 114.818 seconds. This is
not a missing recording interval or evidence that the source stopped.

Adaptive's elapsed clock subtracts a moving retained-window origin. A playlist
start hint and a later ENDLIST do not establish an absolute origin. The continuous
AAC candidate instead starts at composed sample zero and uses FFmpegDirect's
stable packet timeline. Its delivery ceiling only bounds possible playback;
socket writes are never counted as played ads.

The mapper checks the exact active AAC URL on binding and every poll, rejects
backward clocks, seeks, changed routes, retired sessions, and clocks beyond
delivered media. A session first bound to HLS cannot upgrade because another
client requests its AAC route. Pause/cache/speed evidence retains the existing
guards. Ordinary launches retain `candidate_only=true` and
`tracking_enabled=false`. The explicit **Test ad tracking** context action uses
a single-use service ticket and names VideoPlayer instead of relying on the
user's default player rules. Only its exact continuous route with enabled
FFmpegDirect 22.2.10 can qualify; the adapter still requires an owned AV start
and checks route/session identity around bounded clock reads. Public Python APIs
do not provide an atomic renderer/clock attestation, and none of these fixtures
establish production or billable tracking. Terminal delivery ends mapping before
buffered tail audio may finish, so finite sources can produce no success events.

## Startup buffering experiment

The original realtime startup path applied an approximately 655 ms audio clock
correction. A separate, optional FFmpegDirect patch honors its existing explicit
`is_realtime_stream=false` property while preserving the omitted timeshift
default. See [the patch and reproduction notes](patches/README.md). It changes
the inputstream addon, not Kodi core, and no native binary is distributed here.

A patched external continuous-AAC probe captured the full four-second 990 Hz
preroll, followed by programme and replacement tones without inserted silence
after playback began. Eighteen later unique-tone comparisons placed the nearest
raw clock observations between -243 and +74 ms of their sample-derived boundaries.
Those figures include 100 ms audio windows and roughly 100 ms RPC sampling; they
are bounded measurements, not sample-accurate proof.

The initial PAPlayer alternative was rejected: its clock froze while buffered
audio continued during starvation. A second invalid run reused that stuck
player. Two initial integrated trials did not start playback because of the
JSON-RPC media-type ambiguity. None is counted as acceptance evidence.

## Captured extended result

The integrated patched run captured 9,611,264 frames over 200.235 seconds with
zero reported or measured timing gaps. All unique source tones 006–092 were
classified: 87 boundaries, including 31 beyond output position 128 seconds.
The late-boundary classification/RPC offset envelope was -390 to +200 ms,
without a growing offset trend. No classified silence occurred between first
audio at 1.1 seconds and stop at 190.6 seconds, and no sustained raw-clock
plateau occurred during audio. This run used package SHA-256
`db96c76c5ee90253f89db0bd8497181a5346d9e93d3be2a3b828c409aedd9f5c`;
the later package also preserves the candidate-only flag in serialized logs.

## Stock inputstream and control checks

Unmodified FFmpegDirect 22.2.10 also completed an extended pause/resume run.
It retained bounded late clock alignment across 85 unique source boundaries,
including 29 beyond 128 seconds (late envelope -384 to +200 ms). The three-second
pause held the raw clock near 1.33–1.42 seconds. A separate approximately 210 ms silent interval
appeared during resume settling (6.615–6.825 seconds, measured at 5 ms resolution); this run is not evidence of seamless resume.
The 200.149-second recording contained 9,607,168 frames, zero capture timing
gaps, and zero success beacons. Resampling shifts and the resume gap prevent
claiming an independently proven full preroll sample count in that run.

A separate 40.107-second captured seek run had 1,925,120 frames and no timing
gaps. The requested backward seek was observed in Kodi callbacks and raw time
(7.519 to 6.061 seconds). The candidate permanently changed to
`continuous_clock_seek`; subsequent playback did not reactivate tracking.

The 80.224-second starvation capture contained 3,850,752 frames and no timing
gaps. All 18 unique source tones through source023 played before silence at
53.7 seconds. Its 146-sample, 16.434-second raw-clock plateau at 51.846 seconds
contained only silence, not continuing programme audio. Kodi reported caching;
no success beacons were emitted. This does not test recovery after a prolonged
upstream outage.

The continuous transport therefore does not require a Kodi core patch. The
optional FFmpegDirect change addresses its startup buffering policy separately.
Stock startup still exhibits resampling/correction behavior; the private patch
is not represented as a released dependency.

## Reproducing the measurements

Generate the longer source with `tools/generate_ad_audio.py --extended`, then
serve it with `tools/ad_fixture_server.py`. The extended fixture contains 128
source segments (256.683 seconds), exceeding the 64-output retention limit.
Each source segment has 94 AAC frames, 96,256 samples at 48 kHz, independently
validated PTS steps, and a declared identifying tone. The composition inserts
192,512 preroll samples and replaces the original break with equal-length media.

Use the approved process-audio capture helper and record Kodi RPC observations
on the same Mach absolute clock. Analyze the saved capture with:

```sh
python3 tools/analyze_captured_ad_audio.py /absolute/path/to/capture \
  --metadata tests/generated-audio/ad-extended/metadata.json \
  --preroll-samples 192512 \
  --output /absolute/path/to/capture/measured-audio.json
```

The developer analyzer needs FFmpeg to decode CAF; its measurement core uses
only Python's standard library. It checks captured frame ranges and timestamp
continuity, distinguishes silence from unclassified audio, and reports observed
RPC brackets around sustained unique tones. It neither interpolates a supposedly
exact player position nor equates the recorded item-match flag with independently
verified session identity. Raw captures remain local; reports do not claim that
the physical speakers were heard.

## Earlier baseline verification

The earlier implementation passed 1,566 Python tests and the distributable package
manifest check. Focused native policy tests cover all nine stream-mode/override
combinations. The addon package SHA-256 is
`84c9c3c475d3c86dc63e3c74e2b774b47cc83b9bdec58ebbf9440d08b961ff31`.
The runtime captures use Kodi master `8091c14861` (23.0-ALPHA1, built 2026-10-26),
macOS arm64, and the isolated portable profile. Installed Kodi 21.3 is untouched.

## Qualified launch and resume follow-up

The new controlled context action uses bounded launch phases: 30 seconds waiting
for resolution, 40 seconds for preparation, and 30 seconds after publication to
qualify the selected transport. Replay, cancellation, a newer selection, a changed
route, repeated AV startup, seek and failed/slow observations cannot qualify
success. Pausing or resuming marks the interrupted placement ambiguous; later
progress cannot catch up its quartiles or completion. A zero-start preroll still
lacks an independently observed entry boundary and sends no success beacons.

Fresh captures use stock FFmpegDirect 22.2.10 and the same isolated Kodi master.
No Kodi-core change or rebuilt recorder is involved. These are bounded local
measurements, not production or physical-speaker attestation:

| Capture | Result |
| --- | --- |
| `qualified-extended/2B83EC70` | Early clean midroll emitted each of six events once; no preroll events. Its original fixture timestamps used Python 3.9's process-relative epoch, so they are not used for cross-process timing acceptance. |
| `qualified-late/62B44726` | 168.224 seconds, 8,074,752 frames, no capture timing gaps. Replacement at composed 144.384–148.394667 seconds survived retained-window retirement. Fourteen unique late markers had raw-clock brackets −343 to +213 ms around expected boundaries. |
| `qualified-pause/9C0B16E3` | 34.059 seconds, 1,634,816 frames, no capture timing gaps. Pause during the midroll allowed only impression/start/first-quartile already reached; no midpoint, third-quartile or completion after resume. Resume still contains a short additional silent interval. |
| `qualified-seek/3879D70A` | 32.139 seconds, 1,542,656 frames, no capture timing gaps. Seek permanently invalidated the mapper; the remaining three milestones were suppressed. |
| `qualified-recovery/50743348` | The initial 20-second source hold did not recover in the player. Audio stopped at about 18 seconds; raw clock 15.783 plateaued over silence. No success events. This is failure/suppression evidence, not a recovery pass. |

The late replacement tone occupied capture time 146.5–150.5 seconds at 100 ms
classification resolution. Same-domain Mach timestamps place start at 146.615,
first quartile 147.768, midpoint 148.621, third quartile 149.781, and completion
150.636 seconds; impression was 146.615. These are about 0.11–0.28 seconds after
the corresponding classified milestones, with audio-window uncertainty. They
support this explicit fixture route, not universal sample-accurate rendering.

Fixture event telemetry now reads Mach ticks directly on Darwin. Merely seeing
`mach_absolute_time()` in Python's clock description was insufficient: Python
3.9 subtracts a process-specific origin. Tests cover native tick conversion and
refuse to label a fallback clock as Core Audio-comparable.

### Resume alternatives tested without Kodi-core edits

Speech correlation of the prior patched VideoPlayer pause found approximately
214 ms of source content absent around resume. Older finite Adaptive captures
also omitted about 217–230 ms, so the problem is not specific to the continuous
AAC route. Shorter silence alone is not evidence of preservation.

The supported `inputstream-player=audiodefaultplayer` alternative selected
PAPlayer in a fresh capture (`paplayer-pause/B408B379`). It skipped roughly
1.01 seconds at preroll startup, and pause alignment showed roughly 89 ms of
additional unrendered or inaudible source advancement (fade behavior prevents
claiming an exact dropped-buffer count). Its later raw clock lagged the expected
source position by roughly one second. It is rejected as a fix, and the installed
addon was restored to VideoPlayer.

Source tracing shows both players eventually reach ActiveAE's single-stream
pause flush; PAPlayer first fades for 80 ms. The larger coordinated pause-boundary
proposal is deferred at the user's request. Seamless resume remains unresolved;
this change suppresses misleading tracking instead of claiming the omission is
fixed.

### Local PCM isolation of the resume omission

A follow-up on 2026-10-27 opened a local PCM Matroska file directly in
VideoPlayer through JSON-RPC on the same isolated Kodi master. The file contains
the decoded replacement000/001 and source000/001 fixture audio. This bypasses
the donor-radio addon, relay, HLS parsing, network transport and inputstream
addons. The approved recorder captured the same Kodi process without rebuilding
or changing its permissions.

| Audio setting | Capture | Frames (48 kHz) | Quiet interval | Quiet duration minus settled source-alignment shift |
| --- | --- | --- | --- | --- |
| Low latency enabled (original setting) | `local-pcm-pause/4BB9F294` | 963,072 | 3.395–6.555 s | approximately 237 ms |
| Low latency disabled | `local-pcm-standard-buffer/5F2D8262` | 970,752 | 3.270–6.785 s | approximately 432 ms |

Both captures reported zero timing discontinuities. Full-file pre-pause matching
includes a 64 ms startup resampling transition, so it must not be described as
stable throughout. A separate settled-window analysis excludes the first 1.5 s
and requires correlation >= 0.99. It finds 21/41 and 22/42 accepted before/after
speech matches respectively; within-side offset spread is at most one 48 kHz
sample. The quiet edges have approximately 10 ms combined uncertainty. These
figures describe missing or inaudible source advancement in captured process
audio, not physical speaker measurements or exact discarded-buffer counts.
Full raw matches and `settled-pause-alignment.json` remain with each local capture
under `/private/tmp/kodi-audio-validation-20261027/`.

Kodi accepted the low-latency setting change through JSON-RPC. The original
`true` value and restoration to `true` were read back before and after testing. Disabling it is rejected as a fix. An initial WAV attempt was
rejected by Kodi's player-selection rules before playback and is not evidence.
The Matroska runs establish that this omission does not require HLS or the addon.
They agree with source tracing: ActiveAE's single-stream PAUSESTREAM path calls
FlushEngine, which clears sink buffers. Keep-audio-device-alive controls idle
suspension, not preservation of these samples; the default one-minute timeout
already exceeds these three-second pauses. No core change was made, and no safe
add-on-only preservation fix has been verified. Interrupted placement success
reporting remains suppressed.

The final packaged clean-break rerun (`qualified-final-clean/41AAC911`) recorded
1,449,984 frames over 30.208 seconds without timing gaps. Midroll tone boundaries
were 10.1–14.1 seconds; start/first-quartile/midpoint/third-quartile/complete arrived
at 10.411/11.536/12.354/13.290/14.416 seconds. Each event occurred once, after its
classified milestone (100 ms audio resolution). The installed source ZIP matched
all 40 packaged files, SHA-256
`837d3c53e8d8f77353250f37da5f0c5a0876d04ea6f613edc7324ca5a836c3b8`.

The shortened 12-second hold did recover (`qualified-recovery-final/764B0500`):
43.040 seconds, 2,065,920 frames, no capture timing gaps. The raw-clock plateau
coincided with approximately 0.90 seconds of silence, followed by replacement
and programme audio. However, later source boundaries showed a clock lag of
approximately 0.24–0.58 seconds, and the initial implementation emitted all six
events despite this changed alignment. This run is a **failed qualification
test**, not acceptance of those beacons. It motivated permanently invalidating
the qualified continuous clock on buffering after confirmed startup. Recovery
can continue playing audio, but cannot re-enable success tracking within that
session. Startup buffering before confirmed progress remains pending.

The source-hold failure was traced to Kodi's default 20-second Curl low-speed
timeout (`CCurlFile::CReadState::FillBuffer`, error 28). A Python consumer with
a longer read timeout successfully consumed the same 20-second-hold composition.
The positive fixture now holds 12 seconds and exposes the completed break at
18.016 seconds, after the initial 16.043 seconds of media can drain, without
changing Kodi's timeout or production transport behavior.

With the guard installed, the fresh `qualified-recovery-guarded/E18C584C` capture
contained 2,021,888 frames over 42.123 seconds, without timing gaps. Audio resumed
after about 1.9 seconds of classified silence and continued through replacement
and programme. **Zero beacons** were sent, and later advancing raw-clock samples
remained diagnostic with `continuous_clock_cache`. This verifies recovered
playback plus conservative tracking suppression, not a repaired recovery anchor.

An unavailable player/cache read after confirmed startup also permanently revokes
qualification (`continuous_observation_unavailable`): that failed observation
could hide a buffering transition. Regression tests cover both clock and cache
getter failures followed by apparently small normal clock advances, and ensure
that later milestones remain suppressed.

The final read-failure guard was packaged as SHA-256
`c19bd3069f4d94f8430eafde9fe38cc8cfed36e8c2f137ee68881e0681aea269`
and retested (`qualified-release-clean/DAD4A8A9`): 1,354,240 frames over
28.213 seconds, no timing gaps, and each of six clean-midroll events once.
Completion arrived at capture time 14.205 seconds; a separate two-frequency
10 ms analysis placed the transition from replacement to programme at
14.15–14.17 seconds. This supports completion after that recorded boundary
in this run, while retaining the broader timing limitations above.

Final source verification: **1,644 Python tests passed** in 178.65 seconds,
including launch lifecycle, observation failure, caching and recovery regressions.
The source-only release ZIP passed its manifest checker. No native recorder or
Kodi-core build was performed for this change.
