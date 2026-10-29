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

The subsequent 1 Hz polling experiment also failed no-gap acceptance: the full
repeat reached natural EOF but contained six unrequested quiet intervals.
152 of 154 independent audio points had valid runtime estimates within the
threshold; the other two were explicitly invalidated during pause callbacks.
The preceding incomplete recorder attempt is retained separately. No competing
jobs were stopped, and reduced polling is not claimed to fix sink starvation.

### Bounded native stall measurements

Apply `../inputstream.ffmpegdirect-22.2.10-owned-hls-stall-diagnostics.patch`
after the owned-HLS patch for the next diagnostic build. It changes only the
opted-in addon measurements. Original audio feeding, pause controls, 5 ms sleep,
telemetry flush cadence and clock calculations remain in place. QoS is observed,
not changed. Record the new addon hash separately from earlier recordings.

Each clock row adds fixed per-operation counts and maximum Mach-time brackets,
plus at most 12 completed operations longer than 40 ms. Measurements cover
shared mutex waits, decoding under the shared mutex, telemetry writes/flushes,
control-file reads, AddData/GetSpace, renderer iterations and sleep overshoot,
and synthetic host-packet calls/interarrival. Durations overlap; never sum them.
The aggregator uses nonwaiting try-locks, fixed storage and explicit contention
and event-overflow counters. JSON is formatted outside its lock. Worst-case
extra JSON is below 10 KiB; the full existing row limit remains 16 KiB.

The current row's own write appears in a later snapshot. Final pending operations
may never be published. Unavailable snapshots or loss counters make absence of
an event inconclusive. A long measured operation includes any time its thread
was descheduled: overlap with a captured gap identifies a candidate phase, not
a proven syscall, lock owner, scheduler mechanism or competing process.
Instrumentation itself adds clock calls and output bytes and may affect timing.

After applying the patch to the native addon source, the standalone aggregator
check can be compiled with a C++14 compiler, `-pthread` and `-I/path/to/addon/src`
using `test_stall_diagnostics.cpp`. It checks exact maximum brackets, snapshot
reset, the strict duration threshold, bounded event loss and worst-case JSON
size. It does not replace the native build or captured playback acceptance.

`summarize_stalls.py` reads an archived ledger with `--ledger`, the capture
manifest's first Mach timestamp with `--capture-first-host-ns`, and optional
`--gap START_SECONDS END_SECONDS` pairs. Write its JSON with `--output`. It
validates the complete diagnostic schema and retains unavailable/lost evidence.
Gap overlaps use capture host time directly; downstream pipeline latency means
a contributing stall can precede a captured gap, so zero overlap is not exclusion.
The summary reports observations only and cannot declare a playback pass.

The separate `../inputstream.ffmpegdirect-22.2.10-owned-hls-renderer-qos.patch`
applies after the stall-diagnostic patch. On macOS it requests
`QOS_CLASS_USER_INITIATED` only from the dedicated owned-HLS renderer thread,
then records the setter result and readback. It does not change another thread,
the process policy, Kodi core, or another job. A failed request leaves playback
running and records `renderer_qos_request_confirmed=false`; a confirmed request
is an experiment precondition, never proof of seamless output. The request ends
with that thread. Compare captured output and operation brackets against the
diagnostic baseline, retaining both results. The original shared-lock and file
I/O behavior remains; this change is not a real-time scheduling guarantee.

### Recorded scheduling comparison

The instrumented default-QoS baseline still captured one extra 512-frame output
interruption. Immediately beforehand, a requested 5 ms feeder sleep took
55.846 ms. All 33 diagnostic snapshots in the gap neighborhood (capture
193.9–194.6 seconds) were available with no contention or long-event loss.
The full baseline had 36 contention drops and one unavailable snapshot outside
that window. Measured work stages in the neighborhood did not explain the
sleep overshoot, but the owned AE cache remained populated and silent host
delivery also stalled. This identifies a scheduling-related interval without
proving that the addon feeder alone caused the sink underrun.

Two consecutive recordings with the same feeder-QoS addon binary then passed
the controlled repeated-capture gate: the complete 180.032-second source, four
normal pause/resume cycles, natural EOF, and no detected extra quiet intervals
or skipped/repeated source blocks at the tested resolution. Independent waveform
checks included the prefix, tail, every pause seam and dense overlapping windows
in both channels. All 125 and 126 independently qualified recorded runtime-clock
points passed the unchanged 21.333 ms tolerance, spanning -7.868 to +1.666 ms.
Maximum feeder sleep overshoot was 19.476 and 21.711 ms versus 50.846 ms in the
baseline. The independent audit confirmed native/runtime row identity and actual
captured buffer timestamps; estimator replay is not the runtime evidence.

