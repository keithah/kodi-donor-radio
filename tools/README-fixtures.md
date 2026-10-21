# Controlled radio fixtures

Regenerate playable synthetic 440 Hz tone assets (MP3, AAC, and HLS transport-stream segments):

```sh
python3 tools/generate_audio.py --ffmpeg /opt/homebrew/bin/ffmpeg
python3 tools/fixture_server.py --assets tests/generated-audio --port 8765
```

FFmpeg is a developer tool only. Generated assets live in the ignored `tests/generated-audio/` directory, outside `plugin.audio.kodi_donor_radio/` and the addon ZIP. Source code and synthetic tone generation are GPL-2.0-or-later; no third-party audio is used.

The fixture server exposes:

- `/continuous.mp3` and `/continuous.aac`: paced, repeating tone audio.
- `/icy.mp3`: MP3 tone with a metadata block every 4,096 audio bytes.
- `/hls/normal/index.m3u8`: explicit OUT and IN.
- `/hls/mid-break/index.m3u8`: continuation on entry, then IN; no inferred start.
- `/hls/missing-end/index.m3u8`: OUT without IN; expected duration produces unresolved evidence.
- `/hls/malformed-cue/index.m3u8`: malformed duration observed as invalid.
- `/hls/no-cue/index.m3u8`: no cues observed.
- `/expected/{scenario}.json`: expected ordered event kinds and classifications when all three manifest versions are consumed.
- `/stats`: request counts by route, including the resolver's manifest probe.
- `/assets/segment000.ts` through `/assets/segment005.ts`: playable HLS audio, with single-range 206 and 416 responses.

Each HLS schedule starts with its first request, advances at four-second intervals, and finishes after three versions. The resolver probe and immediate playback fetch therefore normally see the same initial window. Restart the server to reset schedules and counters. Late polling or mid-schedule entry can skip observations; the expected JSON describes a client that consumes every window.

Production URL policy intentionally rejects loopback/private origins. Python integration tests explicitly allow their exact local fixture origin. For actual addon/Kodi smoke tests, run this developer server on an explicitly reachable public test host with `--host 0.0.0.0`; do not weaken the production URL policy. The fixture server is a developer origin, not the embedded media relay.

Relay behavior requiring actual Kodi playback validation: HEAD returns 405 without consuming a continuous probe; continuous Range returns 416 without consuming it; one continuous GET owns the upstream, and its completion/disconnection retires the session. A later GET uses an expired URL and receives 404 (a simultaneous second GET receives 409). HLS playlists use complete GET responses, while segment/map ranges are forwarded. No Kodi playback compatibility has been verified by these tools or the pure-Python tests.
