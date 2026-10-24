# Task 7 report — GUI package and flow verification

Status: automated/package/documentation scope complete, including consolidated review fixes; live Kodi GUI acceptance unavailable and still outstanding under the plan's explicit fallback branch.

## Scope

- Strengthened `tests/test_package.py` against the actual generated ZIP: GUI toggle defaults and setup action, localized settings/map references, the exact 60 pin-control pool and station rail, profile/map/preview-arbiter modules, and fallback PNG. Added eight independently tampered archives that omit required GUI resources; each is rejected. Expanded exclusion inputs to include fixture HLS, development tools and hidden workflow files.
- The initial Task 7 work strengthened acceptance assertions for the implemented package. A subsequent independent whole-branch review exposed runtime and UI boundary defects; the single consolidated correction wave and regressions are recorded below.
- Updated README and the donor-radio product spec to describe the implemented first slice, UUID-only station actions, local setup and profile records, bounded discovery, settings defaults, deferred themed screens, and outstanding GUI acceptance.
- Added a current testing record and executable acceptance procedure. Updated Task 7 plan checkboxes and the SDD ledger; live Step 4 stays unchecked. Included the approved GUI design spec as directed by the controller.

## Fresh verification

Host: macOS ARM64; Python 3.9.6. Verification includes the consolidated corrections after Task 7's original package-only run.

| Command | Observed result |
| --- | --- |
| `python3 -m pytest tests/test_kodi_radio_map.py tests/test_kodi_runtime.py tests/test_sessions.py tests/test_relay.py -q` | 251 passed in 31.92s |
| `python3 -m pytest tests/test_package.py tests/test_kodi_runtime.py tests/test_kodi_radio_map.py tests/test_radio_map.py tests/test_radio_ui.py tests/test_playback_arbiter.py -q` | 256 passed in 8.88s |
| `python3 -m pytest -q` | Final GUI grid/performance follow-up: 957 passed in 104.76s |
| `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` | Exit 0, no output |
| `python3 tools/package_addon.py --output dist` | Exit 0; installable ZIP built |
| `python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip` | Exit 0, `Package OK` |
| ZIP manifest/member inspection and fresh extraction | 36 approved files; both entry points; version 0.1.0; only xbmc.python 3.0.1 dependency |
| `.venv/checker/bin/kodi-addon-checker --branch omega --skip-dependency-checks` on the fresh extraction | Checker 0.0.36 exit 0: no add-on problems or warnings (dependency availability was checked on the preceding same-runtime package build) |
| `git diff --check` | Exit 0, no output |

Earlier full runs emitted `Exception occurred during processing of request` for a loopback client, without a retained traceback. The newest 943-test run also printed this banner; no test failed. The official checker separately emitted the known host `urllib3` warning that Python's LibreSSL 2.8.3 is outside its OpenSSL support range; the checker itself reported zero add-on warnings/problems.

The final review follow-up also adds focused map textures with distinct Preview/Back colors, localizes the map title, switches profile file permissions to portable path-based chmod, snapshots the session ad reference once during cache retirement, selects smoke entries by exact fixture ID, makes the VAST macro assertion non-vacuous, and clarifies `radius_km` storage and miles conversion. Its focused regression set passed 116 tests. The earlier follow-up package was 439,141 bytes with SHA-256 `8a281b988801c505e1f6559501a54da3511f818c797b1da6e28f4002b472c8b6`.

The subsequent full-branch CodeRabbit pass found six additional minor/trivial issues; all six were verified and fixed. Explicit ICY-start suppression now expires after five minutes without inventing an end, bare URI-bearing HLS cues fail closed, shutdown reads the ad reference once, guided-setup cancellation/offline failure preserves the root listing, the package checker parses the map XML, and relay playlist observation is performed once per response. Focused tests passed 408; the full suite passed 943 in 104.10s. The run emitted a loopback HTTP-handler exception banner without a retained traceback. Compileall, package build/check, diff check, and Kodi checker 0.0.36 passed; the checker reported no add-on problems or warnings and emitted only the known host LibreSSL/urllib3 warning. The historical pre-map-context ZIP was 439,623 bytes with SHA-256 `15770f1b0a2102a7d6a47ad0d7739397271fc420f2db29390609e87f88892804`.

The later follow-up added labeled geographic regions and a 30° latitude / 60° longitude grid beneath the map pins, addressing the adversarial Steward finding that a blank panel gave no location context. It also corrected GUI acceptance links, service-pass player cleanup, the cancellation exception assertion, source-fetch deadlines, control-response normalization, and empty Radio Browser mirror retries. Focused tests passed 515; the suite at that point passed 948 in 104.03s. Compileall, deterministic package build/check, and Kodi checker 0.0.36 passed with no add-on problems/warnings (known host LibreSSL warning only). That ZIP was 36 files, 444,210 bytes, SHA-256 `ae43043b4642c447014e0b8f93f36d9072dd9384c9fe085f5289a17e774671b5`.