See `../../superpowers/reports/kodi-owned-hls-stall-investigation-2026-10-28.json`
for identities and archives. Kodi core and the signed recorder were unchanged;
other jobs remained running. The original profile and installed addon files were
restored. These results clear the controlled candidate's repeated-capture check,
not arbitrary-load reliability or general-station/production mapping. Previous
failed recordings remain preserved. Production beacons remain disabled for this
path; authenticated native ownership and broader adapter qualification remain
work for a later implementation.

Post-capture review added a compile-time diagnostic name-table size assertion
and retained the QoS setter/confirmation fields in the offline summary. The
recorded binaries predate that compile-time guard; their runtime source is
otherwise identical. The guard and both additive patch applications passed the
standalone C++ check. No audio behavior or estimator changed after capture.

### Native session-owned clock protocol v1

Apply `../inputstream.ffmpegdirect-22.2.10-owned-clock-session.patch` after the
realtime, startup/EOF, owned-HLS, stall-diagnostic and renderer-QoS patches.
This is an additive macOS fixture patch. The three registered ListItem properties
are `inputstream.ffmpegdirect.owned_clock_nonce` (32 lowercase hex),
`inputstream.ffmpegdirect.owned_clock_timeline` (64 lowercase hex), and
`inputstream.ffmpegdirect.owned_clock_first_packet` (64 lowercase hex).
The only qualifying URL is exactly `http://127.0.0.1:18789/live/index.m3u8`, with
the existing two lab opt-in markers. Missing properties retain legacy lab mode;
legacy mode never reports owned eligibility. Present but malformed properties do
not fall back to the shared control file.

The Python launcher must create a fresh mode-0700 session directory at
`/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer/clock-sessions/<nonce>`.
Native validates directory ownership, permissions and every path component with
no-follow opens, then pins the directory. Native creates `native.jsonl`
exclusively, and publishes a complete mode-0600 `hello.json` last using an
exclusive temporary file and `renameatx_np(RENAME_EXCL)`. Existing files are
never overwritten. The hello has protocol, nonce, independently random native
incarnation, timeline and first-packet digests, native PID, native Open Mach
nanoseconds, stream generation 1, and `qualification_scope=controlled_fixture`.
Hello replacement or mutation, session-directory replacement, and invalid or
changed command files retire eligibility. These checks prevent accidental
cross-run contamination; they are not a security boundary against same-user code.

Commands are immutable mode-0600 files `command-000001.txt` through
`command-000064.txt`. Stage them under a hidden temporary name that does not
start with `command-`, then publish atomically without overwriting. Each file
contains exactly one canonical ASCII line, at most 512 bytes:

```text
v1 <nonce> <incarnation> <revision> <state> <issued_mach_ns>\n
```

Here `\n` denotes the single terminating newline. Revision is an unpadded decimal
integer from 1 through 64. States are `waiting`, `play`, `pause`, `resume`, `stop`.
Revision 1 must be `waiting` with the Python launch timestamp, no later than
native Open. Later timestamps must be strictly increasing, at least native Open,
and no later than application. Native applies exactly the next revision; identical
repeated records are idempotent, while conflicts, gaps, malformed records and
bounds violations retire eligibility. Each renderer iteration drains at most
eight commands and publishes an acknowledgment for every applied transition.
The source reader sees the same applied pause gate through an atomic value.
Acknowledgment means the feeder gate was applied; it does not mean immediate
audible pause. Ownership failure preserves the source queue and normal audio
failure behavior; subsequent valid sequential controls can still apply. A damaged
acknowledged command or immutable hello permanently retires clock evidence but
does not suppress individually validated next controls from the unchanged pinned
session. A valid pending prefix is applied before a malformed future revision;
malformed or gapped unacknowledged revisions remain barriers and cannot be
skipped or repaired by rewriting files.

Every native clock row retains the frozen estimator inputs and adds `ownership`:
protocol, nonce, incarnation, timeline_sha256, strictly increasing
observation_sequence, control_revision, control_state, control_issued_mach_ns,
control_applied_mach_ns, state_generation_before/after, stream_generation,
source_zero_confirmed, eligible, reason, output_guard_generation and
output_guard_valid. Before binding, revision is 0 and state is `unbound`.
Initial `output_guard_valid=false`, generation 1, and reason
`output_guard_unarmed` are expected and never qualify a clock. Source-zero proof
requires the first raw AAC payload digest, first PTS exactly zero and decoded
48 kHz stereo. ADTS retains legacy playback but cannot obtain this ownership
proof. The timeline digest binds identity; it does not validate every media byte.

