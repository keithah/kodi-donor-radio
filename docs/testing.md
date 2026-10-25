# Validation record

## Playback arbiter GUI follow-up — 2026-10-24

The Kodi preview stop path now marks its playback intent and releases the shared
profile lock before calling `Player.stop()`. Add-on starts wait for that bounded
handoff, while GUI-thread intent checks remain responsive. Abandoned stop markers
expire after one minute using a cross-process monotonic clock; future-dated markers
are stale after a reboot. Regressions verify a blocked stop cannot hold the profile
lock or race a new playback start, and wall-clock changes do not expire an active
marker.

Closing an ad session suppresses late VAST preparation-error callbacks. Station
resolution now trusts a concrete audio MIME type and binary sniff over a playlist-like
URL suffix. ICY learned-marker lookups use immutable snapshots and do not wait for
profile writes. The README and radio-browser spec now use the actual Settings label and
accurately distinguish stream resolution from click counting. Setup asks for a
country before either browsing or filtering a free-text place query, so search is
scoped to that country rather than the global top 200 stations. HLS line parsing
splits only at LF/CRLF so Unicode separators inside EXTINF titles cannot desynchronize
the relay parser from Kodi. The repository tree separates the packaged add-on from
root-level development tests and tools. Focused playback/HLS/marker/resolution checks
passed **205 tests**; the full suite passed
**1,040 tests in 116.51s**. `compileall`, package build, `check_package.py`, and
`git diff --check` passed. Kodi add-on checker 0.0.36 on a fresh ZIP extraction
reported no add-on problems or warnings with dependency checks skipped; its host
Python emitted the known LibreSSL/urllib3 compatibility warning. The ZIP has
**37 files**, **457,688 bytes**, mode **0644**, SHA-256
`b999d864219e1d8d9e1fb0fb7f9601c0b196413ecd5dc4994b80a8a6d889d75d`.
This was the initial GUI attempt. A later Kodi GUI follow-up is recorded below;
this paragraph does not describe that later partial acceptance.

## Final review follow-up — second pass — 2026-10-24

A fresh deep CodeRabbit review found that unreadable place/favorites files could
hide GUI navigation, IPv6 reserved or multicast destinations could pass the URL
policy, fixture beaconing defaulted to enabled without explicit session opt-in,
and creative-preparation failures did not retain resolved VAST Error URLs. These
paths now degrade safely or fail closed, with regression tests. Test-origin
client disconnects are also silenced without hiding unrelated handler errors,
and the VAST checkpoint now documents bounded media retention accurately. Map
and station-info screens also tolerate unreadable favorites/places. The
directory smoke probe accepts ICY interval zero as unframed audio, while
rejecting malformed interval values. Several threaded and timing tests now
assert their outcomes outside worker callbacks and avoid read-scheduling flakes.
Favorites and saved places now lock read-modify-write across Kodi invocations,
and the five-minute marker limit applies only to ad-start markers, not long
song or end-break durations. Fixture-only beacons are experimental; production
stations remain unmapped until the absolute presentation clock is validated.

The package and HLS regression set passed **141 tests**; the full suite passed
**1,029 tests in 112.69s**. Compileall,
`git diff --check`, package creation, and `check_package.py` passed. Kodi add-on
checker 0.0.36 on a fresh archive extraction reported no problems or warnings;
its environment emitted the known LibreSSL/urllib3 compatibility warning. The
ZIP has **37 files**, **455,598 bytes**, mode **0644**, SHA-256
`6d70b23769c9be458660c12c505351deb7adf72601544e9faa467c7c433e2baf`.
Steward and CodeRabbit must run again against the exact committed revision
before updating the PR branch. Interactive Kodi GUI acceptance remains pending
because the desktop is locked.

## Final review follow-up — first pass — 2026-10-24

A fresh deep CodeRabbit review on the preceding committed GUI branch identified
map-station admission, unreadable marker-store preservation, source-cache
retention, VAST assertion specificity, ICY metadata callback isolation, nested
playback-arbiter locking, cancelled playlist responses, and stale specification
details. The fixes now filter unsafe map rows before clustering, preserve unread
marker files, expire inactive old routes under byte/segment pressure, assert
specific VAST errors, keep observer failures from discarding audio, take the
cross-process lock only at the outermost reentrant arbiter call, and answer
cancelled playlist requests with 404. ICY docs now distinguish parser signals
from unresolved events and define exact normalized marker identity. The PR
source branch was independently verified as `codex/vast-controlled-ads`;
Kodi desktop acceptance remains pending while the Mac is locked.

After these changes, `python3 -m pytest -q` passed **1,003 tests in 108.14s**.
`compileall`, `git diff --check`, package build, `check_package.py`, and the
official Kodi add-on checker 0.0.36 on a fresh extraction passed. The checker
reported no add-on problems or warnings; Python emitted only the host LibreSSL
2.8.3 / urllib3 compatibility warning. The package has **37 files**, **452,897
bytes**, mode **0644**. Its previously recorded checksum had an invalid length;
the original archive is no longer available to recompute it.
Steward and CodeRabbit must be rerun on the final committed revision before the
PR branch is updated.

## GUI runtime edge verification — 2026-10-24

The consolidated final fix wave separates passive Kodi pin notifications from
explicit focus requests, restores rail endpoints after Kodi's native list wraps,
and preserves/selects a context station within the 50-row rail. Network failures
have distinct localized states and retain valid selected/cached display records.
Optional origin-query rejection falls back to a validated home route, and preview
cleanup now runs after native modal return or abort. The settings scope statement
matches the approved slice. A zero ICY interval forwards unframed audio unchanged;
positive interval parsing, ad eligibility, and beacon behavior are preserved.

The focused map/runtime/relay set passed **282 tests in 34.53s** with clean output.
The full `python3 -m pytest -q` run passed **995 tests in 107.56s**. It emitted the
previously documented intermittent loopback HTTP-handler exception banner. A
final `python3 -m pytest -q -s` diagnostic run passed **995 tests in 107.71s** and
captured the traceback: `BrokenPipeError: [Errno 32] Broken pipe` at the shared
transport-test origin's `tests/test_transport.py:96` body write after its client
closed. No test failed; full-suite output is not claimed to be pristine. The
diagnostic log is `/tmp/kodi-final-fix-pytest-full.log`.

Compileall, package build, `check_package.py`, and `git diff --check` exited 0.
Kodi add-on checker **0.0.36**, invoked with `--branch omega` on the fresh ZIP
extraction in `dist/checker-gui-final-CECTfo/`, exited 0 and reported **no add-on
problems or warnings**. Its Python environment emitted the known host LibreSSL
2.8.3 / urllib3 warning. The final ZIP has **37 files**, **450,757 bytes**, mode
**0644**, and SHA-256
`681ea71ef5cc9312d19bad51ff2d406b91c308734ad92af2b9d2a2c22d6aa9e1`.

Kodi GUI acceptance remains unverified because the desktop is locked. Queued
focus and native movement-before-callback fakes cover the reported ordering;
they do not establish live rendering, focus delivery, audible output, or setup
interaction. The live acceptance table below remains pending.

## GUI package verification — initial Task 7 run, 2026-10-23

The first GUI slice implements native skin listings, saved places/favorites,
sequential native setup, station/place actions, and one shared schematic map.
Themed Home/Now playing/other themed windows and their interface selectors remain
deferred. This initial record covers the package built at that point; earlier Kodi playback
records below predate these changes and do not establish GUI acceptance.

Development verification used macOS ARM64 and Python 3.9.6. The focused GUI and
package command was:

```sh
python3 -m pytest tests/test_package.py tests/test_kodi_runtime.py tests/test_kodi_radio_map.py tests/test_radio_map.py tests/test_radio_ui.py tests/test_playback_arbiter.py -q
```

It passed **249 tests in 9.17s**, with clean output. The full suite passed
**920 tests in 103.55s**. That full run emitted the previously documented local
HTTP-handler exception banner (`Exception occurred during processing of request`
for a loopback client), without a retained traceback; no test failed, but the
output was not pristine. The exact emitting test was not established in this run.

