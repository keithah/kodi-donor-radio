# Kodi Radio GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first usable Kodi Donor Radio GUI slice: skin-native discovery listings, saved places and favorites, guided setup, and one shared map window, while preserving deliberate playback/click accounting and existing observation-only ICY behavior.

**Architecture:** Pure Python modules own bounded profile persistence, station display records, place filtering, and map projection/navigation. The Kodi plugin layer builds native directory items, settings and dialogs, while a single Kodi `WindowXML` map is launched from either interface mode and reuses station/action routes. Persist station UUIDs and safe display metadata only; resolve stream URLs through the existing deliberate-play route.

**Tech Stack:** Python 3.8+ standard library, Kodi 21.3 Python API, Kodi settings/localization XML, pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-kodi-radio-gui-design.md`; product requirements and acceptance: `docs/donor-radio-spec.md`.

**Status:** User-approved design and implemented first slice: skin-native listings,
sequential Kodi setup dialogs, and the shared map. Automated and package checks are
complete; Kodi 21.3 live acceptance is partial (listings, station-context and
root map launch, preview staying in the map, and return selection observed).
Setup completion, full D-pad/focus matrix, playback/click accounting, and
controlled fixture audio remain pending. Themed windows remain deferred until
first-slice acceptance is complete.

## Global Constraints

- Skin listings are the default and are rendered by the active Kodi skin. Do not implement themed home/listing windows in this slice.
- Keep Radio Browser HTTP work in the existing cancellable `_operation` boundary; do not block Kodi's UI thread.
- Only the deliberate `play` action resolves a station URL and reports one directory click. Browsing, map selection, and previews must never report a click.
- Do not persist station stream URLs, VAST data, or credentials in favorites/place files. Resolve stations by UUID when needed; cached display fields are safe metadata only.
- Preserve the existing playback relay, ICY observation/learning, fixture origin restrictions, and ad behavior. This GUI slice must not change ad eligibility or beacon semantics.
- Bound profile data, Radio Browser page sizes, map pins, and UI-created controls. Validate routes and persisted values as untrusted input.
- Missing coordinates, favicons, city names, and offline results must degrade to clear labels/fallback artwork and explicit empty/error states; they must not prevent normal playback.
- Use Kodi-native APIs and skin-independent `WindowXML` resources for the shared map. No Kodi core/skin changes, external dependencies, or internet-origin ad calls.
- Use test-first changes: add focused failing tests, run them to confirm the failure, implement the smallest slice, then rerun targeted and full checks.

## Review Focus

- UUIDs are the only station identity; persisted metadata must not accidentally include the station URL or volatile service credentials.
- Place radius filtering handles absent/invalid coordinates and antimeridian distances without silently excluding valid nearby stations.
- ListItem folder/playable semantics, context menu routes, and station labels/artwork match the product spec; raw URLs never reach directory items.
- Guided setup cancellation is safe at every dialog and leaves playback untouched. Completing setup writes atomically and refreshes the container.
- Map navigation is deterministic, bounded, non-wrapping, D-pad reachable, and restores the originating directory selection; stale/cancelled requests cannot replace a newly focused city's stations.
- Map previews are debounced/cancellable and never call Radio Browser's click endpoint. Preview does not stop unrelated playback unless the approved preview behavior explicitly owns that station session.
- Settings XML and localized strings are valid and translated; Kodi minimum-version behavior is verified against 21.3, not inferred only from master.
- Package archive contains all runtime XML/assets and excludes tests, fixtures, development scripts, and credentials.

---

### Task 1: Add safe station display records and profile stores

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/models.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_browser.py`
- Create: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py`
- Test: `tests/test_radio_ui.py`
- Test: `tests/test_radio_browser.py`

**Interfaces:**
- Extend `Station` with optional safe directory fields (`country`, `country_code`, `state`, `city`, `language`, `tags`, `favicon`, `bitrate`, `latitude`, `longitude`, `click_count`, `votes`) defaulting so all existing five-positional-argument construction remains valid. Validate and bound values while parsing Radio Browser responses; keep `url` transient and excluded from any profile serialization.
- Add `RadioBrowser.by_uuid(station_uuid) -> Station`, using Radio Browser's UUID lookup and the same bounded parser; reject invalid UUIDs and return a clear not-found error when no exact UUID match is returned. The deliberate-play route will resolve listings by UUID in Task 3.
- `StationSummary` is the persisted/display-only record keyed by bounded `station_uuid`, with `name`, optional city/country/codecs/tags/favicon/coordinates/bitrate and last-played timestamp; it has no URL field.
- `PlaceStore(profile_path)` exposes `list()`, `save(name, latitude, longitude, radius_km, make_default=False)`, `set_default(place_id)`, `set_radius(place_id, radius_km)`, and `remove(place_id)` over a versioned JSON schema, atomically replaced.
- `FavoriteStore(profile_path)` exposes `list()`, `add(station_summary)`, `remove(station_uuid)`, `move(station_uuid, index)`, and `mark_played(station_uuid, played_at)` while retaining stable user order and bounded safe display data.
- Pure helpers `distance_km(lat1, lon1, lat2, lon2)` and `stations_in_radius(stations, place, radius_km=None)` provide validated, deterministic radius filtering.

- [x] **Step 1: Add failing tests** for Radio Browser optional field parsing/bounds, UUID lookup success/no match/malformed response/invalid UUID, five-argument Station compatibility, safe summary round-trip without a URL, store schema/version validation, atomic writes, limits, default-place uniqueness, stable favorite ordering, radius edges, invalid coordinates, and antimeridian proximity.
- [x] **Step 2: Run** `python3 -m pytest tests/test_radio_ui.py tests/test_radio_browser.py -q` and verify the new cases fail on missing APIs/behavior.
- [x] **Step 3: Implement the records and stores** with strict schema validation, profile mode `0700`, files mode `0600`, same-directory temporary file + `os.replace`, and bounded item counts/strings. Keep serialization allowlisted.
- [x] **Step 4: Implement distance/filter helpers** with haversine distance and deterministic tie sorting; return no nearby stations if coordinates are missing rather than guessing.
- [x] **Step 5: Run** the targeted tests and nearby station/radio-browser tests; verify all cases pass.
- [x] **Step 6: Commit** as `feat: add saved radio places and favorites`.

### Task 2: Add UI settings and localized labels

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/settings.xml`
- Modify: `plugin.audio.kodi_donor_radio/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_kodi_runtime.py`