The renderer installs CoreAudio default-output/device-list and pinned-device
alive/rate/output-stream listeners immediately before its first owned AddData,
after initial prefill and normal startup. Setup is checked by repeated readback
and generation comparison. Callbacks only atomically revoke shared state; copied
blocks preserve callback lifetime across listener removal, using CoreAudio's
existing global dispatch infrastructure. No new owned worker thread is created.
Sink format, 48 kHz rate, stream format and unit resample ratio are checked around
samples. Any failed check or observed change permanently retires this launch.
The parent launch preflight must establish Kodi's default output and the qualified
buffered profile. This is `fixture_default_device_observation`, not proof of
physical speaker timing or detection of same-format internal Kodi sink restarts.

Eligibility also requires confirmed renderer QoS, a verified source origin,
active play/resume, unchanged native/output generations and no terminal
revocation. Speed 0 invalidates immediately; normal speed plus a resume ack newer
than the latest speed-0 observation permits fresh acquisition, in either callback
order. Normal speed alone cannot revive an old resume. Active seek, flush,
reset, abort, abnormal speed, source discontinuity, native failure, drain and
close revoke before lifecycle work or joins. One exact initial Kodi probe sequence
is exempted: byte capability query `(0, 0x10)`, backwards demux seek `1.0 ms`,
then backwards demux seek `0.0 ms`. Each step requires zero accepted frames,
playback not started, decoding unarmed, no source-reader ownership transfer and
an initialized active fixture. Repeated, out-of-order, nonmatching or active
seeks retire eligibility. The historical byte-query return remains `-1`; changing
it to `0` would suppress Kodi's rewind and prevent the existing zero-seek decode
arming. Demux calls and successful zero-rewind arming are unchanged. Native logs
record the actual seek arguments and gate state for capture validation. Normal
speed initialization remains. A monitor restart requires a new nonce;
there is no reconnect, retained-ledger replay, or heartbeat qualification.

Compile `test_owned_clock_protocol.cpp` with a C++14 compiler, `-pthread`,
`-Wall -Wextra -Werror`, and `-I/path/to/patched-addon/src`; repeat with
`-DNDEBUG`. Its CHECK failures remain active in both builds. Tests cover strict
wire parsing, sequence/conflict/bounds, generation/revocation and speed ordering,
secure directory/file checks, immutable hello/commands, retained ledger collision,
file replacement/truncation and the eight-command drain bound. Run the existing
stall check against the same source too.

Build the existing `ffmpegdirect-patched-build/inputstream.ffmpegdirect.xcodeproj`
with XcodeBuildMCP, scheme `inputstream.ffmpegdirect`, Debug, arm64 and `-jobs 2`.
CoreAudio is linked through CMake. Kodi's AddonHelpers scans the existing
`kodi/AudioEngine.h` include to generate the required audioengine import; inspect
the generated manifest for exactly one required import and all three new
properties before installing it. Build and protocol tests are not playback
acceptance. Archive the new native source/binary hashes and retain earlier
failures. Production paths and production success beacons remain disabled.

## Opt-in owned fixture beacons

The diagnostic query remains `?diagnostic_owned_hls=1`. Owned tracking is a separate
lab-only launch, restricted to `http://127.0.0.1:18789/live/index.m3u8`. It uses the
existing VAST resolver, HTTP client, tracker and beacon worker with the complete
immutable Presentation. Six creatives are exact AAC access-unit subsets of the
precomposed fixture, exposed as copy-muxed TS behind small HLS playlists; this is
not a general insertion implementation.

Use a new copied fixture directory, preserving earlier assets and captures. The
locked acceptance contract is
`/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/acceptance-contract.json`
(SHA256 `0f61940179856249077e9536643c6c2868da68853b2406abf0024849de49f1f8`).
Generate once; existing `owned-beacons` outputs are never overwritten:

```sh
python3 docs/patches/owned-hls-lab/fixture_beacons.py \
  --root /private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture \
  --repo /Users/hermes/.codex/worktrees/radio-poc/kodi-donor-radio
python3 docs/patches/owned-hls-lab/serve_fixture.py \
  --root /private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture \
  --repo /Users/hermes/.codex/worktrees/radio-poc/kodi-donor-radio --owned-beacons
```

