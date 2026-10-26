# Radio Directory Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make country/tag, place, and map browsing resilient to brief network outages without storing stream URLs or persisting search results.

**Architecture:** Add one bounded, versioned profile cache that stores only validated directory names and `StationSummary` records with expiration timestamps. Wire weekly category data and daily place/map pages through it; keep search-result caching in Kodi's in-memory window properties only and fall back to cached display rows when directory requests fail.

**Tech Stack:** Kodi-compatible Python, standard library, existing profile file-lock/atomic-write helpers, pytest.

**Spec:** `docs/donor-radio-spec.md`, sections “Caching” and “Local state”.

## Global Constraints

- Tag and country lists: cache for a week.
- A place's station list: cache for a day, keyed on place id and radius.
- Search results: cache for the session only.
- Map pin data is keyed on the view window.
- Persist station UUIDs, never stream URLs.
- Favorites render from their local profile store before optional background refresh.
- Background favorite refresh resolves station UUIDs in batches, preserves order and `last_played`, and applies no partial update on failure.
- Keep network operations bounded and Kodi's UI responsive.
- Profile state uses a `schema_version` integer, private profile permissions, and atomic writes.
- Cache failures must not prevent ordinary browsing or playback.

## Review Focus

- A malformed or oversized cache file must not be trusted or overwritten as valid data; test invalid schemas, duplicate keys, and size limits.
- Expired values must miss and be replaceable; test exact TTL boundary behavior.
- Place radius changes and map-window changes must never reuse unrelated station rows; test key separation.
- Station URLs must not appear in serialized cache bytes; test with a secret URL query.
- Network failure with valid cached data should render cached display rows; without cache it should preserve the existing error/empty-state distinction.

---

