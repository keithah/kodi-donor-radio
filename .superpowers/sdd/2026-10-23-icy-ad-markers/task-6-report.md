# Task 6 report: controlled ICY fixtures, documentation, and package verification

Status: fix round 1 implementation complete; awaiting scoped independent re-review. Initial implementation commit: `79908df` — `test: add controlled ICY marker fixtures`. Fix round 1 commit: `c968572` — `fix: make ICY marker fixtures playable`. The commits include the controlled fixture server, fixture tests, ICY documentation, localized menu entries, and the stale oversized-marker test correction. The pre-existing Kodi clock-anchor edits remain outside these commits.

## Implementation

Added finite, paced fixture streams at `/icy/{scenario}.mp3` and ordered expectations at `/expected/icy/{scenario}.json`. Scenarios cover `AD_START`/`AD_STOP`, TildeATS category 4/category 0 and `##` end forms, wrapped `#AD_START#`/`#AD_END#`, bare `-`/`#`/`*`, inferred return-to-track and paired-marker endings, and an inferred break that expires unresolved after 300 seconds. The server sends one 4,096-byte audio interval and metadata block every 250 ms.

Tests feed the ICY framing through `IcyDecoder` in 137-byte chunks, compare the decoded audio byte-for-byte with the fixture audio, and assert the ordered event kind/classification for every scenario. The unresolved case advances the monotonic clock to 301 seconds instead of waiting five minutes. Expected events contain no replacement, VAST, or beacon event. Updated the README, fixture guide, and testing record with supported forms, bounded marker recognition, the 64-observation/300-second limits, exact station-scoped listener labels, and the observation-only boundary.

The full suite exposed two stale assertions for 257- and 4,000-byte punctuation values. The existing parser correctly rejected them as marker candidates under the 256-byte candidate limit while preserving their full metadata observation. Updated `tests/test_icy.py` so the 256-byte boundary retains candidate evidence and larger values produce no marker event.

## TDD and verification

RED: `python3 -m pytest tests/test_fixtures.py::test_controlled_icy_scenarios_match_expected_events_and_preserve_audio -q` failed with `AttributeError: module 'fixture_server' has no attribute 'ICY_SCENARIOS'` before fixture implementation.

RED: `python3 -m pytest tests/test_fixtures.py::test_controlled_icy_http_routes_publish_paced_metadata_streams -q` returned 1 failed because `/expected/icy/tildeats.json` returned HTTP 404 before the expected-event route existed.

GREEN: `python3 -m pytest tests/test_fixtures.py -q` → `4 passed in 1.74s` after adding the stream and expectation routes.

The first broad focused run, `python3 -m pytest tests/test_fixtures.py tests/test_icy_markers.py tests/test_relay.py tests/test_kodi_runtime.py -q`, returned **211 passed** (exit 0). The full suite then found the two stale `tests/test_icy.py` expectations: **695 passed, 2 failed in 96.32s** (exit 1). Rerunning `python3 -m pytest tests/test_icy.py -q` reproduced those same two failures and passed the other 18 tests. After updating the assertions, the expanded focused command:

```text
python3 -m pytest tests/test_icy.py tests/test_fixtures.py tests/test_icy_markers.py tests/test_relay.py tests/test_kodi_runtime.py -q
```

completed with:

```text
231 passed in 31.72s
exit status: 0
```

Final full suite command: `python3 -m pytest -q`

```text
........................................................................ [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 41%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 72%]
........................................................................ [ 82%]
........................................................................ [ 92%]
..........                                                               [100%]
697 passed in 97.04s (0:01:37)
exit status: 0
```

Compilation and whitespace checks:

```text
python3 -m compileall -q plugin.audio.kodi_donor_radio tests && git diff --check
exit status: 0
```

Package command: `python3 tools/package_addon.py --output /tmp/kodi-donor-radio-task6-package`

```text
/private/tmp/kodi-donor-radio-task6-package/plugin.audio.kodi_donor_radio-0.1.0.zip
exit status: 0
```