**Interfaces:**
- Add `guided_setup` (false), `offer_setup_row` (true), and `preview_on_browse` (true for the map) settings. Keep skin listings as the only selectable interface until themed screens are implemented; defer `interface` and `theme_open_on` so users cannot select nonfunctional UI. This deferral is recorded in settings help and the roadmap.
- Add localized strings for root rows, place/favorite/group listings, station information/context actions, place actions, guided setup prompts, radius units, map controls/status, and user-facing empty/offline/error/loading states.
- Do not expose interface or theme-entry settings before the themed windows exist; a setting that does nothing would mislead users. Add them with the themed-window implementation.

- [x] **Step 1: Add failing settings/localization tests** for defaults, enum values, unique string IDs, XML validity, every referenced label's translation, and test-mode/ad setting compatibility.
- [x] **Step 2: Run** `python3 -m pytest tests/test_kodi_runtime.py -q` and confirm failures for missing settings and translations.
- [x] **Step 3: Implement the GUI settings/strings**; expose only the approved skin-mode settings until theme screens ship and document this deliberate deferral in settings help.
- [x] **Step 4: Run** the targeted settings/localization tests.
- [x] **Step 5: Commit** as `feat: add radio interface settings`.

### Task 3: Build skin-native root and browsing listings

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_browser.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_kodi_runtime.py`
- Test: `tests/test_radio_browser.py`
- Test: `tests/test_radio_ui.py`

**Interfaces:**
- Extend the plugin router using the product spec's `mode=` parameter for `setup`, `place`, `favorites`, `tag`, `country`, `search`, `map`, `station_info`, and `play`. Accept the existing `action=` parameter temporarily for backward compatibility. Route payloads use validated place IDs, station UUIDs and bounded query/page parameters; do not serialize raw station URLs or full station records into route strings. Ordinary `mode=play&uuid=<stationuuid>` performs bounded `RadioBrowser.by_uuid(station_uuid)` resolution only on deliberate selection. Controlled test entries use a fixed fixture identifier (`mode=play_fixture&id=<known-id>`) and construct the station from a hardcoded fixture table after the exact-origin guard; never put fixture stream URLs in plugin route strings.
- Add bounded geographic discovery for place listings using Radio Browser's documented `countrycode` and `has_geo_info` filters plus local radius filtering; cap each user request to a fixed scan budget and keep a continuation offset so large regions remain cancellable and pageable. Do not claim the upstream directory exposes a direct radius query.
- Persist the selected country's two-letter code with each guided-setup place when available so geographic searches can be country-scoped. Migrate old version-1 records as empty country code.
- Extend `SavedPlace` and `PlaceStore.save(..., country_code='')` with optional validated two-letter country code; existing calls and version-1 records without the field continue to work.
- Root rows are setup (when enabled), one per saved place, favorites, browse by genre, browse by country, search, open map, settings, and the existing marker-label action. Keep the marker-label item available during playback without breaking navigation.
- Build station ListItems with label=name, label2=city then codec/bitrate, `thumb`/`icon` from the bundled generic DR tile, music info from tags/comment, and `IsPlayable=true`; remote artwork is deferred until safe validated fetching and local caching exist. The route resolves the UUID to a transient station record before using existing playback.
- Keep folders `isFolder=True`; station selections use `setResolvedUrl` through the deliberate play route. Use no click call for list construction or lookup.
- Preserve paging and container cache disabled for mutable directory results.

- [x] **Step 1: Add failing route/render tests** for data-dependent root ordering, folder/playable flags, labels/artwork fallback, UUID-only real station routes, fixed-ID loopback fixture routes, mode/action backward compatibility, route UUID validation, tags/countries paging, search cancellation, offline cached saved rows, empty/error/loading states, and zero clicks except deliberate play.
- [x] **Step 2: Run** `python3 -m pytest tests/test_kodi_runtime.py -q` and verify new route/render assertions fail.
- [x] **Step 3: Implement** small route helpers and ListItem construction. Use cancellation-aware browser calls and map transient station details by UUID; never put the stream URL into a visible item or profile file.
- [x] **Step 4: Run** the runtime, Radio Browser, and package tests.
- [x] **Step 5: Commit** as `feat: add skin-native radio discovery listings`.

### Task 4: Add station/place actions and sequential guided setup

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_kodi_runtime.py`
- Test: `tests/test_radio_ui.py`