Archive-level assertions
check the three GUI toggle defaults and setup action, settings/map localization
references, all 60 pin IDs and the station rail, profile/map/arbiter modules,
and the fallback PNG. Eight tampered archives each omit a different required
GUI resource and are rejected. Unlisted fixture manifests/audio, development
tools, hidden workflow files, secrets and caches are excluded. No runtime code
was changed during this verification task.

Build and validate from the repository root:

```sh
python3 -m pytest -q
python3 -m compileall -q plugin.audio.kodi_donor_radio tests
python3 tools/package_addon.py --output dist
python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip
git diff --check
python3 -m zipfile -e dist/plugin.audio.kodi_donor_radio-0.1.0.zip dist/checker-gui-task7
.venv/checker/bin/kodi-addon-checker --branch omega dist/checker-gui-task7/plugin.audio.kodi_donor_radio
```

The official checker is a development-only installation; it is not required by
the add-on. Version **0.0.36** exited 0 and reported **no add-on problems and no
warnings** on the fresh ZIP extraction. Its Python environment separately emitted
the known `urllib3` warning about host LibreSSL 2.8.3. Compilation, package build,
and the repository package check exited 0. The inspected ZIP contains **36 files**,
**427,329 bytes**, one `plugin.audio.kodi_donor_radio/` root, version **0.1.0**,
both plugin/service entries, and only the `xbmc.python` **3.0.1** dependency.
SHA-256: `bdc73eaedab1602445b8498df2b5c73de5e57e9fa7ad7c0c4471b3f8d42c8734`.

### Live GUI acceptance remains unavailable

Kodi **21.3 Omega** is installed. A fresh Task 7 `cua.getState()` check returned
no native apps and reported: “The Mac is locked and automatic unlock could not
unlock it.” No interactive acceptance was performed for this
GUI build. Display resolution and input method were not observed; no focus
behavior was observed. Automated Kodi adapters and geometry tests do not prove
real rendering, audio, focus, or click delivery.

| Check on this GUI build | Status |
| --- | --- |
| ZIP install dialog; Estuary root/nested listings | Unverified |
| Alternate-skin root/nested listings and shared map rendering | Unverified |
| Setup cancellation/completion and settings interaction | Unverified |
| Root/context map launch and return to the originating selection | Unverified |
| Pin/rail/control D-pad reachability, non-wrapping and focus restoration | Unverified |
| Real station and controlled-fixture playback; audible output | Unverified |
| One Radio Browser click per deliberate Play; no preview clicks | Unverified live; adapter tests cover the dispatch boundary |
| Preview debounce, cancellation, session retirement and competing playback | Unverified live; deterministic adapter tests cover ordering |

When the desktop is accessible, install the ZIP in a backed-up isolated profile
and record exact Kodi version, display resolution, active skin and input device.
Exercise root and nested listings in Estuary and one alternate skin; run setup
from both entry points, cancel at each stage, save multiple places and change
their radius/default. Open the map from the root and station menu, move through
pins/rail/Preview/Back, check no directional wrap and record the restored listing
selection. Test real and loopback fixture playback, deliberate click count,
400 ms preview debounce, rapid focus changes, Back during pending lookup,
preview disabled, and competing playback. Capture observed focus defects and
heard audio separately from player-clock or request evidence.

Setup candidates and map pins come from a bounded four-page station scan,
not a complete city catalog. Place filtering uses local radius checks against
bounded country pages. Map limits are 200 seed rows, 60 pins and 50 rail rows.
Preview direct starts and add-on direct Play starts share a profile file lock
and start-intent token; ownership-aware cancellation retires only the preview's
relay. External Kodi UI actions, other add-ons, and native `setResolvedUrl` do not
participate in that lock and can still race. The pending preview handoff is
bounded to five seconds; a later Kodi acknowledgment can fail. There is no atomic
Kodi-wide compare-and-play guarantee. Profile safety, UUID-only routes, live
Radio Browser discovery, exact loopback fixtures and ICY/ad behavior remain
covered by the full automated suite.

### Consolidated review follow-up — 2026-10-23

The follow-up review fixes preserve Kodi's required native WindowXML allocation,
prevent preview preparation from replacing an active deliberate session, and
keep artwork local. The additional UI and portability fixes give map pins and
Preview/Back obvious focused textures, use localized map-title text, apply
portable path-based profile-file permissions, and read the session's ad object
once during cache retirement. The smoke test now identifies controlled entries
by the add-on's exact fixture ID instead of parsing an assumed embedded station
object; VAST macro coverage verifies that its error-URL assertion has a fixture
to inspect. The spec now records the actual `radius_km` storage field and miles
conversion.

Verification on this follow-up: **116 targeted tests passed** and the full suite
passed **937 tests in 103.37s** with clean pytest output. `compileall`, archive
creation, `check_package.py`, and `git diff --check` passed. Kodi add-on checker
0.0.36 reported no add-on problems or warnings; its Python environment emitted
the known host LibreSSL/urllib3 warning. The fresh archive contains 36 files,
is 439,141 bytes, and has SHA-256
`8a281b988801c505e1f6559501a54da3511f818c797b1da6e28f4002b472c8b6`.

Kodi GUI acceptance is still unavailable because the desktop is locked. These
automated checks do not establish live rendering, D-pad focus, click delivery,
playback audio, or interactive setup behavior.

### CodeRabbit follow-up fixes — 2026-10-23

The follow-up addressed six CodeRabbit findings across the full branch. An
unmatched explicit ICY start now releases duplicate suppression after five
minutes without inventing an end event. Bare URI-bearing HLS cue-out values
fail closed. Session shutdown snapshots the ad reference once; guided setup
cancellation or network failure still renders the normal local root menu; the
package checker parses the shared map XML; and each fetched HLS playlist is
observed once. Regression coverage exercises each case.

The focused regression set passed **408 tests**. The full suite passed **943
tests in 104.10s**; pytest printed a loopback HTTP-handler exception banner
without a retained traceback, as seen in earlier runs. Compileall, package
creation, `check_package.py`, and `git diff --check` passed. Kodi add-on checker
0.0.36 reported no add-on problems or warnings; its environment emitted the
known host LibreSSL/urllib3 warning. The package has SHA-256
`15770f1b0a2102a7d6a47ad0d7739397271fc420f2db29390609e87f88892804`.

Live Kodi interface acceptance remains pending while the Mac desktop is
locked.

### Map context and final review follow-up — 2026-10-23

An independent review noted that the first schematic map showed pins against a
blank panel. The shared world map now draws 30-degree latitude and 60-degree
longitude grid lines and labels the world regions beneath the coordinate pins;
local station and place views choose a readable grid from their own bounds and
hide the world-region labels. The footer identifies the selected view and says
that it is schematic and not to scale. The map remains artwork-independent
and the pins continue to use the directory's station coordinates. Also
corrected the acceptance-record links, service-pass player
cleanup, the cancellation exception assertion, total source-segment fetch
deadlines, HTTP/JSON control-response normalization, and retries after empty
Radio Browser mirror discovery.

At this 2026-10-23 checkpoint, the full suite passed **957 tests in 104.76s** after adding world/local/dateline
grid-rendering, density-count, package-localization, repeated-initialization,
invalid-tag, and preview-mirror-reuse cases; compileall, package creation,
`check_package.py`, and `git diff --check` passed. Kodi add-on checker 0.0.36
reported no add-on problems or warnings, with only its environment's known
LibreSSL/urllib3 warning. That checkpoint ZIP has **37 files**, is **448,136 bytes**,
and has SHA-256
`7115aa80854501daf9ad0c4f9ab3b2f043428ad3d642aaed4cf1659acb98fd1c`.
Kodi's actual visual acceptance remains pending because the desktop is locked.

### Final GUI review fixes — 2026-10-24

Tag browsing now keeps its next-page row when malformed Radio Browser rows are
filtered, place scans only show the bounded-scan notice when no results were
found, and cache eviction no longer inverts the session/ad-session lock order.
The package checker rejects XML DTD/entity declarations, and package rejection
tests assert their specific reasons. The ZIP publisher sets the final artifact
mode to `0644` before atomic rename. Failures while cleaning an abandoned ad
cache are non-fatal to ordinary radio-service startup. Redirected VAST manifests
resolve relative segments from the final manifest URL, cached media routes are
excluded from unrelated resource eviction, and the source deadline test uses an
isolated monotonic clock. Both profile-file locking paths share one helper.

