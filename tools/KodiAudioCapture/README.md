# Kodi Audio Capture

A development helper for recording the audio output of one running Kodi build. It saves lossless PCM audio with timing observations for Donor Radio's controlled playback tests. It is separate from the Kodi addon and is not included in addon packages.

Runs on macOS 14.2 or later; build with Xcode 26 or later for the macOS 26 SDK declarations used behind runtime availability checks. The helper uses Core Audio process taps, AVFoundation, and temporary IOKit power assertions. It has no external package dependencies.

## Build and tests

Run from the repository root using XcodeBuildMCP:

```sh
xcodebuildmcp macos build \
  --workspace-path tools/KodiAudioCapture/KodiAudioCapture.xcworkspace \
  --scheme KodiAudioCapture \
  --derived-data-path /tmp/kodi-audio-capture-derived-data

xcodebuildmcp swift-package test \
  --package-path tools/KodiAudioCapture/KodiAudioCapturePackage
```

The app is `/tmp/kodi-audio-capture-derived-data/Build/Products/Debug/KodiAudioCapture.app`. This local development build uses ad-hoc signing and is unsandboxed so an explicit output path works. macOS recording authorization still applies. Keep build products and recordings outside the repository.

An ad-hoc rebuild changes the app's signing identity and can invalidate an earlier macOS recording approval. For repeatable local acceptance, build the final app with an existing local Apple Development certificate through a build-only `CODE_SIGN_IDENTITY` override, then run it from a stable application path. Keep personal signing identities and teams out of the project configuration. Approve recording access for that final installed app; moving it or changing its signature may require fresh approval. Never treat a previous permission click as proof that a rebuilt binary can capture audio.

The unit suite includes a delayed native-start regression proving that cancellation prevents readiness and activation while cleanup remains deferred until the native call returns. It also checks argument validation, owned planar/interleaved PCM copies, bounded-ring overflow, native-rate frame accounting against the saved audio file, timestamp gaps and validity, explicit format-change failures, readiness, and empty/interrupted recording failures. The separate UI smoke test launches the helper without beginning capture; run it only when desktop testing is intended.

## Record a test

1. Start the Kodi build to test.
2. Open Kodi Audio Capture, refresh its process list, and select the exact PID. The picker and selected-process detail show the application's path so concurrent Kodi builds can be distinguished.
3. Choose a recording duration and output folder, then start recording. The default is 30 seconds; the allowed range is 1–1200 seconds.
4. Start the test stream once the helper says it is recording.
5. Stop manually or wait for the duration limit. Open the recording folder or play the saved audio using the helper's controls.

The helper creates a private Core Audio tap for the selected Kodi process. It neither captures video nor enables microphone input, and it preserves the tap's native sample rate without changing the Mac's output-device settings. It prevents display and system idle sleep only during capture, releases those assertions on stop/failure, and cannot unlock a manually locked Mac.

macOS asks for **System Audio Recording Only** access when the process tap starts. If capture is denied or entirely silent, use **Recording access settings…** and review the helper in that section under **Privacy & Security → Screen & System Audio Recording**. macOS may require authentication or a helper restart. The helper does not request screen capture permission or modify privacy settings. Permission denial can produce silence, so a recording containing only zero-valued samples is failed explicitly.

Permission-gated startup has a known cancellation limitation: Core Audio's synchronous start call can wait before recording becomes ready. The duration limit covers active recording, not this startup wait. Cancel setup and normal Quit acknowledge cancellation immediately and wait for macOS to return. A cancelled startup cannot publish recording readiness or activate recording/keep-awake assertions afterward. Teardown still waits for the native call to return; the helper does not impose a startup deadline or free callback storage while Core Audio may still use it. One observed macOS 26.6.2 attempt returned a timeout after 180 seconds with zero frames; that observation is not a guaranteed upper bound. Establish recording permission interactively before automated runs, and have automation enforce its own readiness deadline and treat an absent final manifest as failure. Unattended startup acceptance remains open.

## Repeatable fixture capture

The app accepts these launch arguments:

```text
--pid N
--duration SECONDS
--output-root /absolute/path
--auto-record
--exit-after-recording
```

`--auto-record` requires a PID. `--exit-after-recording` requires automatic recording. The target is checked by bundle identifier (`org.xbmc.kodi`), PID, executable path, bundle path, and launch time immediately before capture. A stale PID fails rather than selecting another process.

For example, launch the built app with XcodeBuildMCP, substituting the current Kodi PID and output location:

```sh
xcodebuildmcp macos launch --json '{"appPath":"/tmp/kodi-audio-capture-derived-data/Build/Products/Debug/KodiAudioCapture.app","launchArgs":["--pid","12345","--duration","30","--output-root","/tmp/kodi-audio-evidence","--auto-record","--exit-after-recording"]}'
```

Each run creates a new `Kodi-<timestamp>-<UUID>` folder. A harness should watch its `capture.json`: the status starts as `preparing` and becomes `recording` only after the private audio device starts and the serial audio writer is armed. `capture_started_host_ns` records that readiness observation. Start fixture playback after that transition; do not wait for the first audio buffer, because an idle target may produce none. Final status is `completed` or `failed`. A recording with no audio frames, or with only zero-valued samples, is always failed. With `--auto-record --exit-after-recording`, the process exits 0 only after a successful finalized capture and exits nonzero on argument, target, or capture failure. Failures before the output folder is created can have no manifest; automation must check the process exit status as well as any final manifest. Without `--exit-after-recording`, errors remain visible in the helper window. App termination alone is not a successful capture.

## Evidence files

- `audio.caf`: lossless stereo Float32 PCM at the tap's native sample rate. It appears after the first valid audio buffer. Final frame count is verified by reopening the saved file.
- `timing.jsonl`: one `audio_buffer` observation per written buffer, including file frame offset/count, raw input/callback `AudioTimeStamp` host and sample fields with validity flags, converted input host timestamp, separate callback arrival time, and gap/overlap information. Explicit capture/format issues are also retained here.
- `capture.json`: target identity, requested duration, format, counters, readiness/final times, stop reason, failure detail, and `mapping_proven: false`.

The native HAL callback performs bounded validation and copies into a preallocated 32-slot single-producer ring. It does no disk I/O, JSON encoding, heap allocation, or queue waiting. A separate serial worker drains owned samples to disk. Oversized buffers, pool overflow, invalid timestamps, device loss, or native format changes stop capture and produce explicit failed evidence. Stop removes the callback, drains accepted buffers, and destroys the private aggregate and tap.

Input timestamps describe the tap's first acquired frame. Their `AudioTimeStamp.mHostTime` values use Mach absolute time and are converted to host nanoseconds with CoreMedia. Fractional sample positions and validity flags remain in the sidecar. Callback arrival time is separate; it includes scheduling latency and is not the input sample time. These timestamps establish capture acquisition timing, not physical-speaker presentation timing.

The audio file concatenates the delivered frames. Timing gaps and overlaps remain explicit in the sidecar; they are not filled with invented samples or treated as an uninterrupted presentation clock. A completed recording establishes a saved capture, not a proven Kodi/VAST clock mapping or sound at a physical speaker. Waveform comparison against known fixture boundaries and the required live/discontinuity cases remain separate acceptance checks.