**Interfaces:**
- Station context actions: Play, Add to/Remove from favorites, Show on map, Station info. Use `ListItem.addContextMenuItems` with Kodi built-in `RunPlugin(...)` actions and the same route handlers as ordinary selections.
- Place context actions: Make default, Change radius, Remove. Confirm destructive removal with a Kodi dialog; if the default is removed, select a deterministic remaining default or none.
- Setup is a sequential `xbmcgui.Dialog` flow: search/select country or city, choose from matching city candidates with valid coordinates, choose a radius, optionally add another place, then save. Every cancel path aborts without partial writes and does not stop playback.
- On completed setup, persist selected places atomically and refresh the current container. “Run setup from Settings” opens the same flow.
- Playback updates favorite `last_played` only after deliberate route selection; preview paths remain excluded.

- [x] **Step 1: Add failing tests** for all context route arguments, favorite add/remove/order, place default/radius/remove, each cancellation point, invalid/missing coordinate rejection, complete multi-place setup, refresh, and playback timestamp update versus preview.
- [x] **Step 2: Run** targeted runtime/store tests and confirm these assertions fail.
- [x] **Step 3: Implement** context menu construction and dialog flow with validation before writes. Keep each save atomic and leave current listing/playback intact when cancelled.
- [x] **Step 4: Run** targeted tests plus existing runtime/ad/ICY tests to show no playback behavior changed.
- [x] **Step 5: Commit** as `feat: add radio setup and station actions`.

### Task 5: Add pure map projection, pin selection, and D-pad navigation

**Files:**
- Create: `plugin.audio.kodi_donor_radio/resources/lib/radio_map.py`
- Test: `tests/test_radio_map.py`

**Interfaces:**
- `MapView` represents a validated geographic rectangle and pixel canvas; `project(latitude, longitude, view)` maps coordinates into bounded display positions.
- `cluster_cities(stations)` groups only valid coordinates and returns stable city/country clusters with station counts and UUIDs.
- `visible_cities(cities, view, selected_city=None, limit=60)` returns at most 60 deterministic pins, prioritizing the view center and then station count.
- `nearest_city_in_direction(cities, selected_id, direction)` chooses the nearest projected city inside the requested ±45-degree cone, returning `None` when no candidate exists. It never wraps.
- Map station selection and ordering reuse UUIDs and the shared UI station/action projection; projection and navigation do not import Kodi modules.

- [x] **Step 1: Add failing pure tests** for world and regional projection bounds, longitude/latitude edge handling, city clustering, duplicate/invalid coordinates, stable 60-pin capping, nearest directional selection, ties, no candidate, and non-wrapping behavior.
- [x] **Step 2: Run** `python3 -m pytest tests/test_radio_map.py -q` and verify failures due to absent module/API.
- [x] **Step 3: Implement** pure geometry and bounded stable selection.
- [x] **Step 4: Run** the map tests and static checks.
- [x] **Step 5: Commit** as `feat: add radio map navigation model`.

### Task 6: Implement the shared skin-independent Kodi map window

**Files:**
- Create: `plugin.audio.kodi_donor_radio/resources/lib/kodi_radio_map.py`
- Create: `plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_kodi_radio_map.py`
- Test: `tests/test_package.py`

