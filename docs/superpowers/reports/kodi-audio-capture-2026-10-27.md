# Kodi master interface and audio capture acceptance

## Environment and scope

- macOS 26.6.2; Xcode 26.6; XcodeBuildMCP 2.7.0.
- Isolated portable Kodi master: 23.0-ALPHA1, revision `20261026-8091c14861`.
- Original interface/capture source: `ccdda7ad403d14a1320995fcab35f6bbd9ebc56a`; later clock-fix package evidence is recorded below.
- The separate installed Kodi profile was not modified.
- Production Radio Browser ad insertion and success tracking remain disabled.

## Visible interface

The unlocked desktop was inspected through native UI automation. Kodi's Music
add-ons list displayed Kodi Donor Radio 0.1.0. Opening it displayed setup,
Favorites, tag/country browsing, search, map, settings, ICY labeling, and
controlled-stream entries.

The station map rendered its world geometry, station pins, selected location,
and station list. An Up key moved selection from Lagos to a Moroccan station
and updated the location and station panels. Back to stations returned to the
Donor Radio directory. These observations verify this navigation path on master;
they do not cover every setup dialog, skin, or remote-control layout.

The Settings dialog also rendered its interface, diagnostics, and developer
fixture controls. Run setup opened Find a place; Choose a country then displayed
the 240-entry country dialog. Escape canceled that flow and returned to the
Donor Radio directory without choosing or saving a place. This covers the
settings-to-setup entry and cancellation path, not completion of setup.

## Recorder and audio evidence

The development helper was built and tested separately from the add-on. Its
initial ScreenCaptureKit build launched, but application enumeration required
screen-recording permission. No such permission was granted. The helper was
changed to a Core Audio process tap for the system-audio-only permission path.

The Core Audio helper built successfully through XcodeBuildMCP; all 16 unit
tests passed. Tests cover argument bounds, planar/interleaved sample ownership,
bounded buffer overflow, native 44.1 kHz frame accounting, fractional input
sample timestamps and validity flags, timing gaps, readiness, format changes,
and explicit failure for empty, interrupted, or entirely silent recordings.
Automation tests also require nonzero exits for invalid targets or missing PIDs
while retaining errors in manual mode.
Independent review found no actionable correctness defects.

Two additional regressions reproduced a canceled startup publishing readiness
and activation after a delayed native return. The shared startup gate now
serializes cancellation with readiness publication and checks it again before
creating keep-awake assertions or showing Recording. The regressions failed
before the fix and passed afterward; cleanup still waits for the native call
to return, and canceled startup finalizes as failed with a nonzero automation
exit. This fixes false readiness without claiming to cancel Core Audio's call.

The first Core Audio run saved 1,441,280 frames at 48 kHz, all zero-valued. It
correctly reported failure. The built application was then found to omit
`NSAudioCaptureUsageDescription`; an explicit plist input corrected that, and
the rebuilt plist was inspected. A subsequent start waited for macOS consent
and failed without recording frames. Later macOS privacy logs confirmed that
the user approved an earlier build, but the rebuilt ad-hoc signature no longer
matched that authorization and macOS requested consent again. These attempts
do not establish audible playback. The desktop-control tool cannot operate the
protected system permission dialog; approval must cover the final app build.

The final build was signed locally with an existing Apple Development
certificate and copied to a stable Applications path. Strict signature
verification passed; its designated requirement is certificate-based rather
than an ad-hoc code hash. Personal signing settings were not added to the
repository. Its next attempt returned a Core Audio startup timeout after 180
seconds with zero frames and no recording-ready transition. That attempt did not establish audio-only consent or measured output; the successful later runs are recorded below.

Steward's adversarial review identified that Stop and normal Quit defer cleanup
while the synchronous native startup call is pending. This is retained as an
explicit developer-tool limitation: the configured recording duration does not
bound permission startup, and no native-call cancellation deadline is provided.
Teardown remains serialized to avoid releasing memory still owned by a possible
audio callback. A separate capture worker would be needed to enforce process
isolation for a hard startup deadline. Unattended startup acceptance remains open;
the observed 180-second timeout is not a guaranteed limit.

The finite fixture and control captures below establish bounded captured-output behavior. Recording files and full timing data remain outside the repository. `mapping_proven` remains false until measured
audio and player-clock evidence establish the required behavior across startup,
sliding windows, discontinuities, and playback controls. Captured process audio
does not establish sound at physical speakers.

## Package isolation

The original package build and package checker passed. That add-on ZIP had SHA-256:

`df12f861f174d7132a62efe05649c9f27a69b130cce87d0ec7b977af7a06f227`

The native helper, generated recordings, and Xcode build products are outside
the add-on's explicit packaging allowlist.

## Captured-output follow-up

With the unchanged certificate-signed helper and a fresh permission approval,
the finite fixture completed a 30.155-second recording (1,447,424 stereo frames
at 48 kHz). The captured tones followed preroll, programme A, replacement ad,
and programme B. No 100 ms window was classified as the original-break tone.
This dominant-tone test cannot exclude weak or shorter overlapping audio.

A pause/resume capture completed with 1,448,448 frames. All 28 paused player
samples reported speed zero; the position settled within 76 ms. Captured silence
lasted approximately three seconds, then the expected sequence resumed. A seek
capture completed with 1,442,816 frames; later player samples confirmed a jump
from 8.149 to 12.833 seconds, and the audio changed from replacement into
programme B after approximately one second of silence. Only impression/start
beacons occurred for that interrupted placement. All three completed captures
reported zero capture timing discontinuities.