Generation verifies the pinned metadata, all 92 assets, original ADTS access
units, every actual served fMP4 packet through FFprobe, reference PCM, and all six
creative TS extractions. Launch repeats local byte checks, resolves actual VAST,
checks the server proof, and fetches the served creative HLS/TS. Served source and
creative assets are checked again on each HTTP request. `/stats` reports tracking
attempts separately from source requests. `beacon-receipts.jsonl` retains every
accepted, duplicate, unknown and HEAD tracking attempt with a Mach receipt time;
only the first valid GET is accepted. Unknown attempts are never deduplicated out
of evidence. Receipts use hashed session IDs, not raw launch capabilities. Logs
are bounded to 4096 completed tracking attempts; exhaustion is a failure, not a
clean gate. The receipt file is created exclusively. Keep the server running
across both captures: `/reset` resets the source window but retains receipts. To
restart, preserve the previous receipt file under a distinct evidence name first.

Parent-controlled installation layout:

- Replace the isolated addon `default.py` with this lab launcher.
- Keep the existing addon `resources/lib` tree, including `owned_clock.py`,
  `owned_ad_playback.py`, the reviewed `ad_tracking.py`, and VAST/HTTP dependencies.
- In `/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer`,
  place `monitor.py`, `fixture_beacons.py`, `runtime_clock.py` and the unchanged
  `clock-candidate-v2/estimator.py` in that relative subdirectory.
- The real `clock-sessions` parent must exist below that directory. Each launch
  creates a new nonce directory mode 0700 before native Open. Do not reuse one.
- Install the reviewed native binary/manifest containing all three ownership
  properties. Require default output (`Default` or the observed
  `DARWINOSX:default|Default`), lowlatency=false, channels=1, config=2,
  processquality=30, stereoupmix=false and passthrough=false. Native source and
  output checks establish rate; a failed samplerate RPC is not a rate proof.

Exact query (the fixture value may be URL-encoded):

```text
?owned_beacons=1&fixture=/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture
```

The launcher saves private `launch.json` and canonical `timeline.json`. The
monitor claims the session exclusively, starts callbacks, and publishes a
nonce/timeline/PID-specific `monitor-ready.json`. This only confirms monitor
startup. `owned_native_ready` is logged after the transport validates the actual
native hello; qualified samples require the subsequent native acknowledgments
and frozen estimator acquisition. A stopped monitor cannot rejoin its directory.
Callbacks only revoke/queue controls in memory using explicit Mach time. The
monitor polls every 20 ms, flushes commands, and observes every returned snapshot,
including invalid startup rows. `owned-events.jsonl` retains each ingested row
SHA/sequence, contemporaneous snapshot, callback, tracker decision and delivery
authorization. Native `native.jsonl` retains the original bytes. On stop, callbacks
cancel the worker without joining; cleanup joins outside callbacks.

After the parent's unchanged recorder/normal-pause driver completes, run the
existing whole-audio analyzer with the owned native ledger. For its optional
callback annotation, convert `owned_callback` records to `event` and
`monotonic_ns=issued_mach_ns`; this does not alter the native or owned logs.
Then run the new evaluator with the NumPy-enabled analysis runtime:

```sh
/Users/hermes/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  docs/patches/owned-hls-lab/evaluate_owned_beacons.py \
  --capture /absolute/path/to/capture \
  --reference-pcm /private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture/reference-from-fmp4.f32 \
  --analysis /absolute/path/to/capture/hls-audio-clock-analysis.json \
  --native /absolute/path/to/clock-sessions/NONCE/native.jsonl \
  --events /absolute/path/to/clock-sessions/NONCE/owned-events.jsonl \
  --receipts /private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/fixture/beacon-receipts.jsonl \
  --output /absolute/path/to/capture/owned-beacon-evaluation.json
```

The evaluator never imports or replays an estimator. It joins exact native bytes
and sequence to recorded input/snapshot, decision, authorization and actual HTTP
receipt. It directly source-matches every qualified query and event decision at
actual per-buffer Mach timestamps. Capture-only anchors stay within one audible
span; startup uses a forward window with exact query-relative source position.
Threshold timing is independently rematched and cannot cross pause uncertainty.
Clock tolerance stays 21.333 ms; threshold-to-decision bound stays 141.333 ms. Early
events, uncertainty and decision-to-receipt network delay remain separate fields.
Preroll is required only when the first real qualified point is within 250 ms;
late onset cannot manufacture zero. Paused B allows only pre-pause authorization,
and its source overlap must be confirmed. A/C/D/E require all six events exactly
once. All receipt attempts are retained in the report, including rejected ones.
A passing clock/beacon audit still requires separate whole-audio, prefix/tail,
continuity, no-replay and natural-EOF evidence from two final-candidate captures.