The final GUI review follow-up made the grid follow local station/place bounds, hides world-region labels outside the world view, handles antimeridian longitude without wrapping a gridline to the wrong side, and selects a grid for narrow views. A Steward follow-up also changed dense candidate filtering to count lines before allocating coordinates. Further fixes make the WindowXML white texture add-on-local, reuse its preview controller on repeated initialization, reuse discovered directory mirrors for preview, skip invalid remote tags, validate map localization, reject XML DTDs in package metadata, and preserve tag continuation when malformed upstream rows are skipped. The current review round's full suite passed 962 tests in 103.80s; compileall, package build/check, `git diff --check`, and Kodi checker 0.0.36 on a fresh extraction passed. Checker reported no add-on issues or warnings (the host LibreSSL warning is separate). Current ZIP: 37 files, 448,827 bytes, SHA-256 `450612c6f35ea80669e2cef81ef6baa81ef03f7d4e07eb7bb902f1d77f78b78a`.

The consolidated review-fix wave prevents preview creation from replacing active or pending deliberate relay sessions, passes Kodi's native WindowXML constructor arguments at allocation time, accepts both valid empty/trailing-slash plugin roots, and lets setup save truthful station areas when Radio Browser records omit city. The map rail now follows the already-moved Kodi selection and handles a non-wrapping edge; visible focused styling, fallback artwork, metadata, and localized map labels are covered. Regressions also cover the cleanup lock-failure cases noted by the scoped reviewer.

The one scoped re-review confirmed that all six important findings and the two cleanup notes were addressed. It identified one remaining copy issue: setup prompts still called fallback areas “cities.” These prompts now say “place”; the targeted setup/map suite passed again (181 tests).

A separate adversarial Steward pass then found that Kodi's native image loader could fetch an untrusted Radio Browser favicon URL directly, outside the add-on's URL policy. The list and map now use the bundled generic “DR” tile for all station artwork until the add-on can fetch images through bounded destination- and redirect-validated caching. A regression supplies a loopback favicon URL and confirms the native artwork field receives only the local monogram. Station-specific call-letter artwork remains a future target. The product spec records this first-slice boundary. After this change, the GUI/directory/package-focused suite passed (252 tests), the full suite passed (929 tests in 102.84s), compileall/package/archive checks passed, and Kodi checker 0.0.36 reported no add-on problems/warnings on the final archive (dependency check skipped for this last run; dependency availability passed on the immediately preceding package build).

Fresh Steward primary and adversarial passes at commit `aa42466e264cf1323febd7b31a0c13c289364729` found no code blocker and confirmed the private-artwork issue resolved. The primary pass noted the generic “DR” tile distinction now documented above. Reports are saved in the local Steward report directory. For `b612c49873cfa4597b3832ffab1a3ae012367fcb`, primary found no blocker while adversarial identified the map's missing geographic context; the grid and region labels address that finding. A fresh Steward review is required for the latest follow-up commit.

## Live acceptance unavailable

Kodi 21.3 Omega is installed. A fresh Task 7 `cua.getState()` check returned an empty native-app inventory and this explicit error: “The Mac is locked and automatic unlock could not unlock it.” Interactive Kodi acceptance could not proceed. Display resolution, input method, and observed focus behavior: **not observed**.

Still unverified on this GUI package: ZIP install dialog; Estuary and alternate-skin root/nested rendering; setup cancellation/completion; real settings interaction; map launch from root and station menus; pin/rail/control D-pad reachability and non-wrapping; return/focus restoration; real station/fixture playback and audible output; deliberate Play click count; preview debounce/no-click/cancellation/cleanup and competing playback. Fake adapters and pure geometry tests do not establish any of these live outcomes. Prior playback evidence in `docs/testing.md` predates this GUI and is kept as historical evidence only.

## Limits retained

- Live Radio Browser discovery and strict UUID-only routes; bounded profile-safe place/favorite records without station stream URLs or service credentials.
- Native setup uses bounded station-derived location candidates from at most four pages. City is optional; state or coordinates are explicitly labeled as a station area, not a verified city. Radius choices 10/25/60/100/250 miles convert once to kilometers, and saves remain transactional. Place filtering uses bounded country pages and local radius checks. Map seed/pin/rail caps are 200/60/50.
- Themed Home/Now playing/other themed screens, interface/theme-open selectors, separate history and full target map/artwork remain deferred.
- Add-on direct starts share a profile lock and persisted intent tokens. External Kodi UI actions, other add-ons, and native `setResolvedUrl` are outside that arbiter and can race. Pending preview acknowledgment is bounded to five seconds; a later start may fail. No atomic Kodi-wide compare-and-play guarantee.
- Exact loopback-only fixtures, observation-only ICY, and current ad eligibility/beacon behavior preserved; production ad timing/audio limitations remain in the existing validation record.
- Steward was re-run and both primary/adversarial local passes are saved for `aa42466e264cf1323febd7b31a0c13c289364729`; CodeRabbit review is still running for the preceding code commit and must be refreshed against the current head. No push or PR modification has occurred.

Commit message: `test: verify Kodi radio GUI package and flows`.