The final suite passed **966 tests in 104.43s**. Compileall, package build and
internal package validation passed. Kodi add-on checker 0.0.36 on a fresh ZIP
extraction reported no add-on problems or warnings; its host emitted the known
LibreSSL/urllib3 warning. The current ZIP has **37 files**, is **448,567 bytes**,
mode `0644`, and has SHA-256
`0dae8fbe601e48de4a64973a81b24a03d75377d4fa04bfa5df4fe6e7ec421475`.
Live Kodi interface acceptance remains pending because the Mac desktop is locked.

## Controlled VAST acceptance, 2026-10-22

Target: unpatched Kodi `23.0-ALPHA1` Git `434da3c` on macOS 26.6.2 arm64, using an isolated portable profile. The pre-test profile was backed up outside the repository. Reports named `task8-*.json` in the test report directory contain the raw JSON-RPC samples and fixture counters without relay session URLs or tokens. The audible signal was not captured or confirmed by listening.

The original six-segment live fixture stopped producing new media after sequence 005. Kodi then plateaued near `getTime=1.35s` for 16-second runs, including direct source playback without an ad. A finite `#EXT-X-ENDLIST` control with the same AAC files advanced to 9.95 seconds in ten seconds; the composed finite relay advanced across both ad discontinuities and ended near its 16.042-second duration. The fixture was therefore extended to 24 contiguous-PTS source segments, with a six-segment live window driven by monotonic media time rather than manifest request count. Direct Kodi playback of this repaired source advanced to 16.34 seconds in 18 seconds. See `task8-true-live-direct-kodi.json` and `task8-finite-relay-kodi.json`.

On the repaired fixture, `tools/smoke_ad_relay.py` independently decoded every published segment in five runs. The combined preroll/midroll result was replacement twice, programme A twice, replacement twice, programme B twice; preroll-only, no-fill, and missing media preserved their expected source sections; wrong-duration VAST used the validated 4.010667-second AAC media duration rather than its nine-second declaration. Each segment decoded to 94 AAC frames/96,256 samples, and no original break leaked into the successful replacement trace. `task8-relay-smoke-final.json` records sample boundaries and collector counts. The relay-only checker rejects every success beacon because decoded media and HTTP requests do not establish playback evidence, even if a trace claims a mapping.

The actual addon service was then run against the repaired loopback origin in the backed-up portable profile. Normal, advanced-window, and mutation entries all opened, requested new source segments through at least sequence 012, and advanced Kodi's player clock to roughly 14.6–14.8 seconds over 16 seconds. A normal-to-mutation rapid switch reset the player clock and the new stream continued advancing. Stop succeeded and left collector success beacons empty. The later identity-gated rerun is `task8-addon-identity-kodi.json`; it compares Kodi’s active item file with the requested addon item and requires bounded clock advancement.

Kodi JSON-RPC `Player.PlayPause` and `Player.Seek` returned `Failed to execute method` for the active live HLS player (`canseek=false`). `Input.ExecuteAction(pause)` returned OK but the clock advanced by about one second during the following wall second; it did not pause playback. In the isolated delayed-segment run, the addon-owned player requested the delayed `source006.ts` and its clock held at 1.370 seconds from wall 3.026–8.424 seconds while `Player.Caching=false`. It then jumped from 1.370 to 7.770 seconds between adjacent 260 ms samples as `Player.Caching` became true; caching stayed true until wall 10.756 seconds and cleared by 11.011 seconds. `cachepercentage` remained 100 and speed remained one. The earlier run showed a similar 6.441-second jump; see `task8-buffer-stall-caching-kodi.json` and `task8-buffer-stall-kodi.json`. Normal live playback also showed up to 1.214-second jumps after short plateaus. The jump crosses the preroll completion boundary at 4.011 seconds without proof that those samples were presented, so a caching-only gate is insufficient. The absolute composed-media epoch at a sliding-window start is not exposed by Kodi, and segment requests show availability rather than decoded position. This is why the initial Kodi run did not emit success tracking. The fixture-only raw-clock mapper and its observed stall behavior are recorded below; production mapping still lacks a verified absolute anchor. The acceptance milestone remains incomplete pending anchor and rendered-audio verification. Missing/early-end playback remains unverified on target Kodi.

After the user approved a fixture-only mapper, the adapter mapped raw `getTime()` only when test mode supplied an exact `127.0.0.1` origin and the session had ads enabled. Ordinary station sessions received no mapper. A fresh five-scenario Kodi run delivered impression/start/quartile/completion events on advancing fixture playback; the delayed stall emitted impression/start/firstQuartile before the long jump, with no midpoint, third-quartile, or completion across it. Its report is `task8-fixture-tracking-enabled-kodi.json`.

The 2026-10-23 candidate anchor now treats the composed session's sample zero as a fixed origin, requests `EXT-X-START:TIME-OFFSET=0,PRECISE=YES` on the initial ad-enabled manifest, and never resets its mapping to a later HLS window. Seek or backward Kodi-clock reset disables mapping for the rest of that session. A forward ambiguous clock jump, or a stationary clock lasting at least 750 ms, permanently taints only the placements it intersects; later untouched placements can be tracked after their own continuous start. Unit tests cover these rules. A fresh Kodi run of this candidate anchor has not yet been made, so stable mapping is not yet target-verified; audible output and missing/early-end playback also remain outstanding. Ordinary Radio Browser sessions continue to have no ad mapper.

Kodi's log also exposed an invalid empty `ad_vast_url` default. The setting now explicitly allows empty values; a fresh addon invocation on the target no longer logged that warning. Advertising remains opt-in through test mode, an enabled ad choice, and an explicit fixture URL.

## Runtime target and requirements

