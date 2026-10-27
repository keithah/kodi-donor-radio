# Controlled continuous audio and absolute clock investigation

The controlled fixture path can now compose HLS segments into one continuous
AAC response. Strict MPEG-TS/PES validation extracts the original ADTS frames;
there is no transcoding or external encoder in the addon. Consecutive output
sample positions survive retirement of older HLS segments. This path is offered
only for loopback fixture compositions, never ordinary Radio Browser stations.

An already-enabled FFmpegDirect 22+ is selected with its timeshift buffer and
the video-default player. The optional package dependency is 22.2.10. Kodi master
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
guards. `candidate_only=true` and `tracking_enabled=false` remain explicit:
these measurements do not establish production or billable tracking. User player
rules can override the video-default preference, and public Python APIs cannot
attest the renderer or atomically pair its URL and clock. Terminal delivery also
ends candidate mapping before buffered tail audio may finish.

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

## Automated verification

The final implementation passed 1,566 Python tests and the distributable package
manifest check. Focused native policy tests cover all nine stream-mode/override
combinations. The addon package SHA-256 is
`84c9c3c475d3c86dc63e3c74e2b774b47cc83b9bdec58ebbf9440d08b961ff31`.
The runtime captures use Kodi master `8091c14861` (23.0-ALPHA1, built 2026-10-26),
macOS arm64, and the isolated portable profile. Installed Kodi 21.3 is untouched.
