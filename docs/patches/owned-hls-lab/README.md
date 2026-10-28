# Controlled HLS renderer experiment

These helpers reproduce an isolated diagnostic, not a released radio playback
path. The accompanying owned-HLS patch replaces the owned-AAC lab patch;
apply the realtime override and startup/EOF patches first. Kodi core is not
modified. Fixed local paths deliberately restrict the experiment to one test
profile and one monitor session.

Generate the unique 180-second AAC/TS fixture with `generate_fixture.py`
(developer dependencies: FFmpeg and this repository), then run
`serve_fixture.py`. The loopback origin initially publishes one segment,
grows to six, retires old playlist entries, and finally publishes ENDLIST.
The first media GET starts its clock. Keep generated metadata, packet hashes,
requests, captured audio, timing JSONL and the native ledger together.
`test_fixture.py` checks publication timing without playing audio.

The diagnostic entry point uses Kodi's ordinary pause action through the
existing callback monitor. The native renderer preserves source ordering in
a bounded 64-second PCM ring. Telemetry is bounded to 70,000 rows and currently
fails the diagnostic when exhausted; this is not suitable for indefinite use.
The 180-second experiment exercises ring wraparound and pauses beyond the
visible playlist window. MPEG-TS packets in the first capture were ADTS;
the raw-AAC branch has not been qualified by that capture.

Do not infer audible continuity or clock qualification from accepted-frame
counts or AudioEngine drain status. Compare the captured waveform independently
with the decoded served-media reference, using each capture buffer's host
timestamp. The proposed steady clock is accepted frames divided by sample rate
minus AudioEngine delay; its absolute error, startup, pause tail, resume and EOF
behavior remain subject to captured validation. Success beacons remain disabled.


The first waveform audit and the later partial-playback failure are preserved
in `docs/superpowers/reports/kodi-owned-hls-*-2026-10-27.json`. Run
`analyze_hls_capture.py` with a completed capture, the served-media reference,
and its archived native ledger. It derives source anchors by searching the
entire reference, then checks a fixed held-out clock grid using actual capture
buffer timestamps. The companion `control_context.py` annotates pause actions.
An audio capture with no timestamp discontinuities can still contain source
silence, omissions, or a stalled renderer; those outcomes fail acceptance.

Monitor startup clears a stale stop sentinel and rotates the previous callback
ledger before writing fresh readiness. Start only after the previous monitor
has exited; the helpers still require one monitor session at a time.

Use `clock-candidate-v2/estimator.py` for current analysis. V1 is retained as
the immutable numerical reference used before the held-out recordings; V2 adds
invalid-input handling with exact finite-input equivalence.

## Captured result and remaining gates

The buffered-profile recording preserved all 180.032 seconds through four normal
Kodi pauses, including a pause beyond the visible live window, and reached natural
EOF. Independent per-channel waveform analysis detected no extra gaps or repeated
source blocks. This single controlled run used `audiooutput.lowlatency=false`;
the original isolated-profile setting was restored to `true` afterward. The
low-latency run still had two 512-frame output interruptions and is not accepted.

The frozen offline V2 estimator qualified all 130 held-out steady measurements
with unfitted error from -7.1725 to +0.4271 ms. The raw delay-derived clock failed
two measurements. Neither result qualifies acquisition, pause tails, draining,
physical speaker timing, arbitrary HLS sources, or production success beacons.
Pause-to-quiet took 661–731 ms. See the buffered-capture report under
`docs/superpowers/reports/`. This remains a diagnostic patch stack, not an enabled
production playback adapter.

Post-capture review also hardened the two post-decode teardown fallback branches
to free the source packet and stop rather than hand original programme data to
the host. The archived capture binary predates this defensive change; its hash
and the captured-result scope remain unchanged.

## Startup metadata ownership

A post-review capture exposed an initialization race: Timeshift started its
background reader before the lab renderer copied the stream table and codec
parameters. The reader could rebuild that table during the copy. The current
patch defers the reader only for the exact opted-in HLS lab route until metadata
and decoder initialization are complete. Pre-play callbacks use the copied
metadata immediately; the normal buffer seek-to-zero remains available. Thread
launch failures return through cleanup. Ordinary Timeshift startup is unchanged.
The failed attempt contains no programme audio and its stale shared telemetry
was explicitly excluded; it is not a clock or playback pass.

## Raw AAC in fragmented MP4

`generate_fmp4_fixture.py --source-root /path/to/validated-ts-fixture --root
/path/to/new-fmp4-fixture` copy-muxes the existing AAC access units without
re-encoding. Run it with developer FFmpeg/FFprobe installed. It refuses a
nonempty output directory and publishes staged assets only after checking every
fragment's packet hashes, PTS/DTS/duration, absence of trimming metadata and
byte-identical decoded PCM. Source and output paths are diagnostic data; these
tools are not installed in the radio addon.

Serve the generated root with `serve_fixture.py --root /path/to/new-fmp4-fixture`.
The exact loopback URL remains unchanged for the native lab opt-in. The server
emits a fixed EXT-X-MAP for init.mp4, serves its byte ranges, and retains the
same sample-based rolling window. Initialization requests do not start the
playlist clock. See RFC 8216 sections 3.3 and 4.3.2.5 for initialization-map framing.

