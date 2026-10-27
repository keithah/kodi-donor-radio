# Kodi audio capture helper implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver the user-requested local Mac app that records Kodi output and timing evidence for the controlled-ad acceptance work.

**Architecture:** A development-only SwiftUI app uses a Core Audio process tap to select a running Kodi process by PID and record its audio to a lossless local file. A bounded audio handoff feeds a serial writer that records sample counts and timestamp observations beside the audio; a bounded recording session owns a temporary keep-awake assertion. The helper is outside the Python addon package.

**Tech Stack:** macOS 14.2+, Swift, SwiftUI, Core Audio process taps, AVFoundation, IOKit; XcodeBuildMCP for scaffolding, builds and tests.

**Spec:** `docs/superpowers/specs/2026-10-22-vast-controlled-ads-design.md` (independent output evidence), plus the user's request for a local recording/keep-awake app.

## Global constraints

- Record only the explicitly selected running `org.xbmc.kodi` process; PID identity must be rechecked before capture starts.
- Save audio and timing evidence locally under a new recording directory; never overwrite an earlier recording.
- Default to a 30-second recording; permit a bounded 1–1200-second duration.
- Keep display/system idle sleep prevented only during an active capture; release assertions on every stop/failure path.
- Honor macOS recording permission. The helper cannot unlock a manually locked session.
- Persist no screen/video samples. Do not record microphone input.
- Request system-audio-only permission. The initial ScreenCaptureKit build was replaced after its application-enumeration step required broader screen-recording access.
- Preserve the tap's native audio sample rate and layout; do not change the user's output device settings. Keep sample/host timestamp validity flags and fail explicitly on missing timing, queue overflow, or unsupported format.
- Retain `mapping_proven=false` until output evidence independently establishes the mapping across the required live-window and discontinuity cases.
- Do not ship native binaries, recordings, build products, or developer helper sources in the Kodi addon ZIP.

## Review focus

- Multiple Kodi builds running: select and validate the exact PID, with a readable application path in the UI.
- Permission denial, target exit, or zero audio: produce an explicit failure rather than a successful empty recording.
- Audio buffer layout or format changes: copy valid PCM safely and stop on unsupported changes.
- Stop racing with an audio callback: flush/close in serial order and release both capture and keep-awake resources.
- Timestamp gaps: preserve original buffer PTS and frame offsets; do not manufacture a continuous presentation clock.

### Task 1: Native capture app

**Files:** create `tools/KodiAudioCapture/` with the XcodeBuildMCP macOS template; implement capture, PCM conversion and session metadata inside its Swift package feature target; update its app entry and configuration.

- [x] Scaffold a macOS app with bundle ID `org.kodi.donorradio.AudioCapture` and deployment target 14.2.
- [x] Implement process selection, duration/output selection, start/stop and recording playback/open actions. Support explicit launch arguments for repeatable fixture capture.
- [x] Implement an exact-PID Core Audio process tap and a serial lossless writer with buffer-frame offsets and original sample/host-time observations.
- [x] Cover PCM copying, argument bounds, and recording-summary failure cases with focused tests.
- [x] Build and test using XcodeBuildMCP; inspect the actual app UI.

### Task 2: Controlled output acceptance

**Files:** add usage instructions under `tools/KodiAudioCapture/README.md`; add only a sanitized measured summary under `docs/superpowers/reports/` when available.

- [ ] Run the helper against the isolated Kodi master process and a controlled fixture.
- [ ] Confirm a nonempty decodable lossless recording and compare programme/ad tone sections with the fixture reference.
- [ ] Keep the capture and full timing data outside the repository; retain the measured result and limitations in documentation.
- [x] Treat missing permission/output, lock state, or uncertain timestamps as an explicit open gate. The first capture was silent and failed; after correcting built permission metadata, macOS consent remained pending. Captured-output acceptance is not complete.

### Task 3: Review and PR update

- [x] Run relevant Python packaging checks to establish that the helper remains outside the addon ZIP.
- [ ] Obtain independent review, CodeRabbit and an exact-commit Steward report; address verified findings.
- [ ] Commit/push the helper to existing PR #4 and update its evidence without claiming general-station ad tracking is ready.
