# Task 7 report — GUI package and flow verification

Status: automated/package/documentation scope complete, including one consolidated fix wave from the independent whole-branch review; live Kodi GUI acceptance unavailable and still outstanding under the plan's explicit fallback branch.

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
| `python3 -m pytest -q` | 928 passed in 104.01s; no failures; local HTTP-handler exception banner noted below |
| `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` | Exit 0, no output |
| `python3 tools/package_addon.py --output dist` | Exit 0; installable ZIP built |
| `python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip` | Exit 0, `Package OK` |
| ZIP manifest/member inspection and fresh extraction to `dist/checker-gui-final3/` | 36 approved files; both entry points; version 0.1.0; only xbmc.python 3.0.1 dependency |
| `.venv/checker/bin/kodi-addon-checker --branch omega --skip-dependency-checks dist/checker-gui-final3/plugin.audio.kodi_donor_radio` | Checker 0.0.36 exit 0: no add-on problems or warnings (dependency availability was checked on the preceding same-runtime package build) |
| `git diff --check` | Exit 0, no output |

The full run emitted `Exception occurred during processing of request from ('127.0.0.1', 60913)` and a separator banner without a retained traceback. The emitting test was not identified. All 929 tests passed, but the output was not pristine. The official checker separately emitted the known host `urllib3` warning that Python's LibreSSL 2.8.3 is outside its OpenSSL support range; the checker itself reported zero add-on warnings/problems.

The consolidated review-fix wave prevents preview creation from replacing active or pending deliberate relay sessions, passes Kodi's native WindowXML constructor arguments at allocation time, accepts both valid empty/trailing-slash plugin roots, and lets setup save truthful station areas when Radio Browser records omit city. The map rail now follows the already-moved Kodi selection and handles a non-wrapping edge; visible focused styling, fallback artwork, metadata, and localized map labels are covered. Regressions also cover the cleanup lock-failure cases noted by the scoped reviewer.

The one scoped re-review confirmed that all six important findings and the two cleanup notes were addressed. It identified one remaining copy issue: setup prompts still called fallback areas “cities.” These prompts now say “place”; the targeted setup/map suite passed again (181 tests).

A separate adversarial Steward pass then found that Kodi's native image loader could fetch an untrusted Radio Browser favicon URL directly, outside the add-on's URL policy. The list and map now use the bundled generic “DR” tile for all station artwork until the add-on can fetch images through bounded destination- and redirect-validated caching. A regression supplies a loopback favicon URL and confirms the native artwork field receives only the local monogram. Station-specific call-letter artwork remains a future target. The product spec records this first-slice boundary. After this change, the GUI/directory/package-focused suite passed (252 tests), the full suite passed (929 tests in 102.84s), compileall/package/archive checks passed, and Kodi checker 0.0.36 reported no add-on problems/warnings on the final archive (dependency check skipped for this last run; dependency availability passed on the immediately preceding package build).

Fresh Steward primary and adversarial passes at commit `aa42466e264cf1323febd7b31a0c13c289364729` found no code blocker and confirmed the private-artwork issue resolved. The primary pass noted the generic “DR” tile distinction now documented above. Reports: `/Users/hermes/.config/steward-os/reports/kodi-donor-radio-e2e/gui-aa42466-primary.md` and `gui-aa42466-adversarial.md`.

ZIP: `dist/plugin.audio.kodi_donor_radio-0.1.0.zip`, **433,800 bytes**, SHA-256:
`2d687f9c413dee6b1b334583d62a3be8f265499fa32d1a850089002883b16cb5`.

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
