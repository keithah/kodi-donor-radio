# Task 5 report — controlled ads in the relay

## Result

`AdConfig(enabled, preroll, midroll, vast_url, fixture_origin)` is now the sole session advertising input. The session manager accepts `ad_config` and `cache_root`; a station record cannot enable insertion. Composition is restricted to actual HLS sources and a fixture origin explicitly present in the client's test-origin policy. General HLS and continuous playback remain on the existing relay path.

An ad session resolves and prepares preroll before choosing the first retained source window. Source segments are inspected as AAC-LC MPEG-TS, copied into session-owned immutable in-memory storage, and bounded separately to 32 MiB and 128 segments. The timeline retains at most 64 output segments so a full new source window can fit alongside the previous published window. Cached creative and source bytes are served only under opaque session routes with strict single-range responses. HEAD and repeated/ranged GET do not resolve or prepare ads. Published output uses the first retained segment's sequence and discontinuity as playlist bases, whole-frame durations, and `EXT-X-START:TIME-OFFSET=0,PRECISE=YES` on the first preroll playlist.

Midroll preparation runs in a background worker. If the first observed source manifest already contains a complete break, the relay publishes the safe programme prefix until preparation finishes; a later reload exposes the complete interval to `AdTimeline`. Cue observation still uses the full original manifest. Concurrent reloads produce the same immutable replacement output. Preparation or source-cache failure before first publication falls back to the original relay path. After publication, an incompatible failure or `TimelineEpochLimit` ends the composed session with a fixed 502 response; it never replays old audio or switches back to a different timeline. Session stop deletes the creative cache and invalidates all routes. The creative cache releases prepared pins on discard, output retirement, and close.

## RED / GREEN

- RED: `tests/test_ad_relay.py::test_preroll_is_first_published_audio_and_cached_gets_are_stable` failed importing the missing `AdConfig` before implementation.
- RED during the first focused integration run: no-fill incorrectly emitted a composed source manifest; the test expected the original cue-bearing manifest. The session now discards an unavailable preroll when no midroll is requested.
- GREEN: `python3 -m pytest tests/test_ad_relay.py tests/test_sessions.py tests/test_relay.py tests/test_cues.py -q` — 123 passed.
- GREEN: `python3 -m pytest -q -s` — 476 passed in 72.54 seconds, exit 0.
- `git diff --check` and Python compile checks passed.

The HTTP integration tests decode served MPEG-TS through FFmpeg, compare the audible preroll/replacement tone with the programme tone, and verify byte identity, ranges, source-window expiry, concurrent midroll completion, no-fill/unsupported-media fallback, a small retention limit, and stop cleanup. An untrusted station control payload carrying an `ads` field is rejected. No fixture tracking endpoint is called by media fetches.

## Interfaces and follow-up

- `PlaybackSession.presentation()` returns the immutable `Presentation` for composed sessions and `None` otherwise.
- `SessionManager(..., ad_config=None, cache_root=None)` remains backward compatible. Task 7 must pass a writable profile `cache_root` and raise the existing ten-second control request timeout: synchronous preroll setup can consume the resolver's five-second deadline plus the creative cache's ten-second deadline before `create()` returns.
- Source cache bytes are held in memory. The creative cache's abandoned-directory cleanup happens when the first ad session opens the cache namespace. Task 7 can initialize that namespace at service startup if cleanup is required before any ad session.
- The full suite printed the intermittent fixture-server `BrokenPipeError` banner at `tests/test_transport.py:89` when that pre-existing test's HTTP handler wrote to a disconnected socket. The suite completed with exit 0 and no failed tests; Task 9 may quiet the fixture if desired.
