# Shared World Map Texture Design

## Intent

Give the shared station map a recognizable, geographically correct land backdrop while keeping Radio Browser discovery and map rendering usable offline. The basemap and station pins must use the same equirectangular bounds so the focused place or station remains aligned when the map zooms.

## Goals

- Render a local PNG texture from bundled Natural Earth 1:110m land polygons for the current `MapView` bounds.
- Keep the texture and existing city pins on the same projection and panel dimensions.
- Use only Python standard-library modules at add-on runtime; do not request map tiles or other network resources.
- Keep the skin-mode map shared with the existing directory flow and preserve the D-pad station rail.
- Keep source data, output dimensions, and generated profile files bounded.

## Non-goals

- Implement the deferred themed interface or political labels/boundaries.
- Claim Kodi on-screen visual acceptance until the add-on can be launched and the map inspected in Kodi.
- Introduce a third-party GIS or imaging runtime dependency.
- Expand the data to higher-resolution country or street maps.

## Approaches

1. **Static world PNG.** Small and simple, but does not follow the region bounds used by pins when the user opens a saved place or station.
2. **Local vector data with a pure-Python PNG renderer.** Uses the same `MapView` bounds, works offline, and adds a small amount of bounded CPU work when opening the map. Chosen because it preserves geographic alignment without adding a runtime dependency or hosted tile service.
3. **Remote map tiles.** Could provide richer detail, but introduces a network dependency, external service/privacy considerations, and degraded offline behavior.

## Data and licensing

Use the Natural Earth 1:110m physical land polygon archive. The archive's dataset-specific `ne_110m_land.VERSION.txt` says 4.1.0; the download page currently labels the same Land link 4.0.0. Record the bundled archive's embedded 4.1.0 value and its SHA-256 in the source note. Natural Earth describes the data as land polygons including major islands and states that its map data is public domain for commercial and noncommercial use. The derived coordinate file will retain a source note and the optional “Made with Natural Earth” credit.

- Data page: https://www.naturalearthdata.com/downloads/110m-physical-vectors/
- Terms: https://www.naturalearthdata.com/about/terms-of-use/

## Rendering behavior

The map renderer receives a validated `MapView`, the bundled land polygons, and bounded output dimensions. It clips polygon rings to the view's longitude/latitude rectangle, including antimeridian-crossing views, projects them with the same equirectangular bounds as pins, fills a fixed ocean/land palette, and writes a PNG using the standard library. The Kodi map image control loads that local PNG. If the data or rendering is unavailable, the existing map surface and station controls remain usable and no network fallback is attempted.

## Acceptance

- Pure tests prove projection alignment, clipping, holes, antimeridian behavior, bounded malformed-data rejection, and PNG dimensions/pixels.
- Kodi adapter tests prove that each view installs a generated texture for the same bounds and that render failure retains map navigation.
- Package validation confirms the vector data is included and no external imaging dependency is added.
- Actual Kodi visual rendering remains pending live acceptance on the target Kodi master build.
