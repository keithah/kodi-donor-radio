# Shared World Map Texture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render an offline, geographically aligned map texture behind the shared station pins and rail.

**Architecture:** Bundle a compact Natural Earth land-polygon coordinate file and use a pure-Python renderer to create an equirectangular PNG for the active `MapView`. The Kodi map window loads that PNG from its profile directory behind the current D-pad pin and station controls.

**Tech Stack:** Python standard library, Kodi `xbmcgui.ControlImage`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-world-map-texture-design.md` and `docs/donor-radio-spec.md` (Map window and basemap/projection sections).

## Global Constraints

- Render the map with the spec's equirectangular projection and active geographic view bounds.
- The base world texture is 1680×840; the visible map viewport remains aspect-matched at 1030×515.
- `radio_map.py` projection and map rendering have no Kodi imports.
- Bundle source polygons locally; no map tile or data network request at runtime.
- Add no third-party imaging or GIS runtime dependency.
- Bound coordinate input to 2 MiB and 100,000 points; bound each generated PNG to 2 MiB.
- Preserve the existing city-pin pool, station rail, context actions, and D-pad behavior.
- Any generated profile images use bounded, atomic writes; image failures leave local map navigation usable.

## Review Focus

- Antimeridian-crossing windows must render land on both sides of the seam without filling across the whole map.
- Polygon holes and separate islands must not invert the land/ocean fill.
- Malformed or oversized bundled data must fail closed before unbounded work or allocation.
- Generated PNG coordinates must align with the existing pin projection for the same bounds.
- A failed map render must preserve station navigation and avoid an external network fallback.

---

### Task 1: Bundled Land Geometry and Pure PNG Renderer

**Files:**
- Create: `plugin.audio.kodi_donor_radio/resources/map/natural_earth_110m_land.json`
- Create: `plugin.audio.kodi_donor_radio/resources/map/SOURCES.md`
- Create: `tools/convert_natural_earth_land.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_map.py`
- Test: `tests/test_radio_map_render.py`
- Test: `tests/test_natural_earth_converter.py`

**Interfaces:**
- `load_land_polygons(path) -> tuple`: validates the versioned bundled coordinate file and returns polygon rings.
- `render_land_png(polygons, view, width=1680, height=840) -> bytes`: returns a complete bounded RGBA PNG for the supplied `MapView`.
- Source file schema: `{"schema_version": 1, "source": "Natural Earth 1:110m land v4.1.0", "polygons": [...]}`; each polygon contains one or more closed rings of `[longitude, latitude]` pairs. The archive's embedded version is authoritative if the web download page differs. Reject coordinate data over 2 MiB or 100,000 total points.

- [x] **Step 1: Write failing renderer/data tests.** Cover equirectangular pixel alignment, polygon clipping, holes, antimeridian views, source schema/coordinate limits, PNG dimensions, and converter output.
- [x] **Step 2: Verify RED.** Run the focused tests and confirm expected missing API/asset failures.
- [x] **Step 3: Add the converter and bundled Natural Earth coordinates.** The converter reads a local official ZIP path; it must not make network requests and must retain only polygon coordinates plus schema/source metadata.
- [x] **Step 4: Implement bounded standard-library loading, clipping, raster filling, and PNG encoding in `radio_map.py`.** Keep the module Kodi-free and use the same `MapView` bounds as `project()`.
- [x] **Step 5: Verify GREEN.** Run focused renderer/converter tests and document the exact local-data source and generation command.
- [x] **Step 6: Commit the independently testable renderer task.**

### Task 2: Kodi Map Image Integration

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_radio_map.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `tools/check_package.py`
- Test: `tests/test_kodi_radio_map.py`
- Test: `tests/test_kodi_runtime.py`
- Update: `README.md`
- Update: `docs/testing.md`

**Interfaces:**
- The Kodi map window receives the add-on profile path and bundled geometry path.
- Image control id `19` displays a PNG rendered from the exact `MapView` passed to the window; it is placed behind the graticule and pin controls.
- Display viewport is `1030×515` at `(75, 145)`, matching the base texture's 2:1 aspect and all pin coordinates.

- [x] **Step 1: Write failing integration tests.** Assert the window requests and installs a view-specific local image, pin bounds stay aligned, and renderer failure leaves the station rail usable.
- [x] **Step 2: Verify RED.** Run the focused Kodi map tests and confirm missing control/data wiring fails.
- [x] **Step 3: Add the image control and render/load integration.** Keep each PNG at or below 2 MiB and atomically written; use Kodi's `ControlImage.setImage` with cache bypass for generated view files; cap retained generated textures at 20 matching files.
- [x] **Step 4: Align the map viewport and graticule to 1030×515.** Keep all focusable controls and station rail geometry unchanged.
- [x] **Step 5: Add the local map data/source files to the package allowlist. Run map/runtime tests, the full suite, package checks, official Kodi add-on checker, and `git diff --check`.** Report Kodi visual inspection as pending if the documented USB startup block persists.
- [x] **Step 6: Commit the integration after task review.**
