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
- `/icy/{scenario}.mp3`: finite, paced MP3-format ICY framing for each controlled marker scenario. It sends one 4,096-byte audio interval and one metadata block every 250 ms.
- `/expected/icy/{scenario}.json`: expected ordered ICY marker events and classifications. Use the `explicit`, `tildeats`, `wrapped`, `return-to-track`, `paired-marker`, `punctuation`, and `unresolved` scenarios.
- `/stats`: request counts by route, including the resolver's manifest probe.
- `/assets/segment000.ts` through `/assets/segment005.ts`: playable HLS audio, with single-range 206 and 416 responses.

Each HLS schedule starts with its first request, advances at four-second intervals, and finishes after three versions. The resolver probe and immediate playback fetch therefore normally see the same initial window. Restart the server to reset schedules and counters. Late polling or mid-schedule entry can skip observations; the expected JSON describes a client that consumes every window.

Production URL policy intentionally rejects loopback/private origins. Python integration tests explicitly allow their exact local fixture origin. For addon/Kodi smoke tests on the same host, enable test mode and set the exact fixture origin (for example `http://127.0.0.1:8765`, without a trailing slash). Only numeric `127.0.0.1`, HTTP and the explicitly configured port are allowed; other local origins stay blocked. Test mode is disabled by default. The fixture server is a developer origin, not the embedded media relay.

The controlled ICY sequences cover exact `AD_START`, bounded `AD_START_<seconds>`, `AD_STOP`/`AD_END`, terminated TildeATS category 4 and category 0 values, TildeATS `##` end values, and wrapped `#AD_START#`/`#AD_END#` commands. Each finite route uses stripped ID3-free frame bytes from the generated `tone.mp3`, repeated to fill its 4,096-byte audio intervals; complete audio-plus-metadata frames are sent every 250 ms. In Kodi test mode, each scenario is selectable as an MP3 entry in **Controlled test streams**. Unknown values are candidates only when they are punctuation-only, exact marker vocabulary, or a short visibly framed token such as `*BREAK*`; marker words inside ordinary artist/title text do not match. Bare `-`, `#`, and `*` have no assigned direction. They can contribute to an inferred break when a later track-like value returns or the same marker appears after intervening metadata. A break that remains open past 300 seconds is recorded as unresolved, never as a fabricated end.

Marker inference retains at most 64 observations and 300 seconds. A listener can label the current candidate for the active session; choosing to remember it stores that exact value for that station only. ICY handling is observation-only: metadata framing is stripped while audio bytes are preserved, and ICY events never enter VAST, replacement playback, or beacon tracking. `/icy/unresolved.mp3` sends its marker immediately; tests advance the tracker clock to 301 seconds so timeout behavior is covered without a five-minute wait. The finite endpoints are intended for local fixture decoding and automated checks. The repeating `/icy.mp3` remains available, and Kodi's controlled-stream menu has a separate entry for each finite scenario. Expected marker events are available at `/expected/icy/{scenario}.json`.

Relay behavior requiring actual Kodi playback validation: HEAD returns 200 with retained continuous-stream headers or the known playlist MIME type, without claiming, fetching, or observing playback; segment/map HEAD remains 405 and makes no upstream request; continuous `Range: bytes=0-` is treated as a full-body GET (200), while other continuous ranges return 416 without consuming the probe; one continuous GET owns the upstream, and its completion/disconnection retires the session. A later GET uses an expired URL and receives 404 (a simultaneous second GET receives 409). HLS playlists use complete GET responses, while segment/map ranges are forwarded. No Kodi playback compatibility has been verified by these tools or the pure-Python tests.

## Controlled advertising origin

Generate the separate ad fixtures and start their loopback-only origin:

```sh
python3 tools/generate_ad_audio.py --assets tests/generated-audio/ad
python3 tools/ad_fixture_server.py --assets tests/generated-audio/ad --port 8766
```

The checked-in `tools/ad_audio_sources.json` describes every tone and spoken identifier. Generation uses developer FFmpeg, FFprobe and espeak; no generated binaries or runtime transcoder enter the addon. `metadata.json` beside the generated files records FFprobe packet and decoded-frame sample counts, MPEG PTS, and the tone identity checked by independently decoding each transport-stream segment. All 29 segments are stereo AAC-LC at 48 kHz with 94 frames (96,256 samples); original-break and replacement pairs have equal frame counts.

The ad origin serves `/vast/direct`, `/vast/wrapped`, `/vast/preroll`, `/vast/no-fill`, `/vast/empty`, `/vast/wrapper-cycle`, `/vast/wrapper-depth` (six nested wrappers), `/vast/invalid-xml`, `/vast/slow` (six seconds), `/vast/unsupported-media`, `/vast/wrong-duration`, and `/vast/failed-media`. The successful MediaFile points to `/ads/replacement.m3u8` or `/ads/preroll.m3u8`; their MPEG-TS segments and the intentionally unsupported MP4 are under `/ads/`. Source streams are `/source/normal/index.m3u8`, `/source/mutation/index.m3u8`, `/source/expired-window/index.m3u8`, `/source/finite/index.m3u8`, and `/source/stall/index.m3u8`. Each begins with programme A, an explicitly marked original break, and programme B. The normal and stall windows advance by one media sequence every 2.005333 seconds, independent of request count; the source has 24 sequential TS segments. Mutation and expired-window advance per request to make their error conditions deterministic: mutation changes a break segment identity and expired-window drops the early window. Finite has a static end marker, and stall delays source006.

For the independent ad acceptance run, use `python3 tools/smoke_ad_relay.py --origin http://127.0.0.1:8766 --report /path/outside/addon/relay.json`. It fetches every published relay TS segment, independently probes its AAC frame/sample count with FFprobe, decodes a tone window with FFmpeg, and checks the resulting section order and collector state. The trace validator rejects original-break leakage, missing programme, sample-count differences, duplicate playback events, completion after stop, and any claimed success beacon because this relay-only run has no independent playback evidence. The first live window includes source000 through source005 even after multiple prompt manifest requests. VAST `/vast/wrong-duration` declares nine seconds but carries a valid 4.010667-second AAC creative; measured media duration is authoritative, so the tool expects replacement audio.

With a disposable Kodi portable profile and its JSON-RPC listener, run `python3 tools/smoke_ad_kodi.py --origin http://127.0.0.1:8766 --rpc-port 9090 --report /path/outside/addon/kodi.json`. It opens the controlled entries in the addon's test menu and records 250 ms player clock samples, selected actions, and fixture requests. It checks that the active Kodi item is the requested addon item and that its clock advances in bounded adjacent samples. This is a Kodi player observation, not a proof of audible output or a calibrated ad position. Keep success tracking disabled until decoded presentation position can be independently tied to Kodi's clock across both discontinuities and a moving live window. Reports omit session URLs and authentication tokens.

`/track/<placement>/<event>` alone records a beacon. `/stats` reports request, beacon, and window counters without counting itself. `/reset` clears them and restarts all source windows. Fetching VAST, playlists, or media cannot create a beacon.
