# Kodi Donor Radio GUI — implementation design

## Status

Approved target design for the GUI in `docs/donor-radio-spec.md`. The first slice
(skin-native listings, sequential Kodi setup dialogs, and shared map) is implemented.
Basic live acceptance on Kodi 21.3 has covered listings, station context actions,
the station-context map, root-map launch, preview remaining in the map, and return
to the selected station. Setup completion, all focus boundaries, playback/click
accounting, controlled-fixture audio, and themed windows remain pending. See the
dated matrix in `docs/testing.md`.

## Goal

Make Radio Browser discovery feel like a radio app organized around saved places,
favorites, and a map while keeping the default listings native to the user's Kodi
skin. Add a shared station/action layer so a later built-in theme does not duplicate
directory, playback, or favorite behavior.

## Approaches considered

1. **Skin listings and shared map first (recommended).** Add versioned place and
   favorite stores, build the root and nested `plugin://` listings from shared
   station records and actions, then add the custom map window used in both modes.
   Implement guided setup with sequential Kodi dialogs in skin mode. This matches
   the current plugin entry point and tests the location-first flow before themed
   windows expand the UI surface.
2. **Theme-first.** Implement all custom windows and the map first, then adapt the
   plugin listings. This establishes the visual identity sooner, but delays
   validation of the skin mode that the spec makes the default and requires the
   largest initial focus/layout effort.
3. **Listings and stores first, map later.** Ship the skin-mode information
   architecture, then add the shared map in a later slice. This is the smallest
   first delivery, but leaves the central location-based interaction unvalidated.

Working recommendation: approach 1, with approach 2 deferred until the listings and
map pass Kodi acceptance. This preserves both interface modes in the product target.

## Scope of the first GUI slice

- Add versioned, profile-local stores for places and favorites. Store station UUIDs
  as identity; cache display fields so saved rows can render while offline. Place
  records include coordinates, radius, and one default-place flag. Favorite order is
  stable and user-controlled.
- Extend directory records with the metadata needed by the UI, while keeping
  playback URLs and click reporting behind the existing deliberate `play` route.
- Build skin-mode root, place, favorites, genre, country, and search listings.
  Add station context actions for play, favorite, map, and station information, and
  place actions for default, radius, and removal.
- Add the map as a Kodi `WindowXML` window reachable from the root and station
  context action. Use a bounded pool of map pins, deterministic D-pad focus and
  nearest-city movement without wrapping. Return to the prior directory and
  selection when closing it.
- Add guided setup and the optional setup row using sequential Kodi dialogs. Setup
  can be cancelled without stopping existing playback. When enabled, automatic
  setup is offered once per Kodi session; manual setup remains available from the
  setup row and Settings after cancellation. Settings independently control
  guided setup, setup-row visibility, and map preview. Interface-mode and
  themed-entry settings are deferred with the themed windows.
- Share station records and action handlers between plugin listings and custom
  windows. Do not introduce themed listing windows in this first slice.

## Architecture

- Keep persistence, place/radius filtering, station ordering, map projection, and
  map-neighbor selection in pure Python modules with no Kodi imports.
- Keep Kodi-specific directory rendering, context menus, dialogs, and `WindowXML`
  interaction in the plugin UI layer. Both listings and the map call the same
  action handlers.
- Reuse Radio Browser's UUID resolution for deliberate playback. Only the existing
  `play` action reports a directory click. Map previews use a separate preview path
  or explicit preview intent and must never call the click endpoint.
- Run network reads through the existing cancellable operation helper so the Kodi
  UI stays responsive. Closing a view or cancelling setup cancels that UI request
  without stopping unrelated playback.
- Keep map projection and pin layout independent of artwork. The map must remain
  usable with missing station favicons; use the spec's shared monogram fallback.
- Use the profile directory for persistent data and atomically replace versioned
  store files so interrupted writes cannot corrupt favorites or places.

## Non-goals for this slice

- Implement the optional themed home, genre, country, favorites, station, and
  now-playing windows.
- Change Kodi core or claim that Kodi media time proves audible output.
- Enable ad insertion or success reporting for ordinary Radio Browser stations.
- Add account sync, cloud storage, or a server-side user database.

## Acceptance

- Pure tests cover store schema validation/migration, UUID identity, cached display
  data, place radius filtering, ordering, projection bounds, pin limits, and
  deterministic non-wrapping neighbor selection.
- Kodi-facing tests cover root/list routes, folder versus playable items, station
  labels and artwork fallback, context actions, setup cancellation, settings,
  previous-selection restoration, preview cancellation, and zero click reports for
  previews.
- Package validation confirms the UI XML, localized strings, map assets, and
  settings are included, with no test fixtures or development tools in the ZIP.
- Kodi 21.3 acceptance covers Estuary and one alternate skin, both entry paths to
  the map, D-pad reachability and region exits, empty/offline/error/loading states,
  focus restoration, map movement without wrapping, setup cancellation, and
  deliberate-play click accounting.

## Risks and dependencies

- The current repository has no place/favorite stores or GUI assets. The attached
  spec names a design canvas, but no canvas export is in the repository. Before
  pixel-matching the map, confirm that its map/background art is available; absent
  that asset, use a functional, clearly labeled schematic map rather than inventing
  branded artwork.
- Radio Browser fields may omit coordinates, favicons, or descriptive metadata.
  The current slice uses the bundled generic DR tile; remote artwork waits for
  bounded validated fetching and local caching. Missing data must produce useful
  empty states and must not prevent playback.
- Custom `WindowXML` focus and return behavior must be checked on Kodi 21.3, not
  inferred only from current Kodi master.

## Approval outcome

The user approved skin listings and the shared map first, followed by Kodi-native
setup dialogs. The later themed-window work remains in the product target but is
deferred until live Kodi acceptance of this first slice.