Checker command: `python3 tools/check_package.py /tmp/kodi-donor-radio-task6-package/plugin.audio.kodi_donor_radio-0.1.0.zip`

```text
Package OK: /tmp/kodi-donor-radio-task6-package/plugin.audio.kodi_donor_radio-0.1.0.zip
exit status: 0
```

Package SHA-256: `8bec111f5ec5daaa2cdc3b56e9b2ab8910f207fe67c11e527898bc8932d87cfc`.

## Kodi target

At the initial Task 6 review, Kodi was installed at `/Applications/Kodi.app` and a process was present on host `mbpdev.local`, the remote test host rather than the user's local Mac. I did not interact with Kodi or claim UI/playback verification. At that point, the finite scenarios were not entries in the add-on's controlled-stream menu; the fix round below adds the menu entries. Manual playback remains pending on the user's local Mac.

## Fix round 1: selectable MP3-backed scenarios and relay-path proof

Addressed all three review findings:

1. Added seven localized entries to the test-mode **Controlled test streams** menu. Their serialized station URLs use only the exact configured `http://127.0.0.1:<port>` origin, and runtime tests verify every scenario label/path and that an invalid `127.0.0.2` origin creates no station entries. Existing production-origin restrictions are unchanged.
2. Scenario responses now use 4,096-byte intervals filled by looping the ID3-stripped MP3 frame payload from the generated `tone.mp3`. Each complete audio interval plus its variable-length ICY metadata block is serialized and written as one frame, followed by a 250 ms wait. Tests read the complete frames and require tolerant 120 ms–1.5 s inter-frame timing.
3. Added a test through a real loopback `RelayServer` and `SessionManager` using the paired-marker route. It verifies exact stripped MP3 bytes, ordered ICY observations, no ad session/eligibility, and no replacement/VAST/beacon event.

TDD RED: the new Kodi-menu test first failed because no scenario URLs were present. The HTTP timing and relay integration tests both first failed because the audio builder did not accept the generated MP3 source. These failures preceded the implementation changes.

Focused verification command:

```text
python3 -m pytest tests/test_fixtures.py tests/test_kodi_runtime.py tests/test_relay.py tests/test_icy.py tests/test_icy_markers.py -q
```

```text
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
.................                                                        [100%]
233 passed in 33.78s
exit status: 0
```

Full-suite command: `python3 -m pytest -q`

```text
........................................................................ [ 10%]
........................................................................ [ 20%]
......................................... [ 30%]
........................................................................ [ 41%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 72%]
........................................................................ [ 82%]
.................... [ 92%]
....................................................
....                      [100%]
699 passed in 98.12s (0:01:38)
exit status: 0
```

`python3 -m compileall -q plugin.audio.kodi_donor_radio tests && git diff --check` completed with exit status 0.

Packaging command: `python3 tools/package_addon.py --output /tmp/kodi-donor-radio-task6-fix1-package` → `/private/tmp/kodi-donor-radio-task6-fix1-package/plugin.audio.kodi_donor_radio-0.1.0.zip` (exit 0). Checker command: `python3 tools/check_package.py /tmp/kodi-donor-radio-task6-fix1-package/plugin.audio.kodi_donor_radio-0.1.0.zip` → `Package OK` (exit 0). Updated ZIP SHA-256: `1b8c18af079336c531bcbcafee44a492cee2ba056140e5f30e5b414311ca03a4`.

Independent media probe: the generated paired-marker MP3 payload, looped to fill its fixture intervals, was accepted by `ffprobe`; command output was `12.000000`, exit status 0.

Kodi target distinction: the current shell host is `mbpdev.local`, where `/Applications/Kodi.app` is installed/running. The user asked to use the local Mac rather than mbpdev; therefore I did not run or inspect Kodi's GUI on this host. The runtime UI test validates menu data only; actual UI playback remains pending on the user's local Mac.
