# Controlled VAST ads — implementation checkpoint

Status: work in progress, not release-ready. Branch: `codex/vast-controlled-ads`.

Checkpoint verification: `python3 -m pytest -q` passed **513 tests in 74.81 seconds**,
exit 0, against implementation commit `beec95b`. No server traceback appeared in
this run. The following checkpoint commit changes documentation only.

## Completed and task-reviewed

Tasks 1–6 implement controlled audio/VAST fixtures, bounded VAST resolution,
creative validation/cache, immutable HLS composition, relay/session integration,
and playback-evidenced tracking. Relay tests independently decode preroll and
midroll media and verify source fallback. Task reviews found and corrected
published-media retention, cache-failure fallback, and false completion after a
seek followed by stationary playback samples.

## Implemented, awaiting review

Task 7 integrates Kodi settings and playback callbacks. Its implementation report
is retained below. Successful tracking remains disabled until empirical clock
mapping is established. The focused adapter/runtime/session/tracking/relay/package
run reported 109 passing tests at commit `beec95b`.

## Next steps

1. Review Task 7 and fix verified findings.
2. Add usable UI entries for the ad fixture server's source endpoints.
3. Run controlled ad acceptance on unpatched Kodi master `434da3c`: normal ads,
   fallback cases, pause, buffering, seek, station switch, and stop. Verify the
   clock mapping across ad discontinuities and advancing live windows before
   enabling successful tracking. The earlier master playback results cover the
   observation POC, not this new insertion implementation.
4. Complete independent smoke tooling, record decoded audio and playback evidence,
   and distinguish that evidence from speaker output actually heard or captured.
5. Finish documentation/versioning, full tests, ZIP validation, and the official
   Kodi addon checker. Obtain current exact-commit Steward primary/adversarial
   and CodeRabbit passes before opening the implementation PR.

## Constraints and known limits

- Ads require explicit test settings and the configured loopback fixture origin;
  general Radio Browser stations are excluded. No production advertising traffic.
- The supported media is clear single-rendition AAC-LC MPEG-TS at 48 kHz stereo.
- Published media is retained for the session. Hard cache caps can terminate new
  manifests on long sessions; previously published resources remain available
  until stop or idle expiry. There is no inferred playback-based eviction.
- Preroll adds latency. No catch-up, arbitrary truncation, or programme deletion.
- Kodi clock calibration, audible output verification, and final review gates are
  outstanding. The new PR has not been opened.

---

# Task 7 — Kodi adapter and explicit test settings

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
