# Owned AAC renderer diagnostic helpers

These are frozen local test helpers, not installed or packaged addon code.
They accompany the owned-AAC lab patch in the parent directory. They retain
the exact temporary paths used by the captured experiment on this Mac.

`default.py` temporarily replaces the radio addon's entry point in the isolated
portable test profile. Its diagnostic route starts `monitor.py`, waits for it
to become ready, then plays the exact loopback AAC URL through FFmpegDirect and
PAPlayer. Other routes delegate to the existing addon runtime. The finite test
item explicitly opts into terminal EOF.

`monitor.py` must be copied to the lab path embedded in both the entry point
and native patch. It gates programme submission from normal Kodi player
callbacks: AVStarted, Paused, Resumed, Stopped and Ended. A bounded test run owns
the control files; only one monitor may run at a time. End each run with the
`monitor-stop` sentinel and wait for `monitor-ready` to disappear before
starting another. These shared files are unsuitable for production sessions.

The native route additionally requires both explicit enable markers and a
matching 13-byte AAC-LC silence frame at its documented lab path. It rejects
missing/invalid setup rather than falling back to ordinary playback. The real
programme is decoded from incoming AAC packets; no predecoded PCM file is
read. Tests renamed the old PCM fixture to prove that dependency was removed.

Do not enable this route for general stations. It only accepts the controlled
48 kHz stereo ADTS AAC fixture, rejects later seeking, and has a bounded
64-second PCM queue. HLS, format changes, device changes, multiple sessions,
volume behavior, live starvation recovery and absolute playback clock mapping
are not qualified. The recorded pause-continuity result is not a success-beacon
or release acceptance result.

The plan `docs/superpowers/plans/2026-10-27-kodi-pause-boundary.md` records binary
identities, captured evidence, review findings and the restoration procedure.

Monitor startup clears a stale stop sentinel and rotates the previous callback
ledger before writing fresh readiness. Start only after the previous monitor
has exited; the helpers still require one monitor session at a time.
