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
a bounded 64-second PCM ring. Telemetry is bounded to 20,000 rows and currently
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