### Component stall diagnostics v2

Version 2 archives retain the following logical diagnostic fields. The
ownership protocol stays v1. Current wire encoding is version 3 (below); the
archived summarizer accepts strict v1/v2/v3 snapshots, including mixed archives,
and reports the original wire versions observed.
Six new completed-operation metrics retain maximum Mach brackets and use the
existing strict >40 ms event threshold and 12-event bound:

- `output_guard_before` / `output_guard_after`: each actual synchronous guard
  check, including failure retirement; absent counts mean no check ran.
- `clock_observation`: the entire clock observation, formatting and append.
  It completes after this row's snapshot and is visible in a later row.
- `native_append`: owned transport identity validation and write. It excludes
  waiting for the outer telemetry mutex, already measured separately.
- `host_demux_call`: top-level host DemuxRead entry through every return.
- `host_demux_interarrival`: successive DemuxRead entry timestamps, separately
  from the existing silent-packet delivery interarrival. Late entries with short
  service times indicate missing host requests rather than slow addon service.

Overlapping/nested durations must not be summed. Missing/unavailable/dropped
metrics do not prove an absence of stalls; no pending operation can be reported
until it completes. Startup and requested pauses legitimately lengthen host
intervals and require the existing control/lifecycle context.

Each available snapshot retains at most four `host_threads` observations, keyed
by thread ID and identity-query status. Each contains the first readback's Mach
time, identity status, QoS status/class and relative priority. Preparation and
playback can use different callers. QoS is read once per retained caller per
instance, never set by this diagnostic; it is not a continuous QoS assertion.
Read failures remain explicit status values, and additional caller observation
attempts increment `dropped_host_threads`. The collector remains fixed-size and
uses try-locks; no listener callback performs diagnostic work. State resets
before workers launch. The existing renderer QoS policy is unchanged.

Compile `test_stall_diagnostics.cpp` with C++14, `-pthread`, and optionally
`-DNDEBUG`; all checks remain active in both configurations. An optional output
filename writes native v3 JSONL for `summarize_stalls.py` integration validation.
The new timing does not qualify physical output or fix the retained run2/run3
underruns, and it does not change the frozen estimator or clock eligibility.

### Lossless compact diagnostic wire version 3

Only `stall_diagnostics` changes representation. Collection, all 22 operation
counts and maximum brackets, retained event order, drop counters, host identity
and first QoS observations are unchanged. Every snapshot still emits every
operation, including zero counts. No clock, ownership, source, output, control,
host representation or eligibility field changes; sampling remains 20 ms.

Version 3 has exactly five keys in emitted order: `version`, `header`,
`operations`, `events`, `threads`. Their immutable layouts are:

- `version`: integer 3.
- `header`: `[threshold_ns, available, revision, dropped_contention,
  dropped_events, dropped_host_threads]`; threshold remains 40000000,
  availability is a strict JSON boolean, remaining values are uint64.
- `operations`: exactly 22 tuples in ascending ID order,
  `[operation_id, count, max_ns, max_start_mach_ns, max_end_mach_ns]`.
- `events`: zero through 12 tuples `[operation_id, start_mach_ns, end_mach_ns]`
  in original occurrence order. Repeated operations are legitimate events.
- `threads`: zero through four tuples `[thread_id, observed_mach_ns,
  identity_status, qos_status, qos_class, relative_priority]` in original order.
  ID/time are uint64; the four status/class/priority values are signed int32.

Operation IDs are frozen in this order (never infer or reorder them):

| ID | Operation | ID | Operation |
| --- | --- | --- | --- |
| 0 | control_read | 11 | renderer_loop |
| 1 | telemetry_mutex_wait | 12 | getspace |
| 2 | telemetry_write | 13 | decode_owned_mutex_wait |
| 3 | telemetry_flush | 14 | decode_owned_mutex_held |
| 4 | renderer_owned_mutex_wait | 15 | packet_owned_mutex_wait |
| 5 | clock_owned_mutex_wait | 16 | output_guard_before |
| 6 | host_owned_mutex_wait | 17 | output_guard_after |
| 7 | adddata | 18 | clock_observation |
| 8 | renderer_sleep_overshoot | 19 | native_append |
| 9 | host_packet_interarrival | 20 | host_demux_call |
| 10 | host_packet_call | 21 | host_demux_interarrival |

