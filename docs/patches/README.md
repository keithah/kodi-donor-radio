# FFmpegDirect realtime override experiment

`inputstream.ffmpegdirect-22.2.10-realtime-override.patch` targets
[inputstream.ffmpegdirect 22.2.10-Piers](https://github.com/xbmc/inputstream.ffmpegdirect/tree/70c378da893cdcfb031b54f84811c17c14b4fb2d),
base commit `70c378da893cdcfb031b54f84811c17c14b4fb2d`.
It changes the inputstream addon only. It contains no Kodi core change or native
binary, and the radio addon does not install or require this private build.

`inputstream.ffmpegdirect-22.2.10-timeshift-startup-eof.patch` applies after
the realtime override. It keeps the first timeshift packet seekable, waits
briefly for buffered packets before reporting EOF, and only treats an upstream
EOF as terminal when `inputstream.ffmpegdirect.terminal_eof=true` is explicitly
provided. This property is forwarded through `addon.xml.in` and defaults off,
so this change does not turn transient live-HLS reload EOFs into terminal EOF.
Only the isolated diagnostic entry currently sets it for finite controlled
fixtures; the shipped radio addon does not set it.

Official FFmpegDirect 22.2.10 reports every timeshift stream as realtime, even
when `inputstream.ffmpegdirect.is_realtime_stream=false` is supplied. Kodi's
VideoPlayer therefore applies its realtime startup clock policy. The patch
preserves realtime as the omitted timeshift default and honors explicit true
or false. Omitted regular/catchup properties become deterministically false;
the previous property storage was uninitialized. The optional override resets
on every open.

The three production changes are in `Properties.h`, `StreamManager.cpp`, and
`TimeshiftStream.cpp`. A standalone C++ test covers all nine combinations of
stream mode and omitted/true/false override, and runs in the existing GCC/Clang
CI matrix. Captured playback must additionally verify the actual VideoPlayer
path; selecting an inputstream alone does not force it instead of PAPlayer.

Apply with `git apply --check` followed by `git apply` from a clean checkout of
the exact base commit. The patch includes its tests; run them independently of
Kodi with:

```sh
cmake -S tests -B build-tests
cmake --build build-tests
ctest --test-dir build-tests --output-on-failure
```

The local native build used the existing Kodi addon SDK (inputstream API 3.4.1,
minimum 3.4.0), FFmpeg 9.0.2, macOS 26.5 SDK, arm64, and CMake's Xcode generator.
XcodeBuildMCP compiled both stock and patched libraries successfully. Use the
[upstream macOS build instructions](https://github.com/xbmc/inputstream.ffmpegdirect/tree/70c378da893cdcfb031b54f84811c17c14b4fb2d#mac-osx)
with a matching Kodi SDK/dependency installation to reproduce a native build.

The controlled continuous AAC clock remains diagnostic in the radio addon.
Neither this patch nor the unit test establishes billable playback evidence;
startup, extended-window, pause/seek, and starvation capture acceptance is a
separate gate. Stock FFmpegDirect retains the startup limitation until an
upstream fix is accepted and released.

## Owned AAC renderer lab patch (LAB ONLY)

`inputstream.ffmpegdirect-22.2.10-owned-aac-lab.patch` applies after the
realtime-override and timeshift-startup-eof patches above. It is a preserved
diagnostic artifact, not production activation: it has no Kodi core changes,
is not installed by the radio add-on, and requires the exact loopback fixture
URL plus two explicit local marker files. The path accepts controlled AAC-LC
48 kHz stereo only, decodes the actual AAC packets into a bounded 64-second
PCM ring, and uses the Python monitor to exercise Kodi's normal pause/resume;
it does not read a predecoded PCM file. The matching diagnostic scripts and operating limits are preserved in
`owned-aac-lab/`. The exact source snapshot and hash manifest remain alongside
the local capture evidence in `owned-renderer/decoder-source-final-a04d/`.
Playback remains a lab experiment and does not imply production readiness.

## Owned HLS continuation (LAB ONLY)

`inputstream.ffmpegdirect-22.2.10-owned-hls-lab.patch` is an alternative to the
owned-AAC patch, applied after the same realtime and startup/EOF prerequisites.
It extends the diagnostic to the exact controlled loopback HLS URL. Source-only
fixture and capture helpers are in `owned-hls-lab/`. The first 180-second HLS
capture failed audible continuity despite complete frame counts; the plan
`docs/superpowers/plans/2026-10-27-owned-hls-clock.md` records the investigation.
Do not treat these diagnostic files as a released playback path.

`ffmpeg-9.0.2-hls-recovery.patch` applies to FFmpeg 9.0.2 `libavformat/hls.c`.
It permits an existing reader to recover from an exhausted playlist-hold limit
when the exact next segment becomes available, preserving the input state until
that segment opens. It was built into a private static archive for the inputstream
addon; shared libraries and Kodi core were unchanged. The source-only regression
harness is in `owned-hls-lab/ffmpeg-recovery/`. This fix does not recover media
that has expired from an upstream playlist, nor establish audible clock accuracy.
