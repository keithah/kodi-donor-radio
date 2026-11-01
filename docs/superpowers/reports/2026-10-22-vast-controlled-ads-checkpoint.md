# Controlled VAST ads — implementation checkpoint

Status: acceptance blocked, not release-ready. Branch: `codex/vast-controlled-ads-release`.

## Fresh no-replay run — 2026-10-26

The latest package was installed in an isolated portable profile on this Mac and
run with unpatched Kodi master `434da3c` (`23.0-ALPHA1`, JSON-RPC 14.1). All five
controlled scenarios matched the requested item, showed bounded clock progress,
and reported no smoke-validation failures. The add-on log withheld startup clock
samples `-0.091` and `0.110`, enabled tracking at `0.516`, and did not replay the
withheld values. The normal fixture recorded one impression, start, and
first-quartile beacon. Expired-window, mutation, and stall scenarios sent no
success beacons.

Pause, resume, and seek were rejected by Kodi for this nonseekable HLS item; a
separate stop action succeeded. The run validates the no-replay safeguard, not
the absolute clock-to-decoded-presentation mapping or audible output; both report
flags remain false.

## Post-speed-guard master startup retry — 2026-10-26

The speed-guard package was installed in a fresh isolated Kodi portable profile
and launched twice on the same unpatched master revision. Both launches blocked
the main `XBMCMainThread` in USB peripheral initialization at
`IOCreatePlugInInterfaceForService` / `IOUSBInterfaceClass::start`; Kodi never
opened JSON-RPC or reached the addon route. The USB network adapter remained
connected. This leaves post-speed-guard Kodi playback unverified. The earlier
successful no-replay run above is retained as separate, pre-speed-guard
evidence. Details and process/log paths are in
[`kodi-master-startup-hang-2026-10-26.md`](kodi-master-startup-hang-2026-10-26.md).

## Startup confirmation — 2026-10-26

The fixture adapter now buffers the first usable playback sample and enables
tracking only after two consecutive usable samples show positive, bounded clock
progress. Repeated positions do not confirm startup. A startup reset, caching,
pause callback, seek or unavailable sample discards the pending samples. This
prevents the observed first-zero/next-negative clock sequence from emitting an
early beacon; it does not establish a decoded-media anchor.

Two 8-second normal-fixture runs on unpatched Kodi 23.0-ALPHA1 matched the
requested item. In the startup-gate run, Kodi JSON-RPC observed
`0.000 → -0.091 → 0.316`;
the add-on callback sampled `0.000` then `-0.091`, invalidated its mapper, and
sent no beacons. In the separate clock-progress run, JSON-RPC showed the same
initial sequence, while add-on callback samples were `0.000` with tracking
disabled then `0.318` with tracking enabled. `mapping_proven` and
`audible_output_verified` remain false.

A separate 8-second run's add-on callback log observed `0.000 → 0.318`; the
callback gate opened on that positive advance. It recorded one fixture
impression/start/firstQuartile/midpoint/thirdQuartile sequence, with no
completion. These beacons followed the add-on's gated raw clock on that run;
they do not prove the absolute decoded-media anchor. `mapping_proven` and
`audible_output_verified` remain false.
That run predates a further startup safeguard: the adapter now uses the first
clock sample only to confirm progress and sends only the advancing sample to the
tracker. This avoids retroactively treating a possibly unstable startup zero as
a preroll beacon if Kodi resets between the add-on's polls. Unit tests cover the
no-replay case and confirmed midroll tracking; this stricter behavior still needs
a fresh Kodi run. The live reports above describe the earlier implementation.
The no-replay regression and focused tracking/adapter/event suite passed
**61 tests**; the full suite passed **1,162 tests in 133.15s**. `compileall`,
package build, `check_package.py`, and `git diff --check` passed. Live Kodi
verification of this stricter gate remains pending; `mapping_proven` and
`audible_output_verified` remain false.

## Follow-up — 2026-10-26

The fixture-only Kodi clock guard now permanently invalidates on a backward jump
over 50 ms, including a negative clock sample after playback begins. Kodi's
`Player.Caching` condition also suppresses tracking; unavailable cache state
fails closed. With opt-in diagnostics, the adapter records raw clock/mapped
position and playing, pause, seek, and cache state without URLs.