`summarize_stalls.expand_diagnostic` validates tuple widths, strict types,
integer bounds, canonical unique operation IDs, unique thread identities, event
limits and all existing v2 semantic constraints, then returns the equivalent v2
logical object. Unknown versions/IDs/fields are rejected. It does not mutate the
original object. Valid v1/v2 archives retain their existing interpretation.
`compact_diagnostic` is an offline v2 round-trip verification encoder.

The runtime owned-clock provider and beacon evaluator do not consume this
object; their byte-hash paths remain unchanged. Expansion is an offline view,
never a replacement ledger: all evidence references and SHA256 identities must
continue to use the original emitted row bytes. The saved run12 v2 fixture has
three actual diagnostic snapshots and source-row hashes, not reconstructed
runtime evidence. Compaction is a one-variable experiment, with no claim that
fewer formatted bytes resolve output underruns; fresh captured acceptance is
still required.

### Scoped PAPlayer dependency override experiment

The current owned-clock candidate adds one controlled scheduling experiment to
an exact session-owned loopback HLS launch. It does not enable production paths
or establish that the retained output gaps are fixed. The native source FIFO,
AAC host format, startup seek sequence, AddData batching, renderer QoS, output
guard, pause/resume and natural EOF paths retain their existing behavior.

Only a live synthetic host callback with source ownership transferred, decoding
armed and active playback can start an override. The current pthread name must
be exactly `PAPlayer`, its identity must be valid and distinct from the renderer
owner, and the first playback identity stays pinned for the incarnation.
`JobWorker` preparation is ignored before a playback identity is pinned; other
unsupported active callers or an identity change retire the experiment.
Acquisition uses `pthread_self()` and public
`pthread_override_qos_class_start_np(QOS_CLASS_USER_INITIATED, 0)`. No pthread
handle is retained and no requested-base QoS or process policy is changed.

One opaque token spans active host callback intervals. A shared generation gate
serializes acquisition against controls. Native pause creates a revision barrier;
only a fresh validated play/resume revision can reactivate after that barrier.
The existing renderer ends the token before pause acknowledgement, on drain and
on every renderer exit. Abort/close/failure disable future acquisition before
cleanup/join. The target never ends its own token. Every end attempt consumes
and clears the token, including API errors; a consumed token is never retried.
Failures retire clock qualification while leaving audio feeding unchanged.

Native pause also fences the complete authority-Apply/override-publication
transaction: its suspension epoch is captured before Apply, and the command
issue time must be later than the last native pause (same Mach time domain).
A rejected publication blocks that applied revision permanently, including
periodic replay. Every native pause advances the epoch, even repeated pauses.
A fresh command received while native speed is paused cannot acquire a token
until normal native resume; native resume alone never clears the revision barrier.

Each clock row adds bounded `host_override.version=1` evidence: target/owner IDs,
verified target name, requested override class 25/relative 0, control and state
generations, active/terminal/failure flags, start/end attempt/success counters,
last attempt times/statuses, ignored preparation count and outstanding/maximum
counts. At most one token may be outstanding. Requested host QoS can correctly
remain 21 while an override is active; neither token success nor requested QoS
proves effective scheduling. The existing `qos_unconfirmed` pending reason blocks
qualification until an active token exists, and an override generation change
within a sample also blocks qualification. The frozen estimator is unchanged.

`test_host_override.cpp` runs portable C++14 debug/NDEBUG lifecycle and race
checks with injected OS operations. Optional `test_host_override_macos.cpp`
creates and cleans one token on its own test thread only; it does not attach to
Kodi. The archived summarizer validates the new evidence and retains transitions.
An independent review and captured activation/cleanup/no-gap validation remain
mandatory before this experiment can support any acceptance claim.

### Fractional captured-waveform audit

`evaluate_owned_beacons.py` identifies its matching method and hashes the input
PCM, capture metadata, per-buffer timing, analysis, native/event/receipt logs,
reference and evaluator sources. Choose a new output filename for every
re-evaluation: the evaluator refuses to overwrite an existing report. Older
integer-only and supplementary reports remain evidence of their original runs.

The fractional method uses actual buffer Mach times and independent waveform
anchors. Each query retains its own 40 ms stereo held-out comparison. Fractional
phase comes from a separate 50 ms audio window at least 50 ms away, using a
normalized 96-tap Blackman sinc, a fixed ±1-sample search, 0.05-sample coarse
steps and 0.005-sample refinement. The half-second audio grid and one local
fallback are deterministic; a 512-entry training cache never replaces a query's
held-out comparison. Both training and held-out correlations must reach .99;
stereo positions must agree within one source sample. Gains are reported without
fitting them. Recorded clock estimates never select a source window or phase.