Native telemetry must report `raw_aac` for this test; file extensions alone do
not establish decoder-path coverage. General fMP4 edit lists, skip samples,
changing initialization segments and codec changes remain outside qualification.
The frozen estimator and native binary are unchanged for this format comparison.

The final raw-AAC retry used the unchanged reviewed native binary and buffered
profile. It preserved the complete source through four normal pauses and natural
EOF, with no detected extra gaps or replay; all 132 qualified offline-clock
measurements passed. The first attempt's recording stopped early on a transient
identity-validation failure while Kodi remained alive and is not a full pass.
See the raw-AAC capture report under `docs/superpowers/reports/` for identities,
measurements and limits. No runtime mapper or production tracking is enabled.

## Bounded runtime clock diagnostic

Install `monitor.py`, its sibling `runtime_clock.py`, and the byte-identical
`clock-candidate-v2/estimator.py` under the fixed owned-renderer directory. The
loader verifies frozen SHA256 `7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87`.
Start the monitor before launching the exact loopback route. Archive or remove
the previous native ledger before monitor launch: a nonempty preexisting ledger
fails closed. A new native file created between monitor readiness and AVStarted
is accepted, but pre-callback observations cannot acquire eligibility.

The 20 ms loop pins the fresh ledger's device/inode, consumes all complete rows
without sampling, and rejects replacement, truncation, malformed JSON, lines
above 16 KiB or unread batches above 256 KiB. Its separate runtime ledger is
bounded to 80,000 records. Original native evidence is untouched; callback and
runtime ledgers rotate on monitor startup. Partial final lines wait for newline.
The reader's line number counts every native row, including non-clock events.
Each runtime input records that identity, SHA256 of the exact native line,
full native input, query/receive/evaluation timestamps, frozen estimate, and
wrapper validity/reasons. Snapshot transition records expose callback and
freshness invalidation. Compare these written estimates against captured audio;
replaying the estimator afterward does not establish runtime performance.

Query age must be between zero and 100 ms at receipt and evaluation in the macOS Mach nanosecond domain
read explicitly with `mach_absolute_time` and `mach_timebase_info` through ctypes.
Python monotonic clock origins vary by interpreter and are not assumed equal.
The callback ledger retains its legacy `monotonic_ns` key but records explicit
Mach host nanoseconds. No snapshot
extrapolation occurs. Pause revokes synchronously; resume clears acquisition and
requires nine observations after its callback with native play/resume
state. Stale rows remain in the evidence and reach the frozen estimator, but
clear acquisition before a later row can qualify. Epoch/counter changes, route
mismatch, stop, seek, non-unit speed and native failure/drain retire the launch.

This bridge does not authenticate native incarnation, native control revision,
output device generation, or underlying sink-statistic age. Local line identity
is reader evidence only. It does not prove physical speaker timing or production
transport ownership. Production success beacons remain disabled.

Telemetry parsing and disk output run outside the session lock. Callback control
publication precedes queued callback/runtime logging. The output writer checks
session generation and freshness immediately before writing; superseded rows
retain `evaluated_valid` but become invalid. `publication_checked_ns` identifies
the exact state-check time. These records describe historical observations;
reading a valid JSONL record later never grants current eligibility. Only the
locked session snapshot supplies current state. Disk writes may complete after
a later callback, which does not alter their recorded check timestamp.

Before diagnostic playback, install the matching ffmpegdirect manifest as well
as its native binary. Its `kodi.inputstream` extension must advertise both
`terminal_eof` and `is_realtime_stream` in `listitemprops`, and its required
imports must include `kodi.binary.global.audioengine` (the validated local
installation uses version 1.1.1). The diagnostic launcher reads the installed
addon's actual `addon.xml` and refuses to start the monitor or playback when
these declarations are missing or unreadable. Restoring an original manifest
without the startup/EOF patch can silently discard the terminal EOF property
and leave the renderer retrying EOF. The preflight checks manifest declarations;
it does not replace native-binary identity validation.

### Runtime capture status

The live session bridge has now been exercised with recorded output estimates,
not just offline replay. See
`../../superpowers/reports/kodi-owned-hls-runtime-clock-progress-2026-10-28.json`.
The first run passed 140 clock points but missed natural EOF because the test
manifest did not forward terminal_eof. The corrected run passed 129 points and
natural EOF, but recorded one unrequested 512-frame Darwin sink underrun.
Neither run is complete no-gap release acceptance. Production beacons remain
disabled, and authenticated native session ownership is still future work.

The external capture observer accepts `--poll-interval` from 0.1 to 5 seconds
(default 0.1). Use `--poll-interval 1.0` to reduce routine status observation
to roughly 1 Hz while investigating instrumentation overhead. Pause scheduling
still checks wall deadlines every 100 ms and keeps all four original pause
cycles; it makes no action-status calls before an action's deadline. This is a
bounded instrumentation hypothesis, not evidence that polling caused a captured
audio gap or that reducing it fixes continuity. Runtime native-clock acquisition
remains unchanged.
