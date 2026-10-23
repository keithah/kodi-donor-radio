# Task 6 implementation report: shared Kodi radio map

Status: DONE; commit `2ee413c`; ready for independent scoped review.

## Implementation

- Added `RadioMapWindow`, a shared `xbmcgui.WindowXML` implementation loaded from the add-on's `Default/1080i/map.xml`. Both the root map row and station `Show on map` route construct this same window. The station menu route carries a bounded, validated parent route; closing the modal window returns to the Kodi window that opened it.
- The local schematic displays up to 60 city pins backed by the pure `radio_map.py` projection/navigation model, a station rail capped at 50 entries, selected-city metadata and an explicit empty/offline message. Dynamic pin controls are positioned inside the map bounds. D-pad movement is deterministic and does not wrap; focus can move between pins, station rail, Preview, and Back.
- Root map discovery uses a cancellable, maximum four-page Radio Browser scan (200 rows) scoped to the default saved place's country when available. Station-context entry resolves its UUID first and centers on that station when coordinates exist. The selected station is kept in the seed even if it falls outside the scan page. No station stream URL appears in routes or map list-item properties.
- Rail Play dispatches the existing deliberate `mode=play&uuid=...` route. Map discovery and preview do not call Radio Browser's click endpoint.
- Preview waits for 400 ms of stable focus, performs lookup and session creation on a worker, and checks both generation and cancellation event before playback. Playback starts only when Kodi is idle or still playing this map's own preview. Focus changes/close cancel pending work and stop playback only if the current file is still the map-owned preview URL; server-session cleanup is asynchronous.
- Added localized map labels, runtime integration tests, a Kodi-boundary test module, package-manifest entries for both map Python modules and XML, and package assertions for all 60 pin controls.

## Verification

- TDD RED was observed: the initial focused tests failed because `kodi_radio_map` did not exist.
- `python3 -m pytest tests/test_kodi_radio_map.py tests/test_kodi_runtime.py tests/test_package.py -q`: 178 passed.
- `python3 -m pytest -q`: 898 passed in 100.35 seconds.
- `python3 -m compileall -q plugin.audio.kodi_donor_radio tests`: passed.
- `python3 tools/package_addon.py --output /tmp/kodi-donor-radio-task6-package` followed by `python3 tools/check_package.py /tmp/kodi-donor-radio-task6-package/plugin.audio.kodi_donor_radio-0.1.0.zip`: passed.
- `git diff --check`: passed.
- Checked Kodi's tagged `21.3-Omega` source for `WindowXML` constructor/fallback behavior, `Control.setPosition`, and `ControlList.addItems`, `size`, `getSelectedPosition`, and `getListItem` APIs. This confirms the used API surface is present in the declared minimum version.

## Not verified in this task

No live Kodi window or real playback test was run. Actual skin rendering, D-pad interception/focus restoration, and preview playback/session cleanup still need Task 7's Kodi 21.3 acceptance run. Automated Kodi fakes cannot confirm whether all custom skins provide the same fonts/textures or whether the real input path matches the fake action sequence.

## Review fix round 1

- Serialized preview handoff against focus changes, cancel, and close. The generation/cancellation checks, playability check, ownership assignment, and `Player.play` now share a lifecycle handoff lock with cancellation/close, so a pending `Player.play` cannot begin after those operations return. Cleanup still stops playback only while Kodi's current file is this map's preview URL.
- Kept the map discovery seed at 200 rows when the context-selected station is missing from the four-page scan by reserving one slot for that selected station and retaining the first 199 scan rows.
- Added a deterministic event-gated race test for both cancel and close, and a 200-scanned-row runtime test for an absent selected station.
- RED: `pytest -q tests/test_kodi_radio_map.py::test_close_serializes_with_preview_play_handoff tests/test_kodi_runtime.py::test_map_scan_reserves_one_of_200_slots_for_absent_context_station` failed for both reported defects (close returned during `play`; scan returned 201 rows).
- GREEN: `pytest -q tests/test_kodi_radio_map.py::test_cancel_or_close_serializes_with_preview_play_handoff tests/test_kodi_runtime.py::test_map_scan_reserves_one_of_200_slots_for_absent_context_station` — 3 passed.
- Full verification: `pytest -q` — 901 passed in 97.72s; `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` passed; package creation and `tools/check_package.py` passed; `git diff --check` passed.
- Live Kodi playback/skin behavior remains unverified; Task 7 acceptance is still required.
- Fix commit: `8fbb487` (`fix: serialize map preview cancellation and selection`).