An 8-second normal-fixture run on unpatched Kodi 23.0-ALPHA1
(`20261021-434da3c`) matched the fixture and showed bounded clock progress. The
add-on observed raw time `0.000` then `-0.091`; mapping invalidated and did not
resume. Impression/start had already been emitted on the initial zero sample;
no later quartile or completion was emitted. The fixture recorded one impression
and one start. This still does not prove the initial sample's decoded-media
position, stable sample-zero origin, or audible output. `mapping_proven` and
`audible_output_verified` remain false. See the
[`validation record`](../../testing.md#controlled-vast-clock-follow-up--2026-10-26).

The full suite passed **1,158 tests in 131.71s**. `compileall`, package build,
`check_package.py`, and `git diff --check` passed. The Kodi add-on checker was
attempted with dependency checks disabled, but its repository-index request kept
retrying and was interrupted after more than two minutes; no checker verdict is
claimed for this run.

Earlier verification (superseded by the 2026-10-26 updates above): `python3 -m pytest -q` passed **534 tests in 85.28 seconds**
after enabling a loopback-only experimental raw Kodi-clock mapper; compileall and
`git diff --check` passed. A fresh five-scenario Kodi run sent success beacons for
advancing fixture playback and suppressed quartiles/completion across the delayed
stall jump. This test-only mapper is not enabled for ordinary Radio Browser
sessions. A stable absolute clock anchor and audible output remain unverified, so
production tracking is not ready. Steward, CodeRabbit, and the implementation PR
remain pending until the broader acceptance gate passes.

## Completed and task-reviewed

Tasks 1–6 implement controlled audio/VAST fixtures, bounded VAST resolution,
creative validation/cache, immutable HLS composition, relay/session integration,
and playback-evidenced tracking. Relay tests independently decode preroll and
midroll media and verify source fallback. Task reviews found and corrected
published-media retention, cache-failure fallback, and false completion after a
seek followed by stationary playback samples.

## Implemented and reviewed; acceptance blocked

Task 7 integrates Kodi settings and playback callbacks; Task 8 adds controlled live
fixtures, independent relay decoding, Kodi smoke tooling, and measured playback
reports. Task 7 and Task 8 code reviews approved the implementation after fixes.
Kodi `getTime` held at 1.370 seconds for over five seconds with caching false, then
jumped to 7.770 seconds in 260 ms as caching became true. This crossed the preroll
completion boundary without proof those samples were presented. The fixture-only
mapper uses the raw Kodi clock and the tracker's continuity guard; its end-to-end
test delivered milestones during normal playback and suppressed milestones across
the stall. Pause/seek were unsupported on the tested live HLS stream, and audible
output was not verified. See `docs/testing.md` and the Task 8 verdict in the
external test report directory.

## Next steps

1. Find and validate a Kodi playback-position source that anchors decoded samples
   to the composed presentation through live-window movement and discontinuities;
   keep the raw-clock mapper fixture-only until that evidence exists.
2. Verify audible output and missing/early-end playback in Kodi, and establish a
   supported pause/seek contract for the intended stream type.
3. Finish packaging/versioning and the official Kodi addon checker. Once playback
   acceptance passes, run exact-commit Steward and CodeRabbit reviews, then open
   the implementation PR.

## Constraints and known limits

- Ads require explicit test settings and the configured loopback fixture origin;
  general Radio Browser stations are excluded. No production advertising traffic.
- The supported media is clear single-rendition AAC-LC MPEG-TS at 48 kHz stereo.
- Published media uses a bounded recent output history. Active requests remain
  pinned while in flight, but routes from a playlist older than the history
  window can return 404 as source bytes and creative pins are released.
- Preroll adds latency. No catch-up, arbitrary truncation, or programme deletion.
- Kodi clock calibration, audible output verification, and final review gates are
  outstanding. The new PR has not been opened.

## Follow-up — 2026-10-23

The session-wide published-ID retention limit is resolved with a bounded recent
output history. Active cached requests stay retained while in flight, and stale
source bytes and creative pins are released as the output window advances. A
rolling fixture test now advances beyond the former source-segment limit without
stopping playback. Dialog-only labeling/settings actions no longer finish as
successful empty folders. Kodi GUI playback acceptance remains outstanding.

---

# Task 7 — Kodi adapter and explicit test settings

> Historical record: Task 8 later enabled the fixture-only raw-clock mapper
> described above. Statements in this section about disabled mapping and
> success beacons describe the earlier Task 7 state only.

## Result

- Added opt-in `ad_enabled`, `ad_preroll`, `ad_midroll`, and `ad_vast_url` settings. The VAST URL must be on the exact configured loopback fixture origin. Defaults disable insertion. General Radio Browser stations remain excluded by the session's fixture-origin and test-policy checks.
- Added `KodiAdPlayback(manager, player)` with 250 ms service sampling, session-scoped pause/resume/seek/end forwarding, stale-session checks, `AdTracker`, `BeaconWorker`, and diagnostic `Event` emission. The output clock mapping is explicitly disabled. `enable_mapping(mapper)` is the sole hook for Task 8 after target-Kodi validation; no successful milestone beacons are emitted before that validation.
- Captured ad configuration when a station selection starts; ad-only edits update later selections without restarting the service or changing the active session. Existing diagnostics/test-origin changes still restart it. A malformed ad-only edit leaves the running service and its previous valid configuration intact.
- Raised the fixture HLS ad-create control timeout to 30 seconds while preserving cancel polling. Passed the writable add-on profile as `cache_root`; service startup cleans abandoned directories inside the owned creative-cache namespace.
- Surfaced typed preparation `AdError`s from `AdSession` through the session to error-beacon dispatch and sanitized diagnostics, including early playback failure. Updated the package source manifest to include all ad modules and the Kodi adapter.

## TDD evidence

- RED: `python3 -m pytest tests/test_kodi_ad_player.py tests/test_kodi_runtime.py -q` failed collection with `ModuleNotFoundError: No module named 'kodi_ad_player'`.
- RED: `python3 -m pytest tests/test_kodi_runtime.py::test_invalid_ad_only_edit_does_not_restart_running_service -q` failed because a malformed VAST setting generated a service error notification.
- RED: `python3 -m pytest tests/test_package.py::test_package_is_installable_source_only -q` failed because the package manifest omitted seven advertising modules.
- RED: `python3 -m pytest tests/test_kodi_runtime.py::test_player_refuses_ad_sample_when_file_has_switched_before_callback -q` failed because `RelayPlayer.sample_ads` was absent.
- RED: `python3 -m pytest tests/test_kodi_ad_player.py::test_early_playback_error_flushes_preparation_error -q` failed because the queued error was not dispatched on early end.
- RED: `python3 -m pytest tests/test_sessions.py::test_inflight_create_keeps_settings_from_selection_time -q` failed because the in-flight selection read the later settings snapshot.
- GREEN: `python3 -m pytest tests/test_kodi_ad_player.py tests/test_kodi_runtime.py tests/test_sessions.py tests/test_ad_tracking.py tests/test_ad_relay.py tests/test_package.py -q` — 109 passed in 10.96 seconds.
- GREEN: `python3 -m compileall -q plugin.audio.kodi_donor_radio/resources/lib` and `git diff --check` — no output, exit 0.

## Interfaces and remaining validation

`KodiAdPlayback.enable_mapping(mapper)` expects a validated callable that maps `(session, presentation, kodi_get_time)` to presentation seconds or `None`. It is never called by the current runtime. `PlaybackSession.take_ad_errors()` drains typed errors; `AdSession` reports them through an optional `on_error` callback. `CreativeCache.cleanup_abandoned(profile)` acts only inside `controlled-ad-creative-cache`.

The 109-test focused run covers adapter/runtime/session/tracking/relay/package behavior. A full suite and actual Kodi playback remain for controller/Task 8. The existing UI fixture menu points to older observation endpoints, while `tools/ad_fixture_server.py` uses `/source/<scenario>/index.m3u8`; Task 8 must add a usable controlled-ad fixture station entry or otherwise launch its source through the UI. Source analysis alone did not establish a safe Kodi clock origin, so success tracking stays disabled.

## Self-review

The adapter never calls Kodi from the beacon HTTP worker; player reads occur in the service sampling path. Stale finish callbacks retain the existing `isPlaying()` guard and session identity. Pauses, seeks, and source end are forwarded to the tracker, but target-Kodi timing behavior still requires Task 8 measurement before mapping activation. No production advertising origin is allowed.
