# Radio Browser HLS Ad Marker Sampler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded developer tool that samples Radio Browser `cdnstream1.com` HLS stations for at least 20 minutes and emits redacted ad-break observations.

**Architecture:** Add a standalone tool under `tools/` that reuses `RadioBrowser`, `HttpClient`, `parse_playlist`, and `CueTracker`. Scan a bounded number of directory pages, sample selected HLS stations with bounded concurrency, and write allowlisted JSON Lines to stdout without reading continuous-media bodies, fetching media segments, or changing Kodi runtime behavior.

**Tech Stack:** Python 3.8+, standard library, existing add-on transport/parser/tracker modules, pytest, loopback HTTP fixtures.

**Spec:** `docs/superpowers/specs/2026-10-25-radio-browser-hls-sampler-design.md`

## Global Constraints

- Runtime dependency floor remains Python 3.8; add no third-party dependency.
- The sampler lives in `tools/` and is excluded from the add-on package manifest.
- Each selected station is sampled for at least 1,200 seconds; CLI default is 1,200 seconds.
- Directory scanning, candidates, workers, request timeouts, playlist size, and polling are bounded.
- Scan 20 directory pages by default (maximum 100), select eight candidates by default (maximum 20), and use at most four workers.
- Sample for 1,200 seconds by default (minimum 1,200, maximum 7,200); cap each HTTP request at a 15-second total deadline and poll every 2–10 seconds.
- Emit at most 1,024 marker events per station and bound station names to 128 characters; indicate event truncation in the station summary.
- Requests use the existing `HttpClient` and URL policy; redirects are revalidated.
- Only playlist responses with an allowlisted HLS `Content-Type` are read; audio, unknown, or missing content types are rejected before reading their bodies. No segment downloads, VAST requests, beacons, or Radio Browser click requests.
- Output is JSON Lines on stdout and omits stream URLs, query strings, segment paths, opaque AdsWizz values, and raw cue payloads.
- A marker-free sample is reported as `no_marker_observed`, never as proof that the station has no ads.

## Review Focus

- A lookalike host such as `cdnstream1.com.attacker.example` must not enter the candidate set; test exact-domain and subdomain matching.
- Master playlists with hostile query values or excessive nesting must not leak URLs or exceed the supported depth; test both.
- HLS cue attributes and AdsWizz context can contain opaque values; output must omit them; test adversarial fixture values.
- Overlapping windows and sequence resets must preserve CueTracker deduplication and unresolved behavior; test repeated windows and `test_sampler_epoch_resets_do_not_join_ad_breaks`.
- Cancellation during a blocking playlist request must stop workers and retain already emitted JSON Lines; test a cancellable local server response.

---

### Task 1: Bounded Radio Browser candidate discovery

**Files:**
- Create: `tools/sample_hls_ad_markers.py`
- Test: `tests/test_hls_sampler.py`

**Interfaces:**
- Consumes: `RadioBrowser.search_page(name, country, tag, offset, limit)` and `Station` from `models`.
- Produces: `host_is_cdnstream1(url: str) -> bool` and `discover_candidates(browser: RadioBrowser, page_limit: int, candidate_limit: int) -> Tuple[List[Station], int]`, using the raw-row continuation flag so malformed rows cannot truncate discovery.
- Implementation bounds: page size 50; `page_limit` 1–100; `candidate_limit` 1–20. Deduplicate by station UUID and retain only `station.hls` entries with exact `cdnstream1.com` host or a subdomain.

- [x] **Step 1: Write failing tests**

  Add `test_host_filter_requires_cdnstream1_domain_boundary` for exact host, subdomain, lookalike suffix, malformed URL, and non-HTTP input. Add `test_discovery_filters_hls_deduplicates_and_stops_at_bounds` to assert page offsets, `hls` filtering, UUID deduplication, candidate cap, and page cap.

- [x] **Step 2: Run the tests and verify they fail**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: FAIL because the sampler module and discovery functions do not exist.

- [x] **Step 3: Implement candidate filtering and bounded pagination**

  Add the exact interfaces above. Parse the candidate host with `urlsplit`, lowercase it, require HTTP(S), and check `hostname == "cdnstream1.com" or hostname.endswith(".cdnstream1.com")`. Request pages of 50 rows from offset 0 in page-size increments; use the upstream raw-row continuation flag, not parsed station count, to detect the final page. Stop when continuation is false or once the page/candidate bound is reached.

- [x] **Step 4: Run the focused tests**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: PASS for host boundary, malformed URL, deduplication, and bounds.

- [x] **Step 5: Commit the discovery slice**

  ```bash
  git add tools/sample_hls_ad_markers.py tests/test_hls_sampler.py
  git commit -m "feat: discover bounded cdnstream1 HLS candidates"
  ```

### Task 2: Playlist-only sampling and redacted cue events

**Files:**
- Modify: `tools/sample_hls_ad_markers.py`
- Test: `tests/test_hls_sampler.py`

**Interfaces:**
- Consumes: Task 1 discovery helpers, `parse_playlist`, `CueTracker`, and `HttpClient`.
- Produces: `load_media_playlist(url: str, client: HttpClient, cancel: Event, max_depth: int = 3) -> Playlist`; `sample_station(station: Station, client: HttpClient, writer: JsonLineWriter, duration_seconds: int, cancel: Event, monotonic: Callable, wait: Callable) -> Dict[str, object]`.
- `JsonLineWriter.emit_record(record: Mapping[str, object]) -> None` writes bounded JSON Lines under a lock. A per-station `_StationEventSink(station: Station, writer: JsonLineWriter)` implements `emit(event: Event) -> None` for `CueTracker` and projects only allowlisted fields, including a bounded sanitized station name. The writer caps marker event records at 1,024 per station and exposes a truncation flag for the station summary.

