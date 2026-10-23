# Task 7 report — GUI package and flow verification

Status: automated/package/documentation scope complete; live Kodi GUI acceptance unavailable and still outstanding under the plan's explicit fallback branch.

## Scope

- Strengthened `tests/test_package.py` against the actual generated ZIP: GUI toggle defaults and setup action, localized settings/map references, the exact 60 pin-control pool and station rail, profile/map/preview-arbiter modules, and fallback PNG. Added eight independently tampered archives that omit required GUI resources; each is rejected. Expanded exclusion inputs to include fixture HLS, development tools and hidden workflow files.
- These are acceptance assertions for the already implemented package. No runtime or packaging-production code needed changing; no new production behavior or TDD red/green cycle is claimed in this task.
- Updated README and the donor-radio product spec to describe the implemented first slice, UUID-only station actions, local setup and profile records, bounded discovery, settings defaults, deferred themed screens, and outstanding GUI acceptance.
- Added a current testing record and executable acceptance procedure. Updated Task 7 plan checkboxes and the SDD ledger; live Step 4 stays unchecked. Included the approved GUI design spec as directed by the controller.

## Fresh verification

Host: macOS ARM64; Python 3.9.6. Runtime source unchanged from reviewed Task 6 HEAD `d8456bc`.

| Command | Observed result |
| --- | --- |
| `python3 -m pytest tests/test_package.py tests/test_kodi_runtime.py tests/test_kodi_radio_map.py tests/test_radio_map.py tests/test_radio_ui.py tests/test_playback_arbiter.py -q` | 249 passed in 9.17s; clean output |
| `python3 -m pytest -q` | 920 passed in 103.55s; no failures; local HTTP-handler exception banner noted below |
| `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` | Exit 0, no output |
| `python3 tools/package_addon.py --output dist` | Exit 0; installable ZIP built |
| `python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip` | Exit 0, `Package OK` |
| ZIP manifest/member inspection and fresh extraction to `dist/checker-gui-task7/` | 36 approved files; both entry points; version 0.1.0; only xbmc.python 3.0.1 dependency |
| `.venv/checker/bin/kodi-addon-checker --branch omega dist/checker-gui-task7/plugin.audio.kodi_donor_radio` | Checker 0.0.36 exit 0: no add-on problems or warnings |
| `git diff --check` | Exit 0, no output |

The full run emitted `Exception occurred during processing of request from ('127.0.0.1', 59190)` and a separator banner without a retained traceback. This resembles the previously documented local HTTP-handler output; the emitting test was not identified in this run. All 920 tests passed, but the output was not pristine. The official checker separately emitted the known host `urllib3` warning that Python's LibreSSL 2.8.3 is outside its OpenSSL support range; the checker itself reported zero add-on warnings/problems.

ZIP: `dist/plugin.audio.kodi_donor_radio-0.1.0.zip`, **427,329 bytes**, SHA-256:
`bdc73eaedab1602445b8498df2b5c73de5e57e9fa7ad7c0c4471b3f8d42c8734`.

## Live acceptance unavailable

Kodi 21.3 Omega is installed. A fresh Task 7 `cua.getState()` check returned an empty native-app inventory and this explicit error: “The Mac is locked and automatic unlock could not unlock it.” Interactive Kodi acceptance could not proceed. Display resolution, input method, and observed focus behavior: **not observed**.

Still unverified on this GUI package: ZIP install dialog; Estuary and alternate-skin root/nested rendering; setup cancellation/completion; real settings interaction; map launch from root and station menus; pin/rail/control D-pad reachability and non-wrapping; return/focus restoration; real station/fixture playback and audible output; deliberate Play click count; preview debounce/no-click/cancellation/cleanup and competing playback. Fake adapters and pure geometry tests do not establish any of these live outcomes. Prior playback evidence in `docs/testing.md` predates this GUI and is kept as historical evidence only.

## Limits retained

- Live Radio Browser discovery and strict UUID-only routes; bounded profile-safe place/favorite records without station stream URLs or service credentials.
- Native setup uses station-derived city candidates from at most four pages, radius choices 10/25/60/100/250 miles converted once to kilometers, and transactional final save. It is not a complete city catalog. Place filtering uses bounded country pages and local radius checks. Map seed/pin/rail caps are 200/60/50.
- Themed Home/Now playing/other themed screens, interface/theme-open selectors, separate history and full target map/artwork remain deferred.
- Add-on direct starts share a profile lock and persisted intent tokens. External Kodi UI actions, other add-ons, and native `setResolvedUrl` are outside that arbiter and can race. Pending preview acknowledgment is bounded to five seconds; a later start may fail. No atomic Kodi-wide compare-and-play guarantee.
- Exact loopback-only fixtures, observation-only ICY, and current ad eligibility/beacon behavior preserved; production ad timing/audio limitations remain in the existing validation record.
- No Steward/CodeRabbit invocation, push, PR creation or PR modification performed.

Commit message: `test: verify Kodi radio GUI package and flows`.
