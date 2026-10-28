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
