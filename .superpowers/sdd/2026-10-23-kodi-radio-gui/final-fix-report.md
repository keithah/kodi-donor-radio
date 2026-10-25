# Consolidated final GUI fix report — 2026-10-24

## Scope and branch

Worked only in the managed development worktree on `codex/vast-controlled-ads`,
starting from clean base
`2eabbee4f9e0d20ed7dddab90b61604d85cbfdb9`. Followed `final-fix-brief.md` as the
requirements. No subagents/reviewers were dispatched, no branch was pushed, and
no GitHub/PR state was changed. The Mac desktop remains locked; nothing in this
report claims live Kodi GUI, rendering, input, or audio acceptance.

Implementation commit: `399dc7703c9f924ec6ee2925a2df45c90195e957`
(`fix: handle Kodi map focus and lifecycle edge cases`). This report is retained
in a following documentation-only commit.

## Findings verified and changes

1. **Pin focus notification loop — fixed.** The original `onFocus` called
   `focus_city`, which always rebuilt pins/rail and called `setFocusId` on that
   same pin. A queued-focus fake reproduced a non-draining queue (`[1000]`
   remained after eight deliveries). Passive focus now calls `focus_city` with
   `request_focus=False`; explicit navigation requests focus only when it differs
   from the current pin. Rail and pin rendering run only when the selected city
   changes. The regression verifies a drained queue, the same rail item objects
   after unchanged selection, and ordinary native selection of a different city.

2. **Native rail endpoint wrap — fixed.** The original code compared the
   already-moved row against the old row, treating Kodi's endpoint wrap as an
   ordinary middle-row change. Removed unsupported `<wraparound>` XML. Python
   checks the previous row before handling the newly delivered position,
   restores the first/final row, and moves Up to the current pin or Down to
   Preview/Back. A Kodi-faithful fake performs native modulo list movement before
   `onAction`. Tests cover both full directional sequences, ordinary middle-row
   movement, endpoint restoration, no preview of the wrapped row, and preview
   enabled/disabled. All four endpoint cases failed before the fix.

3. **Selected context station in the rail — fixed.** The original sorted
   truncation dropped a station beyond row 50 and reset selection to row 0.
   The rail now reserves one of its 50 slots for a selected station beyond the
   normal cap, retains deterministic order for the other 49, and selects the
   context row. Regressions cover `station-59` at row 49 and `station-17` at row
   17; both initially reported row 0. Cached `StationSummary` records also retain
   their selected identity through the existing 200-seed budget and render as
   UUID-only rail items.

4. **Empty discovery versus network errors — fixed.** Discovery and selected
   lookup failures originally reused localized empty-map string 30147, and
   discovery failure discarded a valid selected station. Successful empty maps
   still use the empty state (30147), now worded without implying network
   failure. Discovery errors use 30163; selected refresh/lookup errors use 30164.
   Both errors are retained when both operations fail. Successful lookup data
   survives discovery failure. Failed selected lookup can use a validated cached
   favorite display summary, including its coordinates; successful discovery
   records remain available. Tests separately cover successful empty discovery,
   discovery failure with/without a selected record, selected lookup failure,
   cached selected fallback, and combined failure. Display fallback does not
   cache a playback URL or bypass fresh UUID-based preview resolution.

5. **Settings scope contradiction — corrected.** The design spec now lists only
   guided setup, setup-row visibility, and map preview as first-slice settings.
   Interface-mode and themed-entry settings are explicitly deferred with themed
   windows. This is a prose correction, with no runtime setting added.

6. **Optional origin prevents map launch — fixed.** The constructor validates a
   home route against `plugin_base` first. A rejected optional origin falls back
   to that validated home route. URL-like search text, 300-character ASCII text,
   and long percent-encoded Japanese text all originally raised `ValueError`;
   they now permit map initialization. Five unsafe plugin-root cases remain
   rejected (HTTP scheme, different host, path, query, and userinfo). Invalid
   optional origin URLs/queries are discarded, never executed. Valid origins and
   both existing root spellings remain supported.

7. **Preview leak after native modal exit — fixed.** `_open_map` originally
   called `doModal()` without a `finally` path. Cleanup only happened when the
   adapter's own `close()` override ran. `cleanup()` now idempotently releases
   preview ownership without calling native window close; the runtime calls it
   in `finally`, and explicit close reuses it before native close. Runtime tests
   use the real window initialization, preview controller, start arbiter, client
   registration, player handoff, session retirement, and cleanup. Native modal
   return and abort both originally left the preview playing. Both now stop its
   player/session, close and clear its controller, tolerate a second cleanup,
   and never call native close on the dismissed window. Existing pending-start
   and competing-playback ownership tests remain passing.