**Interfaces:**
- `RadioMapWindow(xbmcgui.WindowXML)` loads the shared schematic equirectangular map; it displays a bounded pool of 60 city pins, selected-city stations in a rail, status/metadata, and reachable Back/Preview controls. Its constructor receives originating route, selected UUID/city, and display geometry; validate every ID before use.
- `onAction` handles directional city hopping using `radio_map.nearest_city_in_direction`, with no map wrap and no hover-only operations. Focus uses explicit navigation between pin area, station rail, and controls.
- Root `map` and station context `Show on map` launch the same window from both settings modes; Back closes it and restores the parent container/selection when still present.
- Missing background artwork uses an accessible, labeled schematic map rendered from bundled local XML controls; no remote map tiles or dependencies.
- Map preview defaults according to `preview_on_browse`, waits 400 ms of stable station focus, resolves the UUID only for playback, uses a distinct preview intent, never calls `RadioBrowser.click`, and cancels/stops only the preview session owned by this map window when focus changes/closes. Do not interrupt a separately selected station's playback.
- Any window-owned worker/request is cancelled on close; late responses are discarded by request generation ID.

- [x] **Step 1: Add Kodi-boundary tests** with fakes for WindowXML/control pool population, both map entry paths, D-pad transitions, no-wrap, focus/selection restoration, empty/offline states, preview debounce/cancel, stale response discard, and no click reports.
- [x] **Step 2: Run** `python3 -m pytest tests/test_kodi_radio_map.py -q` and verify failure before the window is implemented.
- [x] **Step 3: Implement** the XML and window/controller adapter using supported Kodi 21.3 APIs. Keep all coordinate selection in the pure map module and all network work cancellable.
- [x] **Step 4: Run** targeted map/runtime tests plus package validation.
- [x] **Step 5: Commit** as `feat: add shared Kodi radio map`.

### Task 7: Verify settings, packaging, and Kodi end-to-end acceptance

**Files:**
- Modify: `docs/testing.md`
- Modify: `README.md`
- Modify: `docs/donor-radio-spec.md` only where implementation status or accepted first-slice behavior needs updating
- Test: `tests/test_package.py`, `tests/test_kodi_runtime.py`, and all GUI tests

- [x] **Step 1: Add/complete package checks** proving settings/localization/map XML/assets ship and tests/fixtures/dev-only tools do not.
- [x] **Step 2: Run** `python3 -m pytest -q`, `python3 -m compileall -q plugin.audio.kodi_donor_radio tests`, `git diff --check`, and the repository's package-check command; retain exact output/results.
- [x] **Step 3: Build the installable add-on ZIP** and verify its manifest and contents.
- [ ] **Step 4: Install/run with Kodi 21.3** on Estuary and one alternate skin; verify skin-mode root and nested listings, setup cancellation/completion, map launch from root and station menu, pin/rail D-pad navigation without wrapping, return focus, settings, playback and click count, and preview cancellation/no-click behavior. Record Kodi version, display resolution, input method, and observed focus behavior. **Partial live result (2026-10-24):** Kodi 21.3.0, macOS 26.6.2 on MacBookPro18,2, 3840×2160 system display, keyboard via desktop automation. Estuary showed the root map entry and opened the map; Back returned to the same selected root row on repeat. Guided setup opened and cancellation returned to root without saving. A Toronto candidate and 25-mile radius were selected, with the place shown in the root listing, but a subsequent country-picker run ended with “No stations or categories found” and an empty root; end-to-end setup completion remains unverified. Andorra's no-coordinate scan displayed its expected no-location message. The add-on settings screen rendered its interface, preview, diagnostics, and fixture options. Earlier Confluence checks covered station listings/context actions, the station-context map with two Guadeloupe stations, wrapped note text, preview in the map, return to the selected station, and one D-pad edge to Back. Still pending: reliable guided-setup completion, exhaustive pin/rail/control boundaries and focus restoration, live click count, controlled-fixture and audible playback, and preview cancellation/no-click/competing-playback checks.**
- [x] **Step 5: If GUI access is unavailable**, record that explicitly and finish the automated/package checks without claiming end-to-end verification.
- [x] **Step 6: Update user documentation** with implemented routes, settings, local setup, acceptance results, and any remaining known limitations.
- [x] **Step 7: Commit** as `test: verify Kodi radio GUI package and flows`.

Task 7 automated/package/documentation work is complete; Step 4 is partially complete and remains unchecked until its outstanding live checks are done. See `docs/testing.md` for the dated matrix and `.superpowers/sdd/2026-10-23-kodi-radio-gui/task-7-report.md` for earlier evidence and limitations.

## Plan self-review

- Every approved first-slice design area maps to Tasks 1–7: safe local data, native skin listings, shared station actions, guided setup, shared map, preview behavior, settings, and Kodi acceptance.
- Tests are independent of implementation internals except for deliberate boundary adapters; the pure store, distance, projection and navigation logic stays testable outside Kodi.
- Playback and monetization responsibilities remain behind existing routes; the plan does not add unapproved ad serving behavior.
- Each task is bounded and has explicit files, interfaces, failing tests, verification, and a commit point. The Kodi-dependent acceptance is isolated after package validation.