Per-channel 0.5 ms quiet bins, exact zeros and recorder timestamp gaps delimit
regions. Interior quiet edges and both sides of recorder timestamp cuts carry
a 96-frame filter guard covering the full interpolation support. Source bounds
prevent short/negative slices; unavailable windows produce structured
uncertainty. Threshold times are independently rematched in the same region.
The method does not interpolate across silence, stitch a source jump, change
clock tolerances, or resample the tested clock.

This proves only the matched windows. Existing dense-analysis aggregate offset
ranges do not provide an adjacent-window continuity trace and cannot distinguish
all gradual drift from omitted/repeated material. Whole-source continuity,
uncommanded gaps, complete prefix/tail and natural EOF retain their separate
whole-audio acceptance gate. An improved clock match cannot accept failed audio.

Only the independently identified initial source prefix may contain its own
initial quiet samples; queries there require the complete held-out prefix to
match. This does not make an interior silent interval matchable. Cached matches
also check source progress in capture-time order, allowing one sample of
uncertainty. Contradictory later audio evidence invalidates both endpoints while
retaining their numerical waveforms, regardless of which query was requested
first. The final gate revalidates every required clock, pause, delivered decision
and threshold after all matching, including canceled decisions. An uncertain
canceled decision alone does not fail the required evidence. No global
one-sample limit is imposed on gradual drift over a long span.


### Bounded PCM synthetic host experiment

The exact controlled HLS owned renderer now advertises a separate host projection:
`pcm_f32le`, interleaved stereo 48 kHz, 32 bits/sample, 8-byte block alignment,
3072000 bit/s, no AAC extra-data/profile/fourcc. Every initial format-probe and
playback host packet contains 3840 zero PCM frames (30720 bytes, 80 ms). The original
AAC source snapshot, first-packet hash/proof, decoder, source reader, FIFO and
AddData accounting remain AAC/owned-source data. Source bytes never become PCM
host samples. The existing one-time capability→1ms→0 probe/rewind arms the real
source and resets the synthetic playback timeline to zero; probe counters remain
separate. Unsupported routes retain their previous behavior.

This tests refill granularity: PAPlayer can request 1920 stereo frames per cycle,
and retain the remainder of an 80 ms PCM packet. It does not prove that host cache
starvation caused previous gaps, or establish a no-gap result. The existing
scoped PAPlayer override is unchanged.

Native source/clock rows add `host_representation.version=1` with codec, format,
packet frames/bytes/duration, `playback_frames`, `playback_packets`, `probe_frames`,
`probe_packets`, and `generation_closed`. Historical fields
`host_output_aac_access_units` and `synthetic_host_aac_access_units` remain zero
for PCM because no host AAC is emitted. Never interpret either zero as an empty
PCM host queue. Historical ledgers without `host_representation` retain AAC-unit
semantics. For host delivery comparisons, select the representation first and
use its explicit PCM frame count. Current waveform/clock analysis uses owned
`accepted_adddata_frames` and source sample accounting; it does not use these
legacy host AAC counters and needs no conversion. Existing stall operation times
remain times, while longer PCM demux spacing alone is not evidence of starvation.

PCM generation ends on owned drain/stop/failure and cannot reopen after an error.
It continues during source EOF until owned audio has drained. One packet bounds
the decoder-frame remainder by 3840 frames, **not** total already-queued host tail;
`downstream_queued_tail_frames:null` explicitly records that the addon cannot
observe PAPlayer/AE queues. Capture must measure actual EOF delay. Integer frame
arithmetic avoids cumulative PTS drift and rejects time-counter overflow.

`test_pcm_host.cpp` uses the actual Kodi InputstreamInfo type plus the production
helper to check metadata/source separation, zero payload/sentinels, timeline,
probe/reset, bounds/errors and terminal behavior in C++14 debug/NDEBUG. These are
helper-level checks; the full addon build and manual integration inspection do
not replace live initial-probe/rewind, normal pause, complete-source, EOF and
no-gap capture validation. Kodi's unchanged executable contains the PCM_F32LE
decoder symbol; actual live format discovery remains a capture gate.

### Owned HAL component harness (no playback)

