# Kodi master interface and audio capture acceptance

## Environment and scope

- macOS 26.6.2; Xcode 26.6; XcodeBuildMCP 2.7.0.
- Isolated portable Kodi master: 23.0-ALPHA1, revision `20261026-8091c14861`.
- Add-on source under test: `ccdda7ad403d14a1320995fcab35f6bbd9ebc56a`.
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

## Recorder and audio evidence

The development helper was built and tested separately from the add-on. Its
initial ScreenCaptureKit build launched, but application enumeration required
screen-recording permission. No such permission was granted. The helper was
changed to a Core Audio process tap for the system-audio-only permission path.

The Core Audio helper built successfully through XcodeBuildMCP; all 14 unit
tests passed. Tests cover argument bounds, planar/interleaved sample ownership,
bounded buffer overflow, native 44.1 kHz frame accounting, fractional input
sample timestamps and validity flags, timing gaps, readiness, format changes,
and explicit failure for empty, interrupted, or entirely silent recordings.
Automation tests also require nonzero exits for invalid targets or missing PIDs
while retaining errors in manual mode.
Independent review found no actionable correctness defects.

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
seconds with zero frames and no recording-ready transition. Audio-only consent
and measured output still require verification.

Steward's adversarial review identified that Stop and normal Quit defer cleanup
while the synchronous native startup call is pending. This is retained as an
explicit developer-tool limitation: the configured recording duration does not
bound permission startup, and no native-call cancellation deadline is provided.
Teardown remains serialized to avoid releasing memory still owned by a possible
audio callback. A separate capture worker would be needed to enforce process
isolation for a hard startup deadline. Unattended startup acceptance remains open;
the observed 180-second timeout is not a guaranteed limit.

Captured-output acceptance is pending. Recording files and full timing data
remain outside the repository. `mapping_proven` remains false until measured
audio and player-clock evidence establish the required behavior across startup,
sliding windows, discontinuities, and playback controls. Captured process audio
does not establish sound at physical speakers.

## Package isolation

The package build and package checker passed. The resulting add-on ZIP is
unchanged from the prior tested artifact, SHA-256:

`df12f861f174d7132a62efe05649c9f27a69b130cce87d0ec7b977af7a06f227`

The native helper, generated recordings, and Xcode build products are outside
the add-on's explicit packaging allowlist.
