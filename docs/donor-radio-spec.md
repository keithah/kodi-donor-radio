# Donor Radio — Kodi add-on implementation spec

Oct 22, 2026 · @Keith

## Status and implementation baseline

This describes the full product target. The 2026-10-23 first slice implements
skin-native root/nested listings, safe place and favorite stores (including the
favorite's last deliberate-play timestamp), sequential native setup dialogs,
station/place actions, and one shared labeled schematic map with a coordinate grid and debounced preview.
The existing Radio Browser discovery, local relay, ICY/HLS observation, and
controlled loopback-only VAST/HLS behavior remain in place. A separate history
view/store and themed Home, Now playing, and other themed windows are deferred.

The implemented settings are `guided_setup=false`, `offer_setup_row=true`,
`preview_on_browse=true`, and the `run_setup` action. The target `interface` and
`theme_open_on` selectors are deferred until their screens exist. Setup uses a
country-scoped bounded station-derived place scan and 10/25/60/100/250-mile radius choices,
converted to kilometers in the store. Place results use bounded country pages
and local radius filtering; the map uses at most 200 seed rows, 60 city pins,
and 50 rail stations. It does not yet implement a complete geocoding catalog,
free map panning/zoom, or all target artwork.

Automated and package verification is recorded in [testing](testing.md#gui-package-verification--initial-task-7-run-2026-10-23).
Live Kodi 21.3 acceptance is partial (see the dated GUI acceptance record in
`testing.md`). Estuary and Confluence directory/context behavior, root-map entry,
and a Confluence station-context map were observed; the map's explanatory text
wraps, and moving focus into its station rail leaves the map visible during a preview. Returning
from that map restored the originating selected station. Guided setup selected
a Toronto location and radius and displayed the saved place in the root listing,
but a later country-picker run returned an empty result and exited to an empty
root; the complete setup-to-map flow remains unverified. Complete
focus-boundary coverage, live click counts, and
audible/controlled-fixture playback remain unverified. External Kodi UI/other-add-on/native
`setResolvedUrl` starts do not participate in the add-on's playback arbiter;
no atomic Kodi-wide playback guarantee is claimed. The preview handoff is bounded
to five seconds. Requirements below that exceed this slice remain product targets.

The concrete timing constraint for ad reporting is narrower: Kodi's Python
`Player.getTime()` exposes current-media time, not a decoded sample PTS or the current
HLS segment identity. A controlled run showed a live-playback stall followed by a
large clock jump across an ad boundary. The product goal is to correlate Kodi's
position with the immutable composed session timeline, retain that correlation across
playlist-window reloads and ad discontinuities, and suppress success events for any
placement whose playback interval was crossed ambiguously. The current fixture-only
adapter assumes `getTime()` equals seconds from sample zero; that identity assumption
has not been proven. HLS `EXT-X-PROGRAM-DATE-TIME` can describe segment-to-wall-clock
mapping, but does not reveal Kodi's presented segment to the add-on and cannot prove
the playback correlation by itself. Kodi core changes are out of scope. Until a
controlled Kodi run proves the add-on-only mapping, success beacons remain disabled
outside explicitly enabled loopback fixtures.

## Review corrections incorporated

- The repo-layout section below now describes the actual
  `plugin.audio.kodi_donor_radio/` package and distinguishes current modules from
  proposed UI modules.
- The root listing has seven always-present fixed entry types, an optional setup
  row when `offer_setup_row` is enabled, and one row per saved place. Test mode
  adds a controlled test streams row.
- Radio Browser's official client guidance requests mirror discovery, a descriptive
  User-Agent, and `/json/url/{stationuuid}` for each deliberate user click. A click
  request is directory accounting; it does not prove playback started or that audio
  was heard. Preview starts must not send a click. See the product's [API guidance](https://api.radio-browser.info/)
  and [API reference](https://docs.radio-browser.info/).
- Kodi supports `Window.addControl()`/`addControls()` as well as controls declared in
  XML. A fixed pin pool is a valid bounded design choice, but “controls cannot be
  created per result” is too absolute. Keep the pool unless a Kodi prototype shows
  dynamic controls are simpler and reliable for D-pad focus; either implementation
  must cap visible pins and provide deterministic focus navigation.
- The sample calls `getTime()` “current-media time”; it must not be described as an
  absolute wall clock or proof of audible output. Kodi documents it as fractional
  seconds of the current media ([Player API](https://xbmc.github.io/docs.kodi.tv/master/kodi-dev-kit/group__python___player.html)).

These corrections guide the product target; the implementation baseline above records the delivered slice.

## Scope and the two interface modes

Donor Radio is a Kodi music add-on that plays internet radio from the Radio Browser directory, organised around places the listener cares about rather than around genre alone.

The product target has **two interface modes**, chosen by a setting; this first slice implements skin mode and the shared map:

- **Skin mode** — the default. The add-on returns `plugin://` directory listings and the user's own skin renders them. The add-on controls item labels, `label2`, artwork and the folder structure. Nothing else.
- **Themed mode** — opt-in. Custom `WindowXML` windows with the world map, place rows and station artwork, in the add-on's own palette.

The map is the exception to that split. It is a custom window in both modes: in skin mode an **Open the map** row in the root listing launches it, then returns the user to the listing on back. That row is the seam between the two modes, and it is why skin mode is not a reduced version of the product.

Skin mode is the default for three reasons. It looks native in Estuary, Arctic Horizon or anything else the user runs. It needs no texture or font work to ship. And the root listing carries the whole information architecture in a small, data-dependent list, so the themed Home is an upgrade rather than a prerequisite.

This spec describes the target. The status section and repo layout below identify what exists in the current tree and what remains proposed.

## Settings

Interface and guided setup are **two independent switches**, not one list of combinations. An earlier draft had three named options; the phrase "with wizard" appearing twice was the tell that a second axis was hiding inside the first.

| Key | Type | Default | Label | Help string |
| --- | --- | --- | --- | --- |
| `interface` | enum | `skin` | Interface | Stations appear as ordinary Kodi lists, styled by whatever skin you run. Choose Built-in theme for the full-screen interface with the world map, your places and station artwork. |
| `guided_setup` | bool | `false` | Guided setup | Pick the cities you care about before you start. You can run this again any time from Settings. |
| `offer_setup_row` | bool | `true` | Offer setup at the top of the list | Shows a Set up Donor Radio row above your places. Turn off once you are set up. |
| `theme_open_on` | enum | `home` | Open on | Which screen the built-in theme opens on. Only applies in themed mode. |
| `preview_on_browse` | bool | `true` | Preview stations while browsing | Starts a station as you move onto it. Turn off on slow connections. |

Enum values: `interface` is `skin` or `theme`; `theme_open_on` is `home`, `map` or `favorites`.

`theme_open_on` applies only in themed mode. `preview_on_browse` applies to the Map window in both modes because the map is a custom window even when the surrounding listings use the active Kodi skin. Keep the theme-only setting visibly grouped or conditionally shown; do not hide the preview setting when `interface=skin`.

The mode choice cannot live inside the guided setup, since setup is downstream of it. Settings is the only honest home for it. `offer_setup_row` is the softer path: a dismissable row at the top of the listing rather than a forced wizard on first launch.

## Skin mode — the directory tree

The root listing is the whole information architecture. When enabled, the
optional setup row appears first, followed by one row per saved place and seven
always-present actions. Test mode adds a controlled test streams row at the end.

| Row | Route | Returns | Folder |
| --- | --- | --- | --- |
| Set up Donor Radio | `?mode=setup` | Runs the dialog sequence, refreshes the container; shown only when `offer_setup_row` is enabled | No |
| *\<each saved place>* | `?mode=place&id=<place_id>` | Stations within that place's radius, sorted by `clickcount` | Yes |
| Favorites | `?mode=favorites` | Saved stations in user order | Yes |
| Browse by genre | `?mode=tags` | Tag list, then `?mode=tag&tag=<name>` | Yes |
| Browse by country | `?mode=countries` | Country list, then `?mode=country&code=<iso>` | Yes |
| Search… | `?mode=search` | Opens `xbmc.Keyboard`, then results | Yes |
| Open the map | `?mode=map` | Launches the map `WindowXML`, returns no items | No |
| Settings | `?mode=settings` | `Addon.OpenSettings` | No |
| Label latest ICY marker | `?mode=label_icy_marker` | Opens marker/action and station-memory dialogs | No |
| Controlled test streams | `?mode=tests` | Lists local marker and ad fixtures; test mode only | Yes |

The setup row is hidden when `offer_setup_row` is false. Place rows carry their
station count in `label2`.

When `guided_setup` is enabled and there are no saved places, the automatic setup
dialog is offered once per Kodi session. Cancelling it returns to the root without
reopening the dialog on each navigation refresh; Settings and, when enabled,
the setup row remain available for another attempt. Guided setup does not add a
setup row when `offer_setup_row` is false.

### What each station ListItem carries

- `label` — station name. Keep the frequency if the name has one; do not synthesise it.
- `label2` — city, then codec and bitrate. `San Mateo · AAC 64`.
- `art` — use the bundled generic “DR” tile for this first GUI slice. Do not pass a directory-provided favicon URL to Kodi: its native artwork loader bypasses this add-on's destination and redirect validation. Remote favicons may be enabled after bounded validated fetching and local caching are implemented.
- `setInfo('music', ...)` — `title`, `genre` from tags, `comment` for the plot panel.
- `setProperty('IsPlayable', 'true')` and `setIsFolder(False)`, so the skin treats it as a track.
- Station URL: resolve through `?mode=play&uuid=<stationuuid>` and `setResolvedUrl`, never hand the skin the raw stream URL. The indirection is where the click report and the favourite's last-played timestamp happen.

### Context menu

Every station item gets `addContextMenuItems` with: Play, Add to / Remove from favorites, Show on map, Station info. The same four actions appear in the themed mode's own context dialog, so write them once and share them.

Place and folder rows get: Make default, Change radius, Remove.

## Themed mode — window inventory

Eight themed screens plus the item-options context dialog. The eight screens are `WindowXML` views or dialogs; the context dialog is a separate entry. Design canvas artboard names are given so the two can be matched up.

| Window | XML | Artboard | Python class |
| --- | --- | --- | --- |
| Home | `home.xml` | Main | `WindowXML` |
| Map | `map.xml` | Map, MapStation | `WindowXML` |
| Now playing | `player.xml` | NowPlaying | `WindowXML` |
| Favorites | `favorites.xml` | Favorites | `WindowXML` |
| Browse | `browse.xml` | Browse | `WindowXML` |
| Search | `search.xml` | Search | `WindowXML` |
| Places | `places.xml` | Locations | `WindowXML` |
| First run | `setup.xml` | FirstRun | `WindowXMLDialog` |
| Item options | `options.xml` | Favorites overlay | `WindowXMLDialog` |

All XML lives under `resources/skins/Default/1080i/`, coordinate space 1920×1080.

### Controls behind each region

- **Left nav rail** — one `grouplist`, `itemgap` 6, of `button` controls. Each carries its own focus texture. The current window's button uses a separate "selected" texture, which is state, not focus.
- **Home place chips** — horizontal `grouplist` of buttons, `itemgap` 16.
- **Home station rows** — horizontal `wraplist`, one per place, with `itemlayout` and `focusedlayout`.
- **Favorites and Browse grids** — `panel`, five columns. `panel` already wraps and handles D-pad traversal, so no custom logic.
- **Station lists (map rail, search results)** — `fixedlist` or `list` with `focusposition` set so the focused row stays put while content scrolls under it.
- **Wrapped paragraphs** — `textbox`. Single-line labels that may overflow get `<scroll>true</scroll>`.
- **Station metadata panel** — plain `label` pairs in a vertical `grouplist`, fed from window properties.
- **Toggles in setup** — `radiobutton`.

### Per-item state in lists

List item layouts are fixed. Anything that varies per row — the LIVE badge, the filled star, the Previewing state — is a control inside `itemlayout` with a `<visible>` condition on a `ListItem.Property`. Set those properties when you build the list; do not try to construct layouts per item.

## The map window

The governing idea: **the map is orientation, the list is selection.** A D-pad cannot hunt scattered pins, so the crosshair moves between *cities*, and the stations for the lit city land in a rail the user moves through with up and down. This is the one place the design deliberately diverges from Radio Garden, which depends on continuous mouse drag.

### Basemap and projection

One equirectangular world PNG as an `image` control, drawn at 1680×840. Equirectangular means the projection is two multiplications, with no library:

```latex
x = \left(\frac{\lambda + 180}{360}\right) W \qquad y = \left(\frac{90 - \varphi}{180}\right) H
```

For the zoomed region view, keep a view window `(λ₀, φ₀, λ₁, φ₁)` and scale into it the same way. The canvas uses `52,36 → 108,87` in map units for the North America view, which is an aspect-matched window rather than a free zoom level.

### The pin pool

Kodi also exposes `Window.addControl()` and `addControls()`, so a fixed pool is a design choice rather than an API requirement. Start with a **fixed pool of around 60** `button` controls in `map.xml`, hidden at load, with sequential IDs; it gives a hard resource bound and stable focus IDs. A prototype may replace it with dynamic controls if it demonstrates equally reliable focus and removal behavior.

1. Cluster stations to cities first. Pins are cities, not stations.
2. Take the N cities nearest the view centre that fall inside the view window.
3. For each, take a pool control, `setPosition` it from the projection, `setLabel`, `setVisible(True)`.
4. Hide the remainder of the pool.

Cap the pool rather than the data. If a region holds more cities than the pool, show the highest station counts and let the rest appear as the user pans.

### Crosshair navigation

`onleft` / `onright` need static control IDs, so they cannot express "nearest pin west". Intercept the direction actions in `onAction` instead:

1. Read the action — `ACTION_MOVE_LEFT`, `RIGHT`, `UP`, `DOWN`.
2. From the current city, find the nearest city whose bearing falls in a ±45° cone in that direction. Use squared distance on the projected `x, y`, not great-circle — the user is navigating what they see.
3. No candidate in the cone? Keep the crosshair where it is. Do not wrap, and do not fall back to the nearest city in any direction; both feel like the remote misfired.
4. `setFocusId` the pin control, move the crosshair, repopulate the station rail.

Debounce the rail repopulation. A user holding left will cross several cities, and a Radio Browser call per city is both slow and rude.

### Not attempted

A rotating 3D globe is not reachable from the skinning engine. Free pixel-level panning is possible but gives the D-pad nothing to aim at. Both were considered and dropped; the flat map with city-to-city hops is the honest version.

## Focus and navigation

Exactly one thing is focused at a time, and it is always obvious which. Focus is carried by three things together, never by colour alone: the `focusedlayout` texture, a `Focus` zoom animation, and a change of label colour.

```xml
<animation effect="zoom" start="100" end="106" center="auto" time="120">Focus</animation>
<animation effect="zoom" start="106" end="100" center="auto" time="120">UnFocus</animation>
```

The focused card growing past its neighbours is expected, not a layout bug — the Favorites artboard draws it that way on purpose.

**Selection is not focus.** The current window's rail button and the active filter chip use a separate texture and stay marked when focus moves elsewhere. Two different states, two different treatments.

### Remote actions

| Action | Behaviour |
| --- | --- |
| OK | Play, or open the folder |
| Long-press OK | `ACTION_CONTEXT_MENU` — the item options dialog |
| Back | Up one level. From Now playing, leaves playback running |
| Left / Right on the map | Nearest city west / east |
| Left / Right on Now playing | Previous / next favourite |
| Info | Station info |

Secondary actions belong in the context menu, not on screen. Kodi users already look there, and the shelf of buttons it replaces does not survive at 10 feet.

### Reachability

Every control must be reachable by D-pad, with no hover state anywhere and no action available only by pointer. Set explicit directional neighbors for standalone controls and transitions between regions. Within a `list`, `fixedlist`, `wraplist`, `panel`, or `grouplist`, use Kodi's container navigation and test its boundary exits; do not override internal D-pad behavior with blanket directional targets. Hidden or disabled controls must not receive focus, and returning from a child view should restore the previous selection when that item still exists.

Touch targets stay at or above 44px, and no text falls below 20px — both already hold across the artboards.

Build and verify against the minimum supported Kodi 21.3 API, not only current Kodi master documentation. The target version's [`Window.h`](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Window.h) and [`WindowXML.h`](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/WindowXML.h) define the window and focus boundary; Kodi's [skinning manual](https://kodi.wiki/view/Skinning_Manual) describes the list, panel, wraplist, and grouplist navigation contract.

### GUI acceptance

Every view must define loading, populated, empty, offline-with-cache, and unrecoverable-error states. A failed request must not look like a successful empty result. Network work must leave the Kodi UI responsive, and closing a view or cancelling setup must stop its pending UI operation without stopping unrelated playback.

The skin-mode and themed-mode listings must be built from the same station records and action handlers. The custom map must be reachable from both modes and must return to the previous listing on Back. Preview playback must be cancellable, must not send a Radio Browser click, and must stop when focus leaves a station after the debounce window. Repeated map movement must debounce or cancel stale station requests so a slow result cannot replace the currently focused city's rail.

Acceptance on the target Kodi version must cover root and nested navigation; D-pad reachability; list and panel entry/exit; map movement without wrapping when there is no candidate; focus restoration after Back; setup cancellation; opening Settings; playback continuing when leaving Now playing; and one Radio Browser click per deliberate station selection. Test plugin-directory mode with Estuary and at least one alternate skin, and test the built-in theme independently of the active Kodi skin. Record the Kodi version, display resolution, remote/input device, and focus defects. Projection, clustering, and nearest-direction behavior remain pure functions that can be tested without Kodi.

## Data — Radio Browser

The implemented client and request-parameter tests are in
`plugin.audio.kodi_donor_radio/resources/lib/radio_browser.py` and
`tests/test_radio_browser.py`. The calls below describe the current implementation
and distinguish it from product targets that still need backend support.

### Server discovery

Radio Browser is a volunteer mirror pool with no single stable host. Resolve a mirror at startup rather than hardcoding one, cache the choice for the session, and fail over to the next mirror on error. Send a descriptive `User-Agent` carrying the add-on name and version — the project asks for this, and it is how mirror operators tell traffic apart.

### Calls behind each view

| View | Call | Notes |
| --- | --- | --- |
| A place's stations | **Product target:** stations within a geographic radius | Current code fetches up to four bounded country pages with `has_geo_info=true` and filters them locally by the saved place radius; no Radio Browser radius query is made |
| Browse by genre | `GET /json/tags`, then `/json/stations/search` with exact tag and pagination parameters | Implemented and covered by request tests |
| Browse by country | `GET /json/countries`, then `/json/stations/search` with country code and pagination parameters | Implemented and covered by request tests |
| Search | `/json/stations/search` with name, country, tag, exact-filter, pagination, and `hidebroken` parameters as applicable | Language-scope search remains a product target |
| Map pins | Bounded `/json/stations/search` pages with `has_geo_info=true`; pins are clustered and filtered against the local map view | Client-side map view filtering; not a backend radius query |
| Favourites | None | Resolved from stored UUIDs |

Station refresh and deliberate-play resolution use `/json/stations/byuuid` with
a UUID list. After successful resolution for deliberate Play, `/json/url/{uuid}`
is called once as Radio Browser's click counter; its response is not used for
playback. Both calls are implemented in `RadioBrowser`.

Order results by click count descending for anything the user did not explicitly sort. Filter out entries with a broken flag set, and drop any station whose stream URL is empty.

### Caching

The add-on should be usable with the network briefly down, and it should not re-query on every container refresh.

- Tag and country lists: cache for a week.
- A place's station list: cache for a day, keyed on place id and radius.
- Search results: cache for the session only.
- Favourites: resolved from the local store first, refreshed in the background.

Map pin data deserves its own cache keyed on the view window, since panning will revisit the same regions constantly.

### Giving back

Two obligations, both cheap and both easy to skip:

- Send `/json/url/{stationuuid}` once for each deliberate Play selection, after the resolver has accepted the request; never send it while rendering a list or starting a preview. This is Radio Browser's user-click counter, not proof that Kodi began decoding or that a listener heard audio. Delivery is best effort and must not block or cancel playback.
- Offer **vote** as a context menu action on a station.

## Local state

Four things persist, all in the add-on's profile directory rather than in `settings.xml`. Settings are for preferences; these are user data, and they outgrow a settings file quickly.

| Store | Holds | Notes |
| --- | --- | --- |
| `places.json` | id, display name, lat, lon, `radius_km`, `is_default` | Radius is stored in kilometers; the UI accepts miles and converts with 1 mile = 1.609344 km. Order is the order they appear on Home |
| `favorites.json` | station UUID, name cached, added timestamp, user order | Name is cached so the list renders offline |
| `recent_searches.json` | query string, scope, timestamp | Capped at 10, most recent first |
| `history.json` | station UUID, play count, last played | Drives Most played and Recently played |

Every one carries a `schema_version` integer from the first release. Migration is a cheap thing to add on day one and an expensive one to retrofit once people have data.

Store station **UUIDs**, never stream URLs. Stream URLs change; the UUID is the stable handle, and resolving through it is what lets a station that moved host keep working.

Cache the display name alongside the UUID anyway. A favourites list that renders instantly from disk and then quietly refreshes beats one that shows an empty screen while the network wakes up.

### Places

A place is a point and a radius, not a city record. The radius is per-place and user-adjustable, because the right value differs between a dense city and a rural one — the canvas shows 60 miles for San Francisco and 25 for Lisbon.

Exactly one place carries `is_default`. It decides which place the themed Home leads with and where the map opens.

## Playback and stream health

Playback goes through `setResolvedUrl` on the `?mode=play` route in both modes. One code path, one place the click report and the history update happen.

### Now-playing text

Track titles come from ICY metadata on the stream, read via the player's current-title info. It is unreliable by nature: some stations send nothing, some send the station slogan, some send an ad. Design for absence — when there is no title, the Now playing screen shows the station and its tagline, not an empty row.

There is no track history from ICY. If the Recently played idea comes back, the add-on has to log title changes itself while playing.

### Preview while browsing

With `preview_on_browse` on, moving onto a station in the map rail starts it. Two rules keep this from being miserable:

- Debounce by roughly 400ms, so passing through a row does not start it.
- Never report a click for a preview. Only a deliberate OK counts.

### Stream health

The Now playing screen replaced a spectrum analyser with a connection strip, because an add-on window cannot render an audio-reactive visualiser — `visualisation` is bound to Kodi's own fullscreen music view, not to a `WindowXML`. The strip shows connected state and buffer state when Kodi exposes them. A dropout count is derived from observed playback interruptions; it is not a standard Kodi player property. These indicators matter more on a live stream than eye candy.

Small looping level meters elsewhere are fine: a `multiimage` cycling a handful of frames. The distinction is that those are decorative, and nothing claims they track the audio.

### Ad playback clock and success reporting

VAST composition uses one append-only 48 kHz sample timeline per relay session. The
first output sample is sample zero; preroll and each replacement have immutable
sample intervals on that timeline. The experimental fixture-only adapter maps
Kodi's media-relative `Player.getTime()` directly to seconds from sample zero.
This is an identity assumption, not a demonstrated segment-to-sample correlation;
`getTime()` reports media position, not wall-clock time or audible output. The
adapter never resets its origin when HLS playlist windows slide or the media
playlist carries a discontinuity. Kodi core changes are out of scope.

`EXT-X-PROGRAM-DATE-TIME` associates a segment's first sample with a wall-clock
time. It does not tell the add-on which segment Kodi has presented, so it cannot be
used by itself to fire success beacons. `EXT-X-START` requests a preferred starting
point; it does not prove Kodi honored it. The mapping remains unproven until a
controlled Kodi run independently verifies startup at the expected origin,
playlist-window advancement, preroll and midroll boundaries, and continuity across
HLS discontinuities. A backward/reset clock greater than 50 ms, an explicit seek,
or an unresolvable session origin invalidates the mapping; backward jitter up to
50 ms is clamped and must not be mistaken for forward progress. Do not guess or
silently re-anchor to a later playlist window.

At 250 ms sampling, a transition is considered ambiguous if media advances across
an interval longer than 750 ms, Kodi's media position moves backwards, it advances
by more than elapsed monotonic time plus 250 ms, or it remains stationary for at
least 750 ms. The add-on cannot reliably distinguish a real buffer from a stale
player clock using position samples alone. Mark every ad placement intersecting an
ambiguous position interval as untrackable for the rest of the placement. Earlier
confirmed milestones remain valid; later quartiles/completion for that placement
are suppressed.
The adapter invalidates its mapping for the remainder of the session after an
explicit seek. The tracker may continue with a later placement only after it observes
that placement start and its own playback remains continuous. Seek, stop, station switch, playback
error, and unknown mapping never imply completion.

Until the add-on-only anchor passes those Kodi checks, successful VAST beacons remain
limited to the explicit loopback fixtures. Directory stations remain outside ad
insertion and ad success reporting. Player-clock evidence is not proof of rendered or
heard sound; record audible-output verification separately.

### Reconnect and sleep

Streams drop. Reconnect automatically a few times with a backoff before surfacing an error, and keep the Now playing screen up while retrying rather than dumping the user back to a list.

The sleep timer stops playback and closes the player window. It should survive the window closing, which means it lives in the service, not in the window class.

## Assets

Themed mode needs shipped assets. Skin mode needs almost none, which is part of why it is the default.

### Textures

Kodi has no border radius and no box shadow. Every rounded panel in the designs is a nine-slice PNG, drawn with `<texture border="...">`. The designs were kept deliberately repetitive so the inventory stays small:

| Texture | Use | Radius |
| --- | --- | --- |
| `card.png` | Resting card and list row | 14px |
| `card-focus.png` | Focused card, amber 4px border | 14px |
| `chip.png` | Resting chip, 1px outline | full |
| `chip-selected.png` | Selected chip, amber fill | full |
| `panel.png` | Overlay and dialog panels | 16px |
| `tile.png` | Monogram artwork tile | 8px |
| `rail-selected.png` | Nav rail selected state, amber left bar | 10px |

Seven textures cover every screen. Add an eighth only when a design genuinely needs it, not per screen.

### Fonts

Two families, both shipped with the add-on rather than borrowed from the skin — a skin's font set cannot be relied on. Inter for UI, IBM Plex Mono for frequencies, coordinates, bitrates and key caps.

Define every size used in `Font.xml`. The designs use roughly: 80, 42, 40, 32, 28, 25, 23, 20, 17. Name them by role rather than by number, so changing the scale later does not mean a find-and-replace across nine XML files.

### Icons

All icons are flat single-colour PNGs so they can be recoloured with `colordiffuse` for resting, focused and disabled states, rather than shipped three times. The set: home, globe, star outline, star filled, grid, search, gear, pin, plus, chevron right, clock, info, play.

### Station artwork

Radio Browser's `favicon` field is often missing, often tiny, and sometimes a dead link. Do not stretch it. In the current Kodi GUI slice, remote favicon loading is disabled because Kodi fetches artwork outside the add-on's URL policy; the bundled generic “DR” tile is used until safe validated fetching and local caching exist. A station-specific call-letter monogram remains a future design target; this slice does not generate per-station artwork.

The current slice uses the bundled generic “DR” tile. A future station-specific
call-letter monogram or directory favicon requires bounded validated fetching and
local caching first; Kodi's native artwork loader must not receive an unvalidated
directory URL.

## Repo layout

The repository root contains the add-on package and its development-only
tests, tools, and documentation. This inventory describes the observation/VAST
POC and the implemented first GUI slice; themed windows beyond the shared map
remain proposed.

```
<repository root>/
  plugin.audio.kodi_donor_radio/   distributable add-on root
    addon.xml                         music plugin + service declarations
    default.py                        plugin:// entry point
    service.py                        Kodi service entry point
    resources/
      settings.xml                    setup, map preview, diagnostics, fixture-only ads
      lib/
        kodi_runtime.py               Kodi routes, service loop, player callbacks
        radio_browser.py              mirror discovery, directory calls, click counter
        radio_ui.py                   safe place/favorite stores, display/radius helpers
        radio_map.py                  pure projection, clustering, directional selection
        kodi_radio_map.py             shared map window and preview lifecycle
        playback_arbiter.py            profile lock and add-on direct-start intent tokens
        station_resolver.py           bounded M3U/PLS/HTTP resolution
        sessions.py                   session and opaque resource ownership
        relay.py                      local HTTP relay and HLS route rewriting
        hls.py                         HLS parsing and cue observation
        cues.py                        normalized cue lifecycle
        ad_session.py                 controlled VAST preparation and HLS composition
        ad_timeline.py                immutable 48 kHz output sample timeline
        ad_media.py                    AAC/TS validation and creative cache
        ad_tracking.py                 playback evidence and beacon worker
        kodi_ad_player.py              fixture-only Kodi clock adapter
        events.py, control.py, ...     bounded diagnostics and local service control
      skins/Default/1080i/map.xml      shared map, 60 pin controls and station rail
      station-monogram.png            bundled station fallback
      language/resource.language.en_gb/
  tests/                             repository root; not packaged
  tools/                             repository root; not packaged
  docs/                              repository root; not packaged
```

Projection/clustering and profile stores now remain independent of Kodi. Future
themed windows should reuse these station, favorite, and action boundaries under
the existing add-on package and localization directory.

## Decisions and proposed implementation order

The target remains two modes. The approved first slice delivers skin listings, sequential native setup, and the shared map; themed screens follow after Kodi GUI acceptance.

| Decision | Current spec call | Implementation note |
| --- | --- | --- |
| Map placement | Keep **Open the map** at the root | The location-first path is a primary entry point, and the same map is available from station context actions. |
| Pin controls | Start with a fixed pool of about 60 | Stable IDs make focus behavior easy to test and the cap is explicit. Prototype dynamic controls only if focus and removal are equally reliable. |
| Station artwork | Current slice uses a bundled generic DR tile | Remote artwork and station-specific monograms wait for safe validated fetching and local caching; do not stretch low-resolution favicons. |
| Preview default | On, as listed in Settings | The setting applies to the map in both modes and can be disabled for slow connections. Preview never counts as a deliberate click. |
| Guided setup in skin mode | Implemented: sequential native dialogs | This avoids loading theme assets for the default mode. Revisit if testing shows that dialogs cannot explain location selection clearly. |
| Delivery order | Approved and implemented: skin listings plus shared map first; themed windows after Kodi acceptance | This validates the information architecture before the larger custom-theme investment. Both modes remain in the product target; the first slice follows the approved delivery order. |

Guided-setup presentation and delivery order were approved for this slice. Live Kodi acceptance is partial as recorded above; implementation does not establish unobserved target visual/focus behavior.

### Source for the designs

Every screen referenced here lives on the Donor Radio design canvas, by the artboard names given in the window inventory. Station names, frequencies and cities in the mockups are real; bitrates, vote counts and genre tallies are illustrative placeholders showing where those fields land.