The new additive `../inputstream.ffmpegdirect-22.2.10-owned-hal-fixture.patch`
applies after the complete existing chain through `owned-clock-session.patch`.
At this checkpoint it adds only four independent native component files. It
neither selects a backend nor changes the build, StreamManager, installed addon,
Kodi, recorder, output settings, or frozen V2 estimator. Actual audio/clock and
normal-control acceptance remain separate gates.

Run the focused native harness on macOS after applying the patch. The fake device
API prevents real CoreAudio registration/start. The real `OwnedHalDevice.cpp`
still compiles and links, and the fake dylib includes that actual implementation.
The separate RT interposer is harness-only; its child-process self-tests prove
that malloc, mutex acquisition and write calls would be caught during Render.

```sh
HAL_SRC=/private/tmp/kodi-audio-validation-20261027/inputstream.ffmpegdirect-22.2.10/src
HAL_OUT=$(mktemp -d /private/tmp/owned-hal-tests.XXXXXX)
HAL_LAB=docs/patches/owned-hls-lab
clang++ -std=c++14 -Wall -Wextra -Werror -dynamiclib \
  "$HAL_LAB/hal_test_rt.cpp" -o "$HAL_OUT/hal-test-rt.dylib"
for HAL_FLAGS in '' '-DNDEBUG' '-DNDEBUG -O2'; do
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS -I"$HAL_SRC" \
    "$HAL_LAB/test_hal_core.cpp" "$HAL_OUT/hal-test-rt.dylib" -o "$HAL_OUT/core"
  "$HAL_OUT/core"
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS -I"$HAL_SRC" \
    "$HAL_LAB/test_hal_lifetime.cpp" -o "$HAL_OUT/lifetime"
  "$HAL_OUT/lifetime"
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS -I"$HAL_SRC" \
    "$HAL_LAB/test_hal_device.cpp" "$HAL_SRC/OwnedHalDevice.cpp" \
    -framework CoreAudio -framework CoreFoundation -o "$HAL_OUT/device"
  for HAL_CASE in ok layout buffers pin resolve read add create start route \
      stop remove destroy hang hang_remove hang_destroy callback_hang \
      cancel_before cancel_pin cancel_add cancel_create cancel_race \
      delayed_entry delayed_failed never_exposed_second; do
    "$HAL_OUT/device" "$HAL_CASE"
  done
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS -dynamiclib -I"$HAL_SRC" \
    "$HAL_LAB/hal_test_module.cpp" -framework CoreAudio -framework CoreFoundation \
    -o "$HAL_OUT/module.dylib"
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS \
    "$HAL_LAB/test_hal_lifetime_macos.cpp" -o "$HAL_OUT/dyld"
  "$HAL_OUT/dyld" "$HAL_OUT/module.dylib" unpin
  "$HAL_OUT/dyld" "$HAL_OUT/module.dylib"
  "$HAL_OUT/dyld" "$HAL_OUT/module.dylib" retained-clean
  clang++ -std=c++14 -Wall -Wextra -Werror -pthread $HAL_FLAGS -dynamiclib -I"$HAL_SRC" \
    -DOwnedHalModuleIdentityV1=DeliberatelyWrongIdentity "$HAL_LAB/hal_test_module.cpp" \
    -framework CoreAudio -framework CoreFoundation -o "$HAL_OUT/mismatch.dylib"
  "$HAL_OUT/dyld" "$HAL_OUT/mismatch.dylib" mismatch
done
```

Use a POSIX shell for this example (`sh`/`bash`), so the flag variable splits as
shown. Each device fault runs in its own process. Once any registration API has
received the context, it remains retained and the session slot stays quarantined
until process exit, even after successful cleanup. `clean` means OS cleanup
completed; it does not mean memory was reclaimed or delayed entry was excluded.
A fresh process is required for another exposed HAL session. Never-exposed
contexts may release their slot after cleanup wins the terminal arbitration.

The unpinned control proves actual unloading. The pinned variants drop the
ordinary loader reference and destroy the owner after either hung or successful
cleanup, then invoke the real retained callback/context. Cancellation tests cover
before Prepare and deterministic registration boundaries; the lifetime harness
checks both cleanup/timeout winners with a competing owner. These tests do not
establish live speaker continuity or timing.

The IOProc relies on CoreAudio's documented zero-on-entry buffers. Malformed
buffer counts/lengths retire without traversing or writing their storage. Pure
HalCore callers must supply the same zero-on-entry condition for invalid spans.
A valid callback always processes exactly one 4096-byte stereo block. Source
advance and copied frames include muted source; `zero_frames` counts synthetic
padding, not naturally zero samples or mute-scaled output.
