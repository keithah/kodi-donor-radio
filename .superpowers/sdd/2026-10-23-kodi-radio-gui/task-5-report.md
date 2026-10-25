# Task 5 report — pure radio map model

## Result

Implemented `radio_map.py` as Kodi-independent data and geometry helpers. The map view validates geographic and canvas bounds, projection rejects invalid/out-of-view coordinates and supports both full-world and antimeridian-crossing rectangles, and city clustering groups by normalized city/country plus rounded coordinates. Clusters retain stable IDs, sorted station UUIDs and counts, without carrying station URLs. Visible pins are filtered to the current view and capped at 60, with selected pin first and remaining pins ordered by distance to the view center, station count, and stable ID. Directional navigation uses the nearest projected candidate within a strict ±45-degree cone and never wraps.

Added focused coverage for world/regional/antimeridian projections, boundary validation, invalid coordinates, cluster normalization and duplicate handling, stable output, the 60-pin cap and priority order, directional selection, cone boundaries, ties, no-candidate behavior, and non-wrapping at world edges.

## Verification

- Initial test-first run failed during collection because `radio_map` did not exist.
- `python3 -m pytest tests/test_radio_map.py tests/test_radio_ui.py -q` — **49 passed**.
- `python3 -m compileall -q plugin.audio.kodi_donor_radio/resources/lib/radio_map.py tests/test_radio_map.py` — passed.
- `git diff --check` — passed.

## Scope

No Kodi runtime/window code, package configuration, ad behavior, network calls, or Task 4 code was changed. Kodi UI integration and live pin-control acceptance remain for Task 6/7.