### Task 1: Bounded Profile Directory Cache Store

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py`
- Test: `tests/test_radio_ui.py`

**Interfaces:**
- Consumes: existing `StationSummary` and `profile_file_lock`.
- Produces: `DirectoryCachePage(kind, rows, continuation, stored_at, fresh)` and `DirectoryCacheStore(profile_path, clock=time.time)` with `get(kind, key, ttl_seconds, allow_stale=False) -> DirectoryCachePage | None` and `put(kind, key, rows, continuation=None, stored_at=None) -> None`. Task 2 extends continuation validation to preserve the tag endpoint's boolean raw-row `has_more` value.
- Allowlisted kinds are `countries`, `tags`, `place`, and `map`. Country rows are `(display_name, ISO-3166-1 alpha-2 code)` pairs; tag rows are bounded strings; place/map rows are `StationSummary` objects. Place entries may carry a nonnegative raw-row continuation offset; tag entries may carry the exact boolean `has_more` from the raw response; countries/map entries carry no continuation. Keys are hashed before disk storage.
- Store version is 1, file is `directory_cache.json`, maximum file size is 2 MiB, maximum entries is 100, and station rows per entry are capped at 200. A future timestamp is invalid; an expired entry is returned only when `allow_stale=True`.

- [x] **Step 1: Write failing tests** for round-trip/expiry, stale fallback, URL-free station serialization, hashed key isolation, malformed/duplicate schema rejection, and file/item bounds.
- [x] **Step 2: Run the focused tests** and confirm the new store interface is missing.
- [x] **Step 3: Implement the store** using a versioned `directory_cache.json`, existing cross-process profile lock and atomic replacement, bounded item counts/file bytes, and immutable validated values.
- [x] **Step 4: Run `pytest tests/test_radio_ui.py -q`** and confirm all focused tests pass.
- [x] **Step 5: Commit** as `feat: add safe profile directory cache`.

### Task 2: Directory Runtime Cache Integration

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py` (preserve tag-page continuation metadata)
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_browser.py` only if a small session-cache interface is needed
- Test: `tests/test_kodi_runtime.py`, `tests/test_radio_browser.py`
- Test: `tests/test_radio_ui.py`

**Interfaces:**
- Consumes: Task 1 `DirectoryCacheStore`; current directory page methods and `StationSummary` conversion. This task extends `DirectoryCachePage.continuation` validation to allow boolean `has_more` only for `tags`, preserving integer offsets only for `place`.
- Produces: runtime behavior caching countries and tags for 7 days; place result pages for 1 day keyed by place UUID, radius, and raw continuation offset; map pin pages for 1 day keyed by country and exact `MapView` bounds/canvas; search pages only in bounded JSON held by `xbmcgui.Window(10000)` memory properties, never in profile files.

- [x] **Step 1: Write failing tests** in `tests/test_kodi_runtime.py` proving (a) cached countries and tags suppress repeated network fetches while expired entries refetch, (b) place cache misses on a changed UUID/radius/raw continuation offset and stale data is shown when offline, (c) map cache reuses only an identical country + `MapView` key and shows stale pins after network failure, (d) a search page survives a second plugin invocation via the Kodi window property without writing search results or stream URLs to profile files, (e) a cached station remains a display summary while `?mode=play&uuid=...` still resolves through `by_uuid`, and (f) a cached tag page retains raw-response `has_more` even when malformed rows were filtered.
- [x] **Step 2: Run focused tests** and confirm the integration behavior fails before implementation.
- [x] **Step 3: Add cache reads/writes around directory fetches** while preserving current cancellation, pagination, UI responsiveness, and error handling. Cached station rows remain `StationSummary` items; playback continues to resolve the current station by UUID. Map cache keys use canonical finite numeric bounds and canvas values from `MapView`. Search cache entries are bounded to 64 pages and 512 KiB total and are removed from the window property when invalid. Preserve tag-page `has_more` as boolean metadata, not a fake tag row.
- [x] **Step 4: Run `pytest tests/test_kodi_runtime.py tests/test_radio_browser.py -q`, then `pytest -q`, `python tools/check_package.py`, the repository's Kodi addon checker, and `git diff --check`; confirm all pass.**
- [x] **Step 5: Commit** as `feat: cache radio directory pages safely`.

### Task 3: Quiet Background Favorite Refresh

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_browser.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Test: `tests/test_radio_browser.py`, `tests/test_radio_ui.py`, `tests/test_kodi_runtime.py`

**Interfaces:**
- Consumes: local-first `FavoriteStore`, `StationSummary`, and Kodi's in-memory `Window(10000)` properties.
- Produces: `RadioBrowser.by_uuids(station_uuids)` resolving 1–50 validated UUIDs per request in request order; `FavoriteStore.refresh(summaries)` atomically updates display metadata for matching favorites while preserving order and `last_played`; hidden `?mode=favorites_refresh` route and a session-only visible-route property.
- Favorites listing renders the local records before it schedules one nonblocking `RunPlugin` refresh per Kodi session. Refresh resolves all favorites in batches of 50; a failed batch performs no profile update and clears the session throttle so a later Favorites visit can retry. Missing UUIDs stay saved with their previous details. A successful refresh asks Kodi to refresh the container only if the current visible route is still Favorites.

- [x] **Step 1: Write failing tests** proving the favorites listing renders stored rows before scheduling a refresh; refresh is scheduled once per Kodi session; UUID batches are bounded at 50 and keep request order; refreshed display fields update without changing favorite order or `last_played`; missing station records remain saved; a failed later batch leaves all favorites unchanged and clears the throttle; and container refresh occurs only while Favorites remains the visible route.
- [x] **Step 2: Run `pytest tests/test_radio_browser.py tests/test_radio_ui.py tests/test_kodi_runtime.py -q -k favorite_refresh`** and verify the new batch endpoint, atomic refresh, and runtime route are missing.
- [x] **Step 3: Implement a quiet nonblocking refresh route** launched through `RunPlugin` after local favorite rows render. Keep refresh status and visible-route state only in Kodi in-memory window properties, resolve all favorite IDs before committing any changes, and request `Container.Refresh` only if Favorites is still visible. Network or storage failures stay silent and leave local favorites usable.
- [x] **Step 4: Run `pytest tests/test_radio_browser.py tests/test_radio_ui.py tests/test_kodi_runtime.py -q`, then `pytest -q`, `python3 tools/package_addon.py --output dist/favorite-refresh`, `python3 tools/check_package.py dist/favorite-refresh/plugin.audio.kodi_donor_radio-0.1.0.zip`, the Kodi add-on checker on a fresh extraction, and `git diff --check`; confirm all pass.**
- [x] **Step 5: Commit** as `feat: refresh favorite station details in background`.