## Review fix round 2: addon playback start arbitration

- Added `PlaybackStartArbiter`, which combines a per-path in-process lock with an OS file lock in the add-on profile. The normal `mode=play` path that calls `xbmc.Player().play` and the map preview's final Kodi-state check plus `Player.play` use the same lock file, so addon-controlled direct starts serialize across threads and separate add-on invocations.
- The preview checks Kodi playback again while holding the shared lock immediately before calling `Player.play`. It also checks after the call whether Kodi is now reporting a different current file. If so, the controller retires its relay session and drops ownership; it never stops the currently playing file.
- Added tests for competing arbiter instances, the preview's last-moment busy-state check, a conflicting Kodi start during `Player.play`, and runtime wiring of the same profile lock. Packager allowlisting includes the new module.
- RED: `pytest -q tests/test_playback_arbiter.py` failed because `playback_arbiter.py` did not exist.
- Focused verification: `pytest -q tests/test_playback_arbiter.py tests/test_kodi_radio_map.py tests/test_kodi_runtime.py tests/test_package.py` — 185 passed.
- Full verification: `pytest -q` — 905 passed in 99.09s; `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` passed; package creation and `tools/check_package.py` passed; `git diff --check` passed.
- Boundary: Kodi has no atomic compare-and-play operation, and UI actions, other add-ons, and plugin resolutions handled by Kodi's `setResolvedUrl` do not participate in this add-on's lock. A nonparticipating start can still race between the final Kodi state read and `Player.play`; the post-start check limits the damage by retiring only our session and preserving any different current playback. This does not establish a zero-race guarantee. Live Kodi behavior remains for Task 7 acceptance.

- Fix commit: `d26f0e3` (`fix: arbitrate Kodi addon playback starts`).

## Review fix round 3: queued preview start and deliberate play

- Removed the assumption that `Player.play` returning means `getPlayingFile()` already changed. A successful call now remains owned and pending; it is not retired because Kodi still reports the prior file immediately afterward.
- If focus changes or the window closes before Kodi reports the preview URL, a worker performs an ownership-safe handoff for up to five seconds. It stops playback only after Kodi reports this exact preview URL as playing; a different current stream is left alone. At timeout it retires the relay session. A start delayed beyond that bound may fail, so this remains a bounded mitigation rather than a Kodi-level guarantee.
- The station rail now cancels/clears the owned preview before issuing its direct `RunPlugin` play action. Added a callback-order test proving playback/session cleanup precedes the direct request, and a deterministic queued-play test proving the relay is retained until Kodi acknowledges the preview URL and later cleanup stops only that URL.
- TDD RED: both new tests failed first: the pending preview was retired on an old current URL, and station `RunPlugin` was queued before preview cleanup.
- Focused verification: `pytest -q tests/test_kodi_radio_map.py tests/test_kodi_runtime.py tests/test_playback_arbiter.py tests/test_package.py` — 186 passed.
- Full verification: `pytest -q` — 906 passed in 97.41s; `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` passed; package creation and `tools/check_package.py` passed; `git diff --check` passed.
- External Kodi UI actions and other add-ons still cannot participate in the shared start lock; the five-second pending handoff cannot make those sources atomic with `Player.play`. No zero-race claim. Live Kodi UI behavior remains unverified.
