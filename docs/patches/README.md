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

The owned-clock, owned-HAL, and later owned-HLS lab patches are macOS-only
experiments. Their native transport rejects non-Apple targets, so applying
those patches to the upstream addon intentionally fails non-Apple CI builds;
they are not cross-platform inputstream patches.

The controlled owned-clock lab also has an explicit `videoplayer_pcm_v1`
experiment. Launch with `owned_beacons=1`, the existing exact fixture path, and
`player_mode=videoplayer_pcm_v1`; omission retains `paplayer_pcm_v1`. The launcher
creates a fresh bound session, then requests `Player.Open` with named
`VideoPlayer` and `resume:false` through a one-use `owned_resolve` plugin URL.
Both new native properties, `owned_player_mode` and `owned_player_core`, are
required by that mode. Its initial real raw AAC packet must match the passed
SHA256, stream and PTS0 before the same packet is decoded once; no missing-probe
fallback or new seek exception exists. The PCM host stays 3840 frames at 48 kHz.

Hello and ownership observations echo `player_mode`/`requested_core`. VideoPlayer
observations additionally bind a pinned actual current demux caller, identity
readback statuses, first observation time, arming state and generations around
the clock query. `host_override_applicable:false` means the PAPlayer scheduling
experiment is inapplicable, with zero tokens, not that a substitute override
succeeded. Renderer QoS, source proof, output guard, ownership, callback/native
speed fences, freshness and the frozen estimator remain required. Archived
rows without the mode fields retain their PAPlayer interpretation only.

Use repository `owned-hls-lab/observe-hls.py` with `--owned-fixture <fixture>` and
`--player-mode videoplayer_pcm_v1` plus the existing output/port/duration/action
arguments. It verifies one audio player and exact resolved `dynpath` before
controls and observations; audio ID0 does not identify the playback core. It
retains the four-pause schedule and records any forced stop. The launcher records
`videoplayer.queuetimesize` without changing it. Actual VideoPlayer thread/log
agreement, native late-pause ordering, natural EOF/tail, no gaps/replay, and all
clock/beacon gates require fresh captured qualification. This mode remains an
experiment, with production paths disabled.


The fixture-only `videoplayer_video_v1` experiment uses the same explicit
VideoPlayer request and original retained AAC source proof, but projects exactly
one VIDEO stream and no host audio: rawvideo I420,16×16,10fps,384-byte limited-range
black frames. Source AAC metadata and decoding remain separate. Select it with
`--owned-fixture <fixture> --player-mode videoplayer_video_v1`; the observer now
requires one actual video player and exact resolved dynpath, using its returned
ID for the unchanged four normal pause cycles. This is diagnostic presentation,
not a finished radio GUI or an enabled production route.

This mode requires observed `videoplayer.queuetimesize=40`, bound as integer
`video_queue_tenths` through launch/monitor-ready/hello/ownership and native
`owned_player_queue_tenths=40`. No settings are changed. Frames begin at PTS0 and
advance 100 ms; issuance is capped at `floor(accepted_adddata_frames/4800)+60`.
At the cap, empty non-EOF retries keep cancellation responsive. The six-second
allowance covers the four-second core queue, up to six render buffers and startup
margin; it is neither required prefill nor audible headroom. A 30 s Mach deadline
from native Open requires acknowledgement of the real onAVStarted play command.

At first owned-drained, tracking remains retired and generation freezes. The
owned CAEStream stays alive for an explicit 1 s Mach hold with empty retries, then
normal EOF; cancel/error bypass it. This empirical safeguard covers the prior
~284ms drain-to-audible-tail observation but proves no sink latency. Video PTS is
never an audio clock. `host_representation.stream_type=video` identifies video
frame counts and separately records Open/start ACK/final frame/drain/hold/EOF
Mach times. Legacy host AAC counters remain zero; actual AAC source/decoded and
accepted audio counters retain their meanings. Host terminal telemetry is a
separate non-clock row. Downstream queued tail remains unknown.

Actual codec/pixel format, absence of a host audio stream, real startup/control
callbacks, full captured prefix/tail, natural EOF, no extra gaps/replay and all
clock/beacon gates require two fresh captures. Ordinary video presentation may
negotiate refresh/fullscreen under the user's existing settings; output guards
remain active. No display policy changes or fresh qualification are implied.


Run11 exposed a separate core demux readiness requirement: rawvideo packets with
empty extradata are discarded before reaching the opened decoder. The corrected
video projection supplies an SDK-owned one-byte zero compatibility marker;
FFmpeg 9.0.2 rawvideo ignores it. Original AAC metadata remains independent.
`video_extradata_hex:"00"` now binds the new-mode launch/readiness/hello/ownership
and host representation; missing or wrong evidence retires qualification. Legacy
modes keep null/absent marker. This adds no budget, recovery-point workaround,
callback or clock exception. `check-video-host-startup.py` compiles the exact
external Kodi readiness methods and decodes the generated frame using the matching
FFmpeg 9.0.2 Kodi libraries; actual Kodi startup and captured audio remain gates.