A 60.064-second live capture contained all 18 uniquely identified later source
segments in order. Kodi reported 37.406 seconds for 109 active samples while
audio continued through the final two segments. The fixture caps at source023
without ENDLIST, so this exposes a stalled-window/buffer-drain failure. The
normal fixture is intentionally retained as this bounded starvation test; its
29 source-manifest requests are not evidence of 29 distinct media windows.

A repeat run reproduced the behavior. A process stack sample caught Kodi's
VideoPlayer thread inside ReadPacket, predominantly sleeping in FFmpeg's HLS
reload loop. Kodi updates its public playback-time cache from that thread;
Python getTime, JSON-RPC time and the UI share the cached value. The addon
cannot reconstruct a rendered position from this clock or segment downloads.
A wall-clock estimate would continue through genuine buffering and is not an
acceptable replacement.

The default-reader live raw-clock calibration failed. That does not establish
that a custom Kodi build is required: the existing inputstream addons have
asynchronous demux paths. InputStream Adaptive 22.3.23 requires an audio-only
master declaring AAC and stable program dates to identify completed live
periods across our ad discontinuities. A bare media playlist was mistaken for
video; an audio master without dates stalled after the first period. Neither
failed attempt counts as acceptance.

With both metadata corrections, a 65.077-second exploratory capture completed
with zero capture discontinuities and an advancing clock through the later
unique segments, including the buffered tail that froze with the default
reader. Some tone windows were unclassified and startup contained a short gap;
this is evidence against the frozen-clock defect, not perfect audio acceptance.
FFmpeg Direct timeshift also avoided the freeze, but started partway through
the presentation and did not establish sample zero. No Kodi-core patch was
applied. Production insertion and success tracking remain disabled; an initial
successful transport test does not establish an absolute mapping after output
window retirement.

## Packaged Adaptive clock fix

The final package used InputStream Adaptive 21+ only when it was already enabled
and only for controlled, ad-enabled HLS compositions. It advertised AAC only
after validating the composition, reserved a bounded master-resource slot, and
checked readiness with a cancellable five-second, 4 KiB, no-redirect loopback
request. Unsupported or unavailable readiness fell back to native playback.
The package tested for this historical capture declared Adaptive as an optional
manifest dependency. Kodi may enable an installed disabled optional dependency
or update an installed version below that declaration during installation, so
the earlier claim that no enabling or update could happen was too broad. Current
source removes that manifest import and relies on runtime detection of an
already enabled addon; the ZIP hash below predates that correction. Program dates
begin at first publication and remain fixed to composed sample positions.

The package SHA-256 deployed into the isolated master profile was:

`a666fe28010d846692752a1543c24cad6d285f23166d45ca186d04e1278ff9c9`

All packaged files were read back and compared with the ZIP before testing.
The complete Python suite passed **1,493 tests**; package validation is included.
Focused tests cover invalid/oversized manifests, redirects, cancellation,
timeouts, native fallback, resource bounds, stable dates and the finite-origin
clock guard. Regressions failed before their corresponding fixes.

The final live capture (`F511503C-DCEA-4160-AF52-4EF95FF189D3`) completed with
3,127,808 contiguous stereo frames at 48 kHz (65.1627 seconds), valid timing
observations and zero reported discontinuities. It contained approximately four
seconds of preroll-band audio, programme A, replacement, programme B, and all
18 continuation tones in order. The same 600 ms startup interruption remains;
shifted-frequency windows were verified as audio, not silence. No later silence
gap occurred before final drain. This is not sample-perfect playback acceptance.

Of 439 observer polls, 424 contained raw-clock observations. Active-item checks
matched relay URL shape, not an independently pinned session identity. The clock
advanced through the final tones; the longest preceding plateau was 151 ms.
Its final plateau began at capture time 55.422 seconds, after measured silence
began at 55.4 seconds. Twenty-one boundary comparisons bracketed timing errors
between -156 and +275 ms, subject to 100 ms tone windows and polling resolution.
The source's intentionally stalled manifest did not reproduce the native clock
freeze. No Kodi clock-core patch was applied.

All 185 addon clock diagnostics in the live test interval reported
`mapped=false`, `tracking_enabled=false`, and `live_clock_unanchored`. The
collector received zero success beacons. A later ENDLIST cannot upgrade an
initially live session; a missing retained sample zero also prevents mapping.
The bounded transport result does not prove an absolute anchor after Adaptive
retires old periods. `mapping_proven=false` and production insertion/tracking
remain unchanged. Full recordings and timing observations remain outside Git.

The same final package also passed bounded finite control repeats. Pause/resume
recorded 1,443,840 frames with zero capture discontinuities; all 23 paused
observations had speed zero and position settled from 2.449 to 2.563 seconds.
Audio was silent from approximately 3.5 to 6.7 seconds, then resumed the expected
sequence. Each expected placement-1 beacon occurred once. The seek recording
contained 1,445,376 frames with zero capture discontinuities. Later polls moved
from 8.517 seconds to the requested region around 12.831 seconds (with a transient
13.309-second report); only impression/start beacons occurred. These remain
finite-fixture diagnostic results, not permission to enable live tracking.