- [x] **Step 1: Write failing loopback tests**

  Add tests named `test_sampler_emits_hls_cue_events_without_fetching_segments`, `test_sampler_emits_adswizz_hls_events_across_overlapping_windows`, `test_sampler_redacts_urls_context_and_cue_payloads`, `test_sampler_reports_unresolved_break_on_cancel`, `test_sampler_epoch_resets_do_not_join_ad_breaks`, and `test_master_playlist_depth_is_bounded`. Use a local HTTP server with manifests that include query tokens, opaque AdsWizz context, a raw SCTE payload, explicit cue boundaries, and segment URLs; assert no media segment route is requested.

- [x] **Step 2: Run the sampler tests and verify they fail**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: FAIL because playlist polling, tracking, and the redacted sink are not implemented.

- [x] **Step 3: Implement bounded HLS playlist loading and event projection**

  Fetch the directory station URL directly as an HLS playlist through `HttpClient` and the existing URL policy; do not use generic `resolve_station`, which can follow ordinary M3U/PLS entries and read continuous media. Require an allowlisted HLS playlist `Content-Type` before reading any response body; reject audio, unknown, and missing content types without reading them. Then validate the HLS signature and reject invalid playlist bodies without following their targets. Follow at most three HLS playlist levels and select the first declared playlist reference. Refresh only the selected media playlist. Parse every body with `read_bounded` and `parse_playlist`; pass `deadline=time.monotonic() + 15` to each `HttpClient.open` call. Start the observation clock only after the first media playlist is loaded, so request startup does not reduce the caller-requested sample duration. Send media playlists to one `CueTracker` per station with a stable rendition ID and monotonically increasing epoch when a non-overlapping media-sequence reset is detected. Derive the poll wait from half the target duration, clamped to 2–10 seconds.

  Project only `cue_observed`, `break_start`, `break_update`, `break_end`, and `break_unresolved`. Allow fields `station_uuid`, sanitized `station_name`, `kind`, `source`, `observed_at`, `media_sequence`, `discontinuity`, `media_position`, `duration`, `classification`, and `reason`. Never serialize `Event.evidence`, raw exception text, URLs, or unlisted fields. On normal completion or cancellation, call `CueTracker.finish` so open breaks are unresolved rather than assigned an invented end.

- [x] **Step 4: Run the focused loopback tests**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: PASS; only manifest routes are requested and captured output contains none of the fixture secrets or URLs.

- [x] **Step 5: Commit the sampling slice**

  ```bash
  git add tools/sample_hls_ad_markers.py tests/test_hls_sampler.py
  git commit -m "feat: sample HLS ad markers without media downloads"
  ```

### Task 3: Bounded CLI orchestration and operator documentation

**Files:**
- Modify: `tools/sample_hls_ad_markers.py`
- Modify: `tools/README-fixtures.md`
- Modify: `docs/testing.md`
- Test: `tests/test_hls_sampler.py`

**Interfaces:**
- Consumes: Task 1 candidate discovery and Task 2 `sample_station`.
- Produces: `main(argv: Optional[List[str]] = None) -> int` with options `--pages` (default 20, max 100), `--limit` (default 8, max 20), `--workers` (default 4, max 4), and `--duration-seconds` (default 1,200, minimum 1,200, maximum 7,200). Poll intervals clamp to 2–10 seconds; marker events cap at 1,024 per station.
- Each worker receives an independent cancellation event and emits through the shared thread-safe JSON Lines sink. Completion summaries use only `marker_observed`, `no_marker_observed`, `unsupported`, `policy_rejection`, `reachability_failure`, `parser_failure`, or `cancelled` outcomes.

- [x] **Step 1: Write failing CLI and concurrency tests**

  Add `test_cli_rejects_duration_below_twenty_minutes`, `test_cli_applies_page_candidate_and_worker_bounds`, `test_cli_caps_concurrent_station_workers`, and `test_marker_free_sample_is_not_reported_as_ad_free`. Replace Radio Browser and sampling functions with deterministic fakes; verify JSON Lines output and exit codes.

- [x] **Step 2: Run CLI tests and verify they fail**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: FAIL because the argument parser and orchestration are incomplete.

- [x] **Step 3: Implement bounded CLI and cancellation lifecycle**

  Add the exact options and bounds above. Scan only the configured page count, then use a `ThreadPoolExecutor` with at most four workers. On Ctrl-C, set the shared cancellation signal, wait for workers to close requests and emit unresolved endings/summaries, and return a nonzero status. Flush each completed JSON Lines record so callers redirecting stdout can inspect observations during the run. Do not add output-file creation.

- [x] **Step 4: Document use and limitations**

  Document a command that scans 20 pages, selects up to eight stations, runs four concurrent workers, and samples each for 1,200 seconds. State that output is redacted JSON Lines, no media segments are downloaded, and `no_marker_observed` describes only the sampled interval.

- [x] **Step 5: Run focused and full verification**

  Run: `python3 -m pytest tests/test_hls_sampler.py -q`
  Expected: PASS for all sampler tests.

  Run: `python3 -m pytest -q`
  Expected: full repository suite passes.

  Run: `python3 -m compileall -q plugin.audio.kodi_donor_radio/resources/lib tests tools && git diff --check && python3 tools/package_addon.py --output dist && python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip`
  Expected: all commands exit 0; tool remains outside the package manifest.

- [x] **Step 6: Commit the CLI and documentation slice**

  ```bash
  git add tools/sample_hls_ad_markers.py tools/README-fixtures.md docs/testing.md tests/test_hls_sampler.py
  git commit -m "feat: add long Radio Browser HLS marker sampler"
  ```