Target: **Kodi 21.3 Omega**, the latest stable release found in the official
[release directory](https://mirrors.kodi.tv/releases/windows/win64/) on
2026-10-21. Kodi 22.0 Piers RC1 is a release candidate, not the stable target.
The minimum declared dependency is **`xbmc.python` 3.0.1**, as supplied by the
[21.3 source metadata](https://github.com/xbmc/xbmc/blob/21.3-Omega/addons/xbmc.python/addon.xml).
This is a Kodi addon API version, not an embedded CPython version.

**Tested Kodi runtime: 21.3 Omega**, revision `20251031-a3a448d26b`,
macOS 26.6.2 (25G83), ARM64, embedded CPython **3.11.7**. A process-local USB
restriction was required to complete Kodi startup;
see the runtime record below. All eight controlled streams produced advancing
Kodi player time. HLS cue schedules matched exactly. Audible output was not
independently confirmed; other Kodi platforms remain untested.

The same addon registers the music plugin and service. Official 21.3 sources
verify the [audio plugin extension](https://github.com/xbmc/xbmc/blob/21.3-Omega/addons/xbmc.python/pluginsource.xsd),
[service extension and modern settings format](https://github.com/xbmc/xbmc/tree/21.3-Omega/addons/service.xbmc.versioncheck),
[directory and resolved URL calls](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/ModuleXbmcplugin.h),
[Monitor abort/settings callbacks](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Monitor.h),
[player callbacks](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Player.h),
and [`xbmcvfs.translatePath`](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/ModuleXbmcvfs.h).
Addon metadata continues to identify this as an experimental POC.

## Automated adapter checks

Run `python3 -m pytest tests/test_kodi_runtime.py tests/test_relay.py tests/test_sessions.py -q`
for Kodi navigation/lifecycle and real loopback relay checks. Run
`python3 -m pytest -q` for the full suite. No Kodi fake modules ship in the addon.

Checks cover search/country/tag pages, cancellation and localized failures,
click reporting once per ordinary station selection, opaque loopback playback
URLs, unavailable service and startup deadline, late create cancellation,
restricted atomic descriptor publication and stale-instance-safe deletion,
player callbacks, actual station replacement and stale control token rejection,
settings restart/recovery, opt-in diagnostics and exact fixture-origin policy.
Profile modes 0700 and descriptor mode 0600 are checked on POSIX. Windows uses
its native file locking; Windows ACL behavior has not been runtime-tested.

## Controlled Kodi smoke procedure

Generate authored tone assets and start the developer fixture server as described
in [fixture instructions](../tools/README-fixtures.md). Configure **Enable test
mode** and set the exact origin, for example `http://127.0.0.1:8765`, matching the
server port. Only numeric `127.0.0.1`, HTTP and an explicit port are accepted;
no path, trailing slash or other local address is allowed. Other loopback ports
and private network targets remain blocked. Test mode and diagnostic logging
both default to disabled. Service restart applies setting changes and retires
old playback sessions.

In Kodi 21.3, install the packaged ZIP, verify service startup,
browse/search stations, and play MP3, AAC, ICY and each HLS fixture. Confirm heard
audio, inspect sanitized logs against expected cue schedules, switch stations
rapidly, stop playback, restart the service, and quit Kodi. Record the exact Kodi
build, OS, installation outcome, audio behavior and cleanup timings. Verify
continuous playback when Kodi sends HEAD, Range or repeated GET requests: the
relay returns metadata-only 200 for HEAD on continuous streams and playlists
(405 for segment/map HEAD), accepts `Range: bytes=0-` with HTTP 200 for
continuous streams and rewritten manifests, and returns 416 for other seeks on
those resources. Segment/map byte-range semantics are preserved. Only one
continuous consumer is permitted. The adapter disables Kodi content lookup because it performs an extra disposable
GET that consumes the continuous stream. The control response provides the MIME
type from the resolved upstream response, never directory codec/HLS hints.
Kodi playback was verified with this behavior; see the runtime record below.

Cancellation returns control to the Kodi UI promptly and stops a session when a
late create response arrives. An in-flight service resolver is bounded by its
network limits; if the control connection times out before returning a session
ID, an unclaimed session expires after 120 seconds. Kodi player callbacks do
not identify sessions: tracked media URLs and matching session IDs prevent an
old stop event from cancelling a newer session. If a startup error arrives before
Kodi exposes the media URL, idle expiry provides final cleanup.

No cues observed means no cues observed; it does not prove that no advertisements
exist. Observation time differs from the listener's audible playback time.

## Historical pre-Kodi package and live verification — 2026-10-21

Development host: **macOS 26.6.2, build 25G83**, arm64; Python **3.9.6**.
No `kodi` command or `/Applications/Kodi.app` is installed. No Kodi installation, Kodi
audio playback or audible tone test was performed. The separate FFmpeg decode
smoke below verifies synthetic audio through the relay. The target remains Kodi 21.3;
**actual tested Kodi/embedded Python version: none**.

Reproduce the source-only distribution from the repository root:

```sh
python3 -m pytest -q
python3 tools/package_addon.py --output dist
python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip
```

The packaging test suite verifies the single addon root, both entry points,
metadata/API dependency, complete GPL v2 text, localization/artwork, approved
contents, 0644 regular files and byte-for-byte reproducibility despite source
mtime/mode changes. Tampered ZIP checks cover missing sources, invalid XML,
identity/license mismatch, missing localization, invalid artwork, secrets, extra
roots, traversal, executable members, symlinks and duplicates. No tests, generated
fixture audio, development tools, caches or virtual environments ship in the ZIP.
Final full suite: **321 passed in 33.75s** (Python 3.9.6; final fix wave).
Final package creation and
`check_package.py` both exited 0. Artifact:
`dist/plugin.audio.kodi_donor_radio-0.1.0.zip`; SHA-256:
`df35c648e1fe3bd478213ab91c80d640699869b1e71a6ef846551284070bf182`.

The [official Kodi addon checker](https://github.com/xbmc/addon-check) documents
installation with `pip install kodi-addon-checker` and invocation against an addon
directory. Its current CLI also requires the target branch. Installed locally in
the ignored `.venv/checker/` directory; no application or checker was installed
globally. Exact version command and package-validation invocation:

```sh
.venv/checker/bin/kodi-addon-checker --version
# kodi-addon-checker 0.0.36
.venv/checker/bin/kodi-addon-checker --branch omega dist/checker/plugin.audio.kodi_donor_radio
```

`dist/checker/plugin.audio.kodi_donor_radio` is extracted from the final ZIP, not
a different source tree. The first run exited 0 and reported no problems and no
warnings; icon 512×512, JPEG fanart 1280×720 and PO validation passed. The final
run after the final protocol/cue compatibility fixes also completed successfully
with
`We found no problems and no warnings, please enjoy your day.` The host emits a
separate urllib3 warning because system Python uses LibreSSL 2.8.3; this is a
checker-development-environment warning, not an addon runtime result. The checker
is static validation and cannot establish Kodi playback compatibility.

### Final protocol regression and decoder verification

Initial real FFmpeg requests failed all three fixtures with HTTP 416 because its
normal initial `Range: bytes=0-` probe was rejected. The relay now ignores that
full-body probe for continuous sources and rewritten manifests, returning 200.
It reuses the original continuous probe connection. Unsupported seeks remain
416; segment/map ranges still pass upstream and retain 206/416 validation.

The next decode exposed FFmpeg 9.0.1's `allowed_segment_extensions` rejection of
extensionless opaque HLS routes. Known playlists now use an opaque ID plus
`.m3u8`. Media routes preserve only an allowlisted path extension (`ts`, `aac`,
`mp3`, `mp4`, `m4s`, `m4a`, `ac3`, `ec3`); no upstream names, query values or URLs
are embedded. Exact registered route matching rejects modified/missing suffixes.
Extensionless and unknown origin segment extensions remain opaque; strict
clients can reject these, and no codec/extension guess is made before fetching.
The Kodi boundary recognizes the same safe suffixes for resolution and cleanup.

Reproduce after audio generation:

```sh
python3 tools/smoke_relay_ffmpeg.py --ffmpeg /opt/homebrew/bin/ffmpeg
```

This starts its own exact-origin fixture and relay, decodes two seconds to
mono 8 kHz 16-bit PCM, checks nonzero samples, and shuts both servers down.
On 2026-10-21, FFmpeg **9.0.1** returned **exit 0**, **32,000 PCM bytes** and
**nonzero audio** for each of `/continuous.mp3`, `/icy.mp3` and
`/hls/normal/index.m3u8`, with empty error output. It uses normal FFmpeg extension
policy with no override. This verifies decoding, not audible listening or Kodi.

Cue expiry now reports `break_unresolved(expected_duration_elapsed)` once and
retains association with the original break. A later same-epoch explicit IN
emits one `break_end` with that ID. Supersession, epoch and timeline gaps retire
association. Discontinuities retire generic cues unless an explicit CONT/IN at
the next boundary proves continuation; DATERANGE IDs survive discontinuities. HTTP parser/protocol exceptions become sanitized
`HttpError` values at request/read boundaries; discovery can fail over, while
uncertain delivery and click counting remain single-attempt operations.

PROGRAM-DATE-TIME and DATERANGE accept valid decimal fraction widths on Python
3.9.6 by padding/truncating only the parsing copy to six microsecond digits.
Original manifest and event evidence remain unchanged. Invalid dates, empty or
malformed fractions and out-of-range offsets remain rejected. Python 3.8 has
not been directly exercised.

### Live Radio Browser sample

Command: `python3 tools/smoke_radio_browser.py --limit 3`. Directory discovery and
each station have a 15-second deadline, a maximum 1 MiB finite-body cap and the
production public-address/redirect policy. No `/json/url/` click report is called.
Samples do not decode audio, report tracking or infer cue/advertisement coverage.
Only UUIDs, schemes, classifications and byte counts are recorded; upstream URLs
and directory names are omitted.

Initial run **2026-10-21 17:55:44 UTC**: discovery succeeded; two continuous streams
returned 512 bytes each, while the HLS sample produced a parser failure. Bounded
follow-up inspection identified a valid ISO 8601 basic timezone in
`EXT-X-PROGRAM-DATE-TIME`, for example `2026-10-21T17:58:58+0000`. The playlist master
parsed; its child media playlist used supported tags. Python 3.9's
`datetime.fromisoformat` rejected `+0000`. This was a supported-timestamp
compatibility defect, not a reachability failure or intentionally unsupported HLS
feature. The fix normalizes basic `+HHMM`/`-HHMM` offsets to extended form for
parsing only, retaining original playlist/evidence text. DATERANGE start/end
parsing shares that helper; invalid hour/minute values remain rejected.

Final run **2026-10-21 18:00:27 UTC**, exit **0**, discovery succeeded:

| Station UUID | Input scheme | Resolved protocol | Outcome | Bytes sampled |
| --- | --- | --- | --- | ---: |
| `c685c086-b716-4edd-b313-d4a7baf1084c` | HTTPS | HLS | reachable | 4,096 |
| `db93a00f-9191-46ab-9e87-ec9b373b3eee` | HTTP | continuous | reachable | 512 |
| `ffc0d701-f43b-4232-8be6-f7ff72add8ef` | HTTPS | continuous | reachable | 512 |

Final sample: **0 reachability failures, 0 parser failures, 0 unsupported/policy
rejections**. These three directory results are not a representative protocol,
codec, geographic, uptime or advertisement sample. “Reachable” means bytes were
received and sampled HLS syntax parsed; it does not mean heard/decoded playback.
No cues observed means no cues observed, never proof that no ads exist.

### Kodi acceptance status

| Required real Kodi check | Result on this host |
| --- | --- |
| Install final ZIP and start embedded service | Pending — Kodi unavailable |
| Search/browse and real-station playback | Pending — Kodi unavailable |
| Hear MP3/AAC tone and controlled ICY metadata/audio | Pending — Kodi unavailable |
| Hear HLS tone and match every fixture window/event timeline | Pending — Kodi unavailable |
| Stop, rapid station switch, settings restart, quit and resource cleanup | Pending — Kodi unavailable |
| Verify probing/HEAD/Range/reopen, buffering and audible boundaries | Pending — Kodi unavailable |

A future tester must record exact Kodi build, embedded Python version, OS, heard
audio and timings here. Pure-Python tests and the static checker do not replace
these acceptance checks. Generate fixture audio with FFmpeg into ignored
`tests/generated-audio/`, enable the exact local fixture origin in test mode, and
restart the fixture server between scenarios. Expected events are available at
`/expected/{scenario}.json`; compare ordered event kind/classification for all
three windows rather than assuming a skipped window was observed.

## Review record

All eight task reviews passed after their fixes. CodeRabbit completed a branch review with two findings (HTTP exception normalization and fractional timestamp compatibility); both were reproduced and fixed. The independent whole-branch review additionally identified initial range-request compatibility and late cue-in handling. A real FFmpeg check then exposed extensionless HLS route rejection. The final scoped review approved all fixes within POC scope. No review claims actual Kodi playback verification.

## Controller verification after final review

`python3 -m pytest -q`: 321 passed in 33.71 seconds. This run emitted a transient local test-server exception banner without a retained traceback, so the output was not pristine; no test failed. `python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip` passed. Independent `python3 tools/smoke_relay_ffmpeg.py` decoded MP3, ICY and HLS, each with exit 0, 32,000 nonzero PCM bytes and empty stderr. The package SHA-256 remains `df35c648e1fe3bd478213ab91c80d640699869b1e71a6ef846551284070bf182`.

Targeted review found an intentionally disconnected request in `tests/test_transport.py::test_failure_during_request_transmission_never_tries_next_address` whose standard HTTP-server handler can print a thread exception on write. This plausibly explains the banner but does not establish its exact cause. Review approval is unchanged; no production defect was established by the banner alone.

## Pre-PR review corrections

Verified and fixed the URL-policy bypass through IPv4-embedded IPv6: standard
NAT64 prefixes and mapped IPv4 remain blocked; 6to4 and both Teredo endpoints
are checked for non-global IPv4 addresses. 6to4 eligibility follows the
embedded Python runtime's global-address classification. Deterministic resolver
regressions preserve public native IPv6 and the exact fixture-origin exception.
Radio Browser click reporting now runs once after successful Kodi
resolution, outside playback failure cleanup; reporting errors cannot stop or
revoke the resolved session. Existing startup/create cancellation tests still
pass, and ordering/failure-isolation regressions cover the new boundary. Fixture
documentation now describes the accepted `Range: bytes=0-` full-body probe.

Validation: targeted transport/runtime suite **82 passed in 9.74s**; full suite
**331 passed in 33.76s** on Python 3.9.6. Both runs emitted the previously noted
local HTTP test-server exception banner; no tests failed. Package creation and
`check_package.py` exited 0. Rebuilt ZIP SHA-256:
`8d46bab589c0127fdea6f3bd8294e546bb4ae19ec660cfcbebae70f6d7ed0d0b`.
The official `kodi-addon-checker --branch omega` check on that ZIP extracted to
`dist/checker-prepr/` exited 0 with no addon problems or warnings; the separate
host LibreSSL warning remains. Kodi playback acceptance remains pending.

### HTTPS Host header follow-up

The next CodeRabbit pass identified HTTPConnection's default port of 80 being
used for manually wrapped HTTPS sockets. Wire-header regressions reproduced
unwanted `:443` on standard HTTPS origins and missing `:80` on nonstandard
HTTPS port 80. The connection now uses the URL scheme's default port when
serializing Host. Nine deterministic cases cover implicit/explicit defaults,
nondefault ports, IPv6 brackets, original TLS hostname, and rejection of a
caller-supplied Host override. Four cases failed before the fix; the transport
suite then passed **49 tests in 12.73s**, with the existing test-handler banner.
Full suite: **339 passed in 37.64s**, with that same banner and no failures.
Package creation, `check_package.py`, and official Omega addon checker on the
fresh extraction in `dist/checker-prepr-host/` all exited 0. The addon checker
reported no problems or warnings; its separate host LibreSSL warning remains.
Latest ZIP SHA-256:
`c4d49902cea95b27e3237a51c069c32dc1456b658b499313b68223e93cf94d76`.

### Background failure and legacy ICY follow-up

The next review reproduced two gaps. Background service failures used a modal
OK dialog, delaying its abort/settings wait loop; this path now emits a localized
error notification. Two regression cases forbid modal invocation and exercise
abort and corrected-setting recovery with descriptor cleanup. Other interactive
plugin dialogs retain their behavior.

Legacy SHOUTcast `ICY 200 OK` responses failed standard HTTP parsing before
station resolution despite the existing ICY metadata decoder. A bounded status
adapter now normalizes only the leading `ICY ` token to `HTTP/1.0 ` and delegates
status validation to the stdlib. Headers and body retain their original buffered
stream and stdlib behavior. Actual socket responses verify continuous station
resolution, unchanged audio and metadata, ordinary HTTP/1.0 and HTTP/1.1, malformed
protocol/status rejection, and the status-line size limit. The legacy success
case failed before the fix, while ordinary HTTP and rejection cases passed.

Combined targeted transport/ICY/Kodi runtime validation: **115 passed in 18.63s**,
with the previously documented local HTTP-handler banner and no failures.
Full suite: **349 passed in 42.65s**, with the same banner and no failures.
Package creation, `check_package.py`, and the official Omega addon checker on
`dist/checker-prepr-icy/` all exited 0. The checker reported no addon problems or
warnings, alongside the known host LibreSSL warning. Latest ZIP SHA-256:
`1b2b3635b800e16363b74c1d960c18a75f8743638eeaf602daeddf9e403404a7`.
Rerunning `python3 tools/smoke_relay_ffmpeg.py --ffmpeg /opt/homebrew/bin/ffmpeg`
after the transport change decoded continuous MP3, ICY, and HLS successfully:
each returned exit 0, 32,000 nonzero PCM bytes, and empty stderr. This remains
FFmpeg decoder evidence, not heard audio or Kodi runtime acceptance.

### Unicode request targets and continuation evidence follow-up

A subsequent review reproduced non-ASCII path/query failure at HTTP request-line
encoding. The transport now UTF-8 percent-encodes only non-ASCII characters,
preserving existing ASCII request targets, reserved characters, signature values,
and percent-escape spelling. Actual socket tests cover Unicode paths/queries and
a parsed relative HLS segment URI; three cases failed before the fix.

Increasing continuation ElapsedTime also emitted redundant lifecycle updates.
`cue_observed` still records each distinct continuation, while `break_update`
now reflects supplied duration changes or start/end date changes. Record evidence
and inferred origin timing refresh even without a lifecycle update. Regressions
cover advancing windows after OUT and mid-break entry, deduplication, expected
expiry, later duration arrival, and changed dates. Three regression cases failed
before the fix; existing duration addition/correction checks remain green.
Combined cue/transport suite: **117 passed in 19.73s**, clean output.
Full suite: **357 passed in 44.45s**, clean output. Refreshed FFmpeg smoke decoded
MP3, ICY and HLS with exit 0, 32,000 nonzero PCM bytes and empty stderr for each.
Package creation and `check_package.py` exited 0. Latest ZIP SHA-256:
`414a32316d486de664aa2156c54323fcaf794139c091406eef493ea455f47ef9`.
Official Omega checker on the fresh extraction in `dist/checker-prepr-unicode/`
exited 0 with no addon problems or warnings; its separate host LibreSSL warning
remains. Actual Kodi acceptance remains pending.

### Concurrent station selection follow-up

Review reproduced a second selection waiting behind an uncancelled pending
resolver. Each create now registers its own cancellation/ownership token before
waiting for the serialization lock and cancels the preceding selection. Queued
stale requests cannot enter resolution or overwrite the latest pending token;
both pre-resolution and publication checks enforce ownership. Late responses
from cancelled requests are closed, and manager close also cancels the latest
queued selection. Four deterministic two-/three-selection regressions reproduced
the original cancellation failure and now cover newest-selection publication,
stale queue rejection, late probe cleanup, and close during queued resolution.
The session/relay/Kodi runtime suites passed **81 tests in 17.08s**.
Full suite: **361 passed in 44.37s**, clean output. Package creation and
`check_package.py` exited 0. Latest ZIP SHA-256:
`bb7d0fc37d7b20e9a511d2ac8d551f28533fc724c734458247157371701bc01c`.
Official Omega checker on `dist/checker-prepr-selection/`, extracted from that
ZIP, exited 0 with no addon problems or warnings; the known separate host
LibreSSL warning remains. Actual Kodi playback acceptance remains pending.

### HLS attribute parsing and safe HEAD follow-up

The next review reproduced quadratic attribute-name suffix copying and false
URI detections inside vendor text/quoted base64. Attribute matching now advances
by absolute offsets without repeatedly copying the remaining line. A counted
string regression verifies 2,000 parsed attributes and bounded copied characters
without a timing threshold. The URI guard skips quoted values while checking
assignment keys, retaining actual URI/URL-key, absolute URL and variable guards.
Four benign text/payload cases and the copy-budget test failed before fixes.

The HEAD finding was checked against official
[Kodi 21.3 CurlFile.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/filesystem/CurlFile.cpp#L1554-L1572):
`Stat` falls back from failed HEAD to a normal transfer and aborts after receiving
data. The relay now serves metadata-only HEAD 200 for registered continuous
streams and playlists, using retained upstream headers or the known playlist
MIME type. It does not claim/acquire playback, fetch upstream bytes, or observe
cues. Range is ignored for HEAD; no transformed Content-Length is invented.
Segment/map HEAD remains 405 without an upstream request because their metadata
is not retained. Exact opaque route validation and GET range semantics remain.
Five HEAD regressions failed before this fix; tests now verify HEAD-then-GET
returns intact audio, preserves the sole probe, and does not emit premature
metadata/cue events. Kodi runtime acceptance still requires a real Kodi test.

Combined relay/HLS suite: **138 passed in 18.56s**, clean output.
Full suite: **377 passed in 46.88s**, with the previously documented local HTTP
handler exception banner and no failures. FFmpeg smoke again decoded MP3, ICY
and HLS with exit 0, 32,000 nonzero PCM bytes and empty stderr for each. Package
creation, `check_package.py`, and the official Omega checker on the fresh
`dist/checker-prepr-head/` extraction exited 0. The official checker reported no
addon problems or warnings; its separate host LibreSSL warning remains.
Latest ZIP SHA-256:
`782ca281487adac19becfcae1f5963a91cc7fda51e87fc5288a47d424781d983`.

### Cue identity across discontinuities

The two overlapping review findings were verified against RFC 8216
[§4.3.2.7 and §4.3.2.7.1](https://www.rfc-editor.org/rfc/rfc8216.html#section-4.3.2.7)
and the repository media fixture. DATERANGE identity now spans discontinuities
within one rendition/epoch. The approved generic-cue refinement preserves an
active association only at an explicit consecutive CONT/IN boundary; the full
policy and unchanged terminal cases are in [implementation decisions](implementation-decisions.md).
Crossing does not imply a new start or reliable expiry origin. CONT/IN observation
deduplication uses sequence identity so closing a linked break cannot produce a
duplicate observation when the window repeats.

Eight initial lifecycle regressions failed before the fix. A strengthened
repeated-window test separately reproduced and fixed duplicate CONT evidence.
Coverage includes combined and rolling windows, OUT before the first ad segment,
same-ID DATERANGE end matching, reused IDs/new epochs, malformed/uncued/skipped
boundaries, stale cues, unknown timing, and the existing media fixture. The cue
suite passed **69 tests in 0.32s**; the earlier combined cue/relay/fixture run
passed **105 tests in 19.08s** before the final deduplication strengthening.
The current Kodi smoke instructions now describe the available ZIP and correct
metadata-only HEAD behavior.
Final full suite: **391 passed in 46.72s**, with the previously documented local
HTTP-handler banner and no failures. Package creation and `check_package.py`
exited 0. Latest ZIP SHA-256:
`8ff997287825f02db1c360012cbc3a7b9cafabfccd4a1e8dd60e4de1c26d65c7`.
FFmpeg smoke during this fix wave decoded MP3, ICY and HLS with exit 0, 32,000
nonzero PCM bytes and empty stderr each; that run preceded the final event-only
deduplication adjustment. Kodi runtime acceptance remains pending.
Official Omega checker on the final ZIP extraction in
`dist/checker-prepr-discontinuity-final/` exited 0 with no addon problems or
warnings; the separate host LibreSSL warning remains.

### Exact directory categories

The final full-branch review identified substring matching for chosen country
and tag categories. The [official advanced-search API](https://docs.radio-browser.info/#advanced-station-search)
defaults `countryExact` and `tagExact` to false. Search now supplies each exact
flag only when its category is nonempty. Free-text name matching, offsets,
limits, broken-station filtering and failover remain unchanged. Four query
regressions failed before the fix; directory/Kodi runtime tests passed
**66 tests in 1.59s**. No transport or decoder behavior changed in this correction.
Full suite: **395 passed in 46.83s**, clean output. Package creation,
`check_package.py`, and the official Omega checker on the fresh
`dist/checker-prepr-categories/` extraction exited 0. The checker reported no
addon problems or warnings, alongside the known separate host LibreSSL warning.
Latest ZIP SHA-256:
`f39d96a58b0a89c8b50ba7079be1f220cd881ea763fa9744554691007305d12c`.

## Actual Kodi runtime — 2026-10-21

Installed the official Kodi 21.3 ARM64 application via Homebrew; Developer ID
signature and notarization assessment passed. Normal startup repeatedly hung
in macOS `IOServiceOpen` during Kodi USB interface enumeration. A bounded
standalone diagnostic identified the RTL9210B-CG storage interface: Ethernet
interfaces returned successfully, while its interface-open call timed out.
Reading the interface class from IORegistry completed for all six interfaces.

For testing, the unmodified Kodi binary was launched using a process-local
sandbox profile denying `IOUSBDeviceUserClientV2` and
`IOUSBInterfaceUserClientV3` opens. This disables direct Kodi USB peripheral
discovery for that launch; USB networking remains connected. No system security
setting or app signature changed. A separate proposed Kodi source patch replaces
the blocking interface-class query with a registry read. Its small harness passed
12 property cases and six live interfaces, but it has not been built into Kodi
or submitted upstream. This source patch is separate from the radio addon.

The addon ZIP was extracted into the Kodi profile and enabled through Kodi's
loopback JSON-RPC API. The ZIP-install dialog was not exercised. Test mode and
diagnostics were enabled with exact origin `http://127.0.0.1:8765`. Playback used
the plugin URLs returned by Kodi's fixture directory, exercising `setResolvedUrl`
and the embedded service rather than opening fixture-origin URLs directly.

Initial continuous playback failed with HTTP 409: Kodi's content lookup performs
a disposable GET before the decoder GET. The adapter now disables that lookup
and uses MIME metadata returned by the service from its resolved response. A
second runtime failure delayed service availability: a Python stack dump located
it in `HTTPServer.server_bind` → `socket.getfqdn('127.0.0.1')`. Numeric loopback
binding now avoids this unnecessary reverse-DNS lookup.

| Runtime check | Observed result |
| --- | --- |
| MP3, AAC, ICY | Active Kodi player with advancing time; ICY metadata logged |
| HLS OUT/IN | cue_observed, break_start, cue_observed, break_end, all signaled |
| HLS mid-break entry | in_progress observation then signaled observation/end; no start |
| HLS missing end | signaled observation/start then break_unresolved |
| HLS no cues | No cue records |
| HLS malformed cue | One invalid cue observation |
| Directory | Country/tag pages and next pages, BBC search returned results |
| Live station | First BBC Radio 1 directory result played with advancing time |
| Stop | Player.Stop ended each tested playback |

All five HLS schedules were compared in order with the fixture server's expected
JSON, filtering by station and latest session. No-cue success is absence of
observed cues, not proof of absent advertisements. Initial directory requests
returned Retry after timeouts; subsequent requests succeeded without a directory
code change. The reason for those transient lookup delays was not established.

Raw runtime evidence and the USB diagnostic artifacts are kept outside this
checkout. Player clocks and decoder logs establish playback activity, not heard
audio. VAST insertion, ad replacement, audible-output capture, other platforms,
and full Kodi validation of the USB source patch remain outside this result.

After the DNS-free bind fix, a fresh Kodi launch published the service descriptor
in **0.787 seconds** (the instrumented previous launch spent **35.005 seconds**
in server startup). Authenticated service health and a new MP3/AAC/ICY switching
sequence passed. Switching every four seconds retained active playback and
retired each previous media URL with HTTP 404. Player.Stop left no active player.
Application.Quit completed in **2.023 seconds** and removed the service descriptor.
The temporary startup instrumentation was replaced with the packaged entry point.
The fixture server and Kodi were stopped after testing. Settings-dialog restart
behavior and audible output remain unverified.

Final full suite: **399 passed in 47.92s**. The continuous-playback fix's focused
suite passed 79 tests; the no-DNS bind regression first failed on an unexpected
`socket.getfqdn` call, then the relay suite passed 38 tests including a real
loopback health request. Package creation and `check_package.py` passed.
ZIP SHA-256:
`9db622bc949ff42df6a28b2e6d526dcf329d0d3db036ff3c6b035d1e336964b2`.

## Controlled ICY marker scenarios — 2026-10-23

The fixture server now provides finite, paced streams at
`/icy/{scenario}.mp3` and expected ordered marker events at
`/expected/icy/{scenario}.json`. Scenarios cover explicit AD commands,
TildeATS category 4/category 0 and `##` end-break forms, wrapped commands,
bare punctuation candidates, inferred return-to-track and paired-marker ends,
and an unresolved break after 300 seconds. Tests drive the last timeout with a
synthetic monotonic timestamp, so they do not sleep five minutes.
Each finite HTTP stream now uses generated MP3 frame payloads and paces complete
audio-plus-metadata frames. In test mode with the exact configured loopback
origin, the Kodi controlled-stream menu exposes a localized entry for each
scenario. Automated RelayServer coverage exercises the paired-marker stream
through the actual session path and verifies the stripped audio bytes and
observation-only events.

Automated fixture validation decodes the framed stream through `IcyDecoder`,
checks each event kind and classification against the fixture schedule, and
compares every output audio byte with the original generated interval bytes.
It also confirms that marker observations produce no replacement, VAST, or
beacon event. Tracker history is limited to 64 observations in 300 seconds;
unknown marker candidates are bounded and direction-neutral until inference or
a listener's exact label provides context. A remembered label is scoped to the
station and its exact ICY title. ICY remains observation-only: preroll and
midroll replacement/beacon behavior apply only to eligible HLS sessions.

Run the fixture-focused suite with
`python3 -m pytest tests/test_fixtures.py tests/test_icy_markers.py tests/test_relay.py tests/test_kodi_runtime.py -q`.
This validation is not a Kodi UI playback run; the controlled sequences are
verified as bounded stream/decoder/tracker fixtures, and any separate Kodi
menu or audible-playback check must be recorded independently.

## Live GUI follow-up — 2026-10-24

The Kodi desktop became available after the earlier locked-session attempts.
Kodi reports **21.3.0** (21.3 Omega); `system_profiler` reports **macOS 26.6.2**,
Apple **MacBookPro18,2**, and a **3840×2160** display. Input was keyboard events
sent through desktop automation. Kodi's current Interface settings screen shows
**Estuary**. Earlier in this same GUI acceptance effort, Confluence was installed
and exercised for station listings, station context actions, map rendering, and
return to the selected station; these observations are summarized in the table
below and in Step 4 of `docs/superpowers/plans/2026-10-23-kodi-radio-gui.md`.

| GUI acceptance check | Result |
| --- | --- |
| Kodi skin setting | Estuary shown in Interface settings |
| Alternate skin | Confluence station-list/context-map path observed earlier; see Step 4 of the Task 7 plan |
| Station listing and context menu | Observed; station rows and addon plus Kodi standard actions rendered |
| Map from station context | Observed in Confluence: Guadeloupe heading, two-station rail, selected city pin, and complete wrapped map note |
| Preview while map is open | Observed after installing the `windowed=True` preview change: focus reached the station rail and map remained visible |
| Return selection | Escape from map returned to the originating station list with the same Guadeloupe station selected |
| D-pad boundary exit | From the selected city pin, Left reached the map's Back control; this confirms an available exit, not exhaustive no-wrap behavior in every region |
| Root map entry | Observed in Estuary: selected **Open the map** from the add-on root; the map opened with the Lagos pin and station rail |
| Guided setup cancellation | Observed in Estuary: opened the setup country picker, pressed Back, and returned to the add-on root without saving a place |
| Guided setup completion | Partial: selected Toronto, ON (43.577, -79.343) and the 25-mile radius; the flow displayed “Add another place / Save places.” A second run through the country selector returned “No stations or categories found” and exited to an empty root. A saved Toronto row was visible after the first run, but the final post-save map/use path was not confirmed. |
| Empty-location handling | Observed: selecting Andorra returned the bounded-scan “No matching station locations with valid coordinates” message and saved no place. |
| Settings | Observed in Estuary: add-on settings opened; Run setup, Guided setup, setup-row visibility, preview, diagnostics, and fixture settings were visible |
| Controlled test stream directory | Observed in Estuary: the add-on displayed its controlled MP3/AAC tone, ICY metadata, HLS cue, missing/malformed cue, controlled-break, mutated-source, advanced-window, delayed-segment, explicit ICY command, and TildeATS fixture entries. The Mac locked when opening a fixture, before playback or marker output could be observed. |
| Preview cancellation, zero click count, competing playback | Not verified live; adapter tests cover the dispatch and lifecycle boundaries |
| Controlled fixture, actual playback click count, audible output | Not verified live in this GUI follow-up |

The map rail remained visible while a real Radio Browser station preview was
started. This establishes the GUI behavior, not audible output or a Radio Browser
click count. Focused adapter tests assert that preview uses a separate route and
does not send click reports. The map's single-station heading also exposed a
pluralization defect (`1 stations`); the UI now uses localized singular and
plural labels, covered by adapter tests. The approved first-slice GUI acceptance
is therefore **partial**, not complete; themed windows stay deferred until the
remaining live checks are covered.
The corrected script and localization file were copied into Kodi's profile for
verification, but the Mac locked before the map could be reopened to confirm the
updated heading on screen.

## Kodi player smoke through JSON-RPC — 2026-10-25

Kodi 21.3 remained running with the add-on's controlled stream menu available
over local JSON-RPC 13.5. Desktop automation still reported the Mac locked, so
this run observed the player and fixture requests without a screen inspection.
The five normal, stop, expired-window, mutated-source, and delayed-segment
scenarios all matched the requested add-on item and passed the corrected clock
check. The final rerun reached 14.309 seconds on normal playback, 10.093 seconds
on the expired-window source, and 12.141 seconds on the mutation source. In the
delayed-segment case it stayed at zero through about 8.3 seconds, then advanced
to 8.184 seconds; Kodi reported `Player.Caching=false` and `cachepercentage=100`
through the sample window. The Stop action succeeded. Pause, resume, and seek
were rejected for this live stream, which reported `canseek=false`.

The Kodi profile had test mode enabled and the loopback fixture origin set, with
no VAST URL configured. No beacons were recorded. VAST preroll and midroll
replacement remains validated by the independent relay decode run; this player
smoke does not claim live Kodi ad replacement, audible output, or a visual GUI
inspection. The JSON-RPC observation is stored as
`kodi-player-acceptance-2026-10-25.json` with the local review reports.

## Kodi ICY marker smoke through JSON-RPC — 2026-10-25

With the diagnostic log enabled, Kodi opened each of the seven finite ICY
fixtures from its **Controlled test streams** directory. For every scenario,
the expected fixture item transition and one fixture-source request were
observed, and the new entries in the Kodi profile's `events.jsonl` matched the
fixture's ordered marker events:

| Fixture | Kodi marker log result |
| --- | --- |
| `explicit` | `AD_START_30` produced an explicit break start; `AD_STOP` produced an explicit end |
| `tildeats` | TildeATS category 4/category 0 pairs and `##` end values produced two explicit breaks |
| `wrapped` | `#AD_START#` and `#AD_END#` produced an explicit break |
| `return-to-track` | Bare `-` was a candidate and the later track title closed an inferred break |
| `paired-marker` | `#`, `*`, `#` produced candidate observations and an inferred break |
| `punctuation` | `-`, `#`, `*`, then a track title produced candidate observations and an inferred break |
| `unresolved` | Bare `#` produced a candidate and inferred start; the five-minute unresolved timeout was not awaited |

This verifies marker observation and classification on Kodi's actual add-on
playback path. The locked desktop still prevented screen inspection, and the
finite tone fixtures were not listened to or captured independently.

## Spec-alignment follow-up — explicit station voting

Station context menus now include **Vote for station**, as required by the
radio-browser specification. Kodi asks the user to confirm before sending a
vote. The add-on validates the station identifier and response shape, makes one
Radio Browser request without retrying an uncertain mutation, and keeps voting
separate from playback and click accounting. Tests cover confirmation and
cancellation, endpoint construction, invalid input and response, success and
rate-limited rejection, and one-attempt behavior. The corrected spec also
matches the delivered Kodi input dialog, station metadata API, place rows, and
saved `country_code` field. The GUI plan now identifies the map entry points as
skin-mode scope and leaves themed-mode navigation as future work.

On this revision, `python3 -m pytest -q` passed **1,087 tests in 118.67s**.
`python3 -m compileall -q plugin.audio.kodi_donor_radio/resources/lib tests tools`,
`git diff --check`, package creation, and the package manifest checker passed.
Kodi add-on checker **0.0.36** on a fresh extraction for Omega reported no
problems or warnings; its host Python emitted the known LibreSSL/urllib3
compatibility warning. The Mac is still reported locked by desktop automation,
so this run adds no new visual or audible acceptance evidence. Existing live
GUI, Kodi player, and ICY observations remain limited to the evidence recorded
above; live VAST beacon delivery and audible output are not claimed.

The same pass also rejects a malformed MPEG-TS PAT/PMT packet whose adaptation
field consumes the pointer byte; it now raises the normal bounded `AdError`
instead of leaking an `IndexError` during controlled-ad validation.
Fixture generation also checks numbered source, preroll, and replacement segments
for missing sequence numbers and exact 1,920-tick PTS continuity between
segments, in addition to validating each segment on its own.

## Initial Kodi master startup attempt — 2026-10-24

An initial launch attempt used the locally built Kodi **23.0-ALPHA1**, Git `434da3c`, from an isolated portable profile on the same Mac. Kodi's add-on manager discovered
`plugin.audio.kodi_donor_radio` v0.1.0, and JSON-RPC v14.1 initialized, but Kodi
did not finish startup. A process sample of the main thread remained in
`CPeripheralBusUSB::DeviceAttachCallback` and
`IOUSBInterfaceClass::start`; the configured loopback JSON-RPC listener never
opened. This prevented invoking the add-on route or testing playback on this
master build. Desktop automation also reported that the Mac was locked, so no
GUI screen inspection was possible. This initial attempt was a host startup limitation, not an
observed add-on route failure. The successful same-Mac launch and controlled
VAST placement results are recorded in the 2026-10-25 section below. A fresh Kodi 21.3 relaunch on this Mac showed
the same main-thread stack in `CPeripheralBusUSB::DeviceAttachCallback` and
`IOUSBInterfaceClass::start` before opening its JSON-RPC listener. This
reproduces a host startup blocker on both tested binaries; earlier Kodi 21.3
playback observations remain historical and separate. The official Kodi
add-on checker 0.0.36 also passed its Piers target on the current package,
which is static package validation rather than a master-runtime test.


## Separate Kodi VAST placement smoke, 2026-10-25

On the local Mac, Kodi 23.0-ALPHA1 (`20261021-434da3c`) ran from its isolated portable profile with the addon enabled, test mode on, and an exact loopback-only fixture origin. The same controlled HLS source and player-clock observer were used for both runs. Both passed the requested-item identity check and bounded player-clock progress check.

- **Preroll only:** Kodi requested `/vast/preroll`, `/ads/preroll.m3u8`, and both preroll transport-stream segments. The fixture recorded one impression, one start, and one first-quartile beacon. It also fetched the source manifest and programme segments.
- **Midroll only:** Kodi requested `/vast/direct`, `/ads/replacement.m3u8`, and both replacement transport-stream segments after fetching the marked source stream. The fixture recorded one impression, one start, and one first-quartile beacon.
- **Both placements enabled:** a separate 18-second run requested the direct VAST response and replacement media twice and recorded two impressions and two starts.

The addon event log recorded the controlled HLS cue-out and cue-in and corresponding ad-tracking events. These results prove that Kodi drove each placement through the local VAST, HLS media, and beacon fixtures while its reported player clock advanced. They do not independently prove what was audible or establish production ad-position mapping; no audio capture or listening verification was performed. Radio Browser stations remain outside this loopback-only test configuration.

## Radio Browser CDN/Adswizz stream inspection — 2026-10-25

A read-only Radio Browser query returned 70,055 station records. Filtering the
records locally by resolved hostname and `hls=1` found 8 `cdnstream1.com` HLS
entries. Six top-level playlists were reachable; 5 exposed a readable media
playlist. Four reachable media playlists contained segment paths under the
AdsWizz delivery host: **100Hitz - 90s Alternative Hitz**, **GotRadio - Soft
Rock Café**, **KMHD Adaptive AAC**, and **MIX 96 Sacramento**. KMHD also carried
an opaque `adswizzContext` comment in `EXTINF`; no signed query or context value
is retained here. The fetched manifests did not expose standard CUE-OUT/IN or
SCTE-35 tags. Other entries included a broken IKIM FM endpoint and K-Love
playlists whose child manifests required authorization. Radio Browser's HLS
flag is directory metadata, not proof that a current child manifest is
accessible.

A simultaneous 20-minute ICY sample of the Radio Browser **MS NOW** (listed as
MSNBC), **CNBC International**, and **CNN** feeds found explicit Adswizz fields
on MS NOW and CNN at startup; MS NOW later emitted repeated midroll markers.
CNBC produced no metadata transitions in the sample, and CNN had no observed
midroll. The marker parser now recognizes exact string `adw_ad=true/false`,
records a valid `durationMilliseconds` of 1–300000 as seconds, and preserves a
bounded `insertionType`. It emits explicit start/end events, updates on changed
ad state, and deduplicates identical metadata while refreshing the active
boundary timeout. A missing field does not end a break. Opaque `adswizzContext`
and `adId` values are dropped from diagnostics.
This remains observation-only and does not feed VAST replacement or beacons.