8. **Zero ICY metadata interval — fixed.** Numeric zero passed relay header
   validation but reached `IcyDecoder`, whose positive-interval requirement
   caused a 502 before audio headers. The relay now leaves the decoder unset
   for zero and forwards the body unchanged.
   [FFmpeg 7.0's primary HTTP source](https://github.com/FFmpeg/FFmpeg/blob/n7.0/libavformat/http.c#L1803-L1811)
   independently confirms its ICY reader invokes metadata framing only when
   `icy_metaint > 0`. The new HTTP response regression failed with 502 before
   the change and now returns 200, the complete original binary audio body,
   audio MIME type, no downstream ICY interval header, and no metadata events.
   Malformed/negative/fractional/overlength interval controls still return 502;
   existing positive-interval metadata and ad-marker tests retain their behavior.

## Test-first evidence

Before production changes, the new focus/endpoint/rail set produced seven
expected failures. The zero-interval case failed with 502 while all four
malformed-interval controls passed. All three optional-search-origin cases
failed with `ValueError`, while all five unsafe-root controls passed. The
network-state tests reproduced distinct-message and retained-record failures.
After correcting the modal fake's directory constructor/mirror interface, its
two cases failed specifically because native dismissal left playback running.
Each regression group was rerun after its focused fix.

The cached-summary adapter fixture was corrected to enter the rail directly:
its additional city allowed a Right action to select a neighboring pin, which
was not the seed-reservation behavior that the test intended to exercise.

## Verification

Environment: Darwin arm64, Python 3.9.6. No new runtime dependencies or network
calls were introduced. Existing ad eligibility, click accounting, beacons,
preview arbitration, local artwork, and bounded-record behavior remain within
the approved slice.

| Check | Result |
| --- | --- |
| `python3 -m pytest tests/test_kodi_radio_map.py tests/test_kodi_runtime.py tests/test_relay.py -q` | 282 passed in 34.53s; clean output |
| `python3 -m pytest -q` | 995 passed in 107.56s; known intermittent loopback HTTP-handler exception banner without traceback |
| `python3 -m pytest -q -s` (final full diagnostic run) | 995 passed in 107.71s; captured the fixture `BrokenPipeError` described below |
| `python3 -m compileall -q plugin.audio.kodi_donor_radio tests` | Exit 0 |
| `python3 tools/package_addon.py --output dist` | Exit 0 |
| `python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip` | Exit 0; Package OK |
| `git diff --check` | Exit 0 |
| Official `kodi-addon-checker` 0.0.36, `--branch omega` on fresh extraction | Exit 0; no add-on problems or warnings |

The checker command was
`.venv/checker/bin/kodi-addon-checker --branch omega dist/checker-gui-final-CECTfo/plugin.audio.kodi_donor_radio`.
The extraction directory was freshly allocated with `mktemp` and populated from
the final ZIP. Checker Python emitted its known host LibreSSL 2.8.3 / urllib3
`NotOpenSSLWarning`; this is distinct from add-on warnings.

Final artifact: `dist/plugin.audio.kodi_donor_radio-0.1.0.zip`, **37 files**,
**450,757 bytes**, mode **0644**, SHA-256
`681ea71ef5cc9312d19bad51ff2d406b91c308734ad92af2b9d2a2c22d6aa9e1`.
`docs/testing.md` records the latest package and verification evidence.

## Remaining concerns

Live Kodi 21.3 rendering, native focus/input behavior, setup interaction, and
audible playback remain unverified while the desktop is locked. Automated
ordering fakes specifically cover the reported Kodi focus and list behavior,
but do not substitute for interactive Kodi acceptance. The first full run's
intermittent local HTTP exception banner is retained in the evidence above;
the final diagnostic run captured its traceback at `tests/test_transport.py:96`,
where the shared loopback test origin writes `hello` after a client has closed.
This is the source of the test-server output, rather than a failing assertion or
an exception in add-on code. The exact emitting test case was not identified.
The retained log is `/tmp/kodi-final-fix-pytest-full.log`. No requested code fix
is left unresolved.
