# ICY Ad Marker Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect explicit and likely ICY ad markers, let listeners teach exact marker meanings per station, and keep all ICY ad handling observation-only.

**Architecture:** A pure ICY marker tracker parses known provider formats and keeps at most 64 metadata observations in a five-minute monotonic window. It emits explicit, learned, inferred, candidate, and unresolved events without changing relayed audio. An authenticated loopback control API exposes the active candidate and records a station-scoped label in a bounded profile store; a Donor Radio home action opens the label dialog while playback continues.

**Tech Stack:** Python 3 stdlib, Kodi Python API, pytest, authenticated loopback HTTP control server, versioned JSON profile data.

**Spec:** `docs/superpowers/specs/2026-10-23-icy-ad-markers-design.md`

## Global Constraints

- Preserve existing ICY audio bytes exactly after metadata framing is removed.
- Preserve existing HLS cue parsing and HLS-only replacement eligibility.
- Never route ICY cues into the HLS composer, VAST success tracker, or monetization beacons.
- Retain no more than 64 metadata observations and no longer than 300 seconds.
- Unknown `-`, `#`, `*`, or suspicious titles are heuristic evidence only; an exact user label overrides the heuristic for that station.
- Recognize wrapped known marker commands and bounded, visibly delimited unknown marker tokens as candidates; bare punctuation can produce only bounded per-session inferred events, never a global direction, and marker words are not matched as arbitrary substrings.
- Do not add Kodi core or skin changes, external dependencies, or internet-origin ad requests.
- Keep control endpoints loopback-only and bearer-authenticated; reject origins and malformed or oversized inputs.

## Review Focus

- Ordinary one-word titles, artist/title strings, blank updates, and punctuation-only values can be confused; test exact classification and no fabricated end on expiration in Task 1.
- A stale or malformed marker must not end a newer break; test duplicate starts/ends and timeout in Task 1.
- Learned values may contain credentials, URLs, control characters, or oversized text; test redaction/rejection, per-station scope, and storage bounds in Task 3.
- Unauthenticated clients may try to read metadata or poison marker rules; test bearer auth, Origin rejection, body limits, and candidate equality in Task 3.
- Playback may stop or switch stations while the labeling dialog is open; test stale-session rejection and no label application to another station in Tasks 3–4.

---

### Task 1: ICY marker parser and five-minute inference tracker

**Files:**
- Create: `plugin.audio.kodi_donor_radio/resources/lib/icy_markers.py`
- Test: `tests/test_icy_markers.py`

**Interfaces:**
- Parser/tracker classifications use `MarkerSignal(kind, marker, format, confidence, duration_seconds)` where `kind` is `start`, `end`, or `candidate` and confidence is `explicit`, `learned`, `inferred`, or `candidate`. `expire()` emits a separate `Event(kind='break_unresolved')` with `details.reason='timeout'`; unresolved is not a `MarkerSignal` kind.
- Produces `IcyMarkerTracker(session_id, station_uuid, emit, learned_lookup)` with `observe(metadata, observed_at, monotonic_at)` and `latest_candidate()`. `learned_lookup(marker: str) -> Optional[str]` returns `start`, `end`, or `None` for that tracker’s station.
- `IcyMarkerTracker.expire(monotonic_at)` emits an unresolved event when an open inferred break exceeds 300 seconds, even if no later ICY metadata arrives.
- Keeps no more than 64 records; records older than 300 seconds are removed using monotonic time.

- [x] **Step 1: Write failing parser tests** for `AD_START`, bounded `AD_START_<seconds>`, `AD_STOP`/`AD_END`, valid TildeATS category 4 start/category 0 end and `##` end-break forms, malformed variants, and exact case/whitespace normalization.
- [x] **Step 2: Run `python3 -m pytest tests/test_icy_markers.py -q`** and verify the new parser tests fail because the module/API is absent.
- [x] **Step 3: Implement the parser** in `icy_markers.py`. Parse only exact command formats and valid terminated TildeATS. Accept `AD_START_<seconds>` values from 1 through 300; reject 0, malformed values, and values above 300 rather than clamping them. Tests cover 300 and 301 seconds.
- [x] **Step 4: Add failing state tests** for exact punctuation-only values and exact marker-vocabulary candidates (including `AD`, `AD BREAK`, `COMMERCIAL`, `SPOT`, and start/stop tokens), candidate-to-inferred-start, ordinary-track or reappearing marker inferred-end within 300 seconds, consecutive-duplicate suppression, unresolved expiry, exact learned override, and the 64-item bound. Do not classify broad substring matches such as a song title containing “ad”.
- [x] **Step 5: Implement `IcyMarkerTracker`** with a five-minute monotonic window. Only exact punctuation-only values and exact marker-vocabulary tokens are candidates; generic short titles are not. A likely marker starts an inferred break; an ordinary track-like title or the same marker reappearing after intervening metadata ends it only inside the window. These are session-local inferences; they never assign a persistent direction to bare punctuation. Consecutive duplicate titles do not act as a second marker. `expire(monotonic_at)` emits unresolved once and never a synthetic end.
- [x] **Step 6: Run `python3 -m pytest tests/test_icy_markers.py -q`** and verify all parser/tracker cases pass.
- [x] **Step 7: Commit** as `feat: classify ICY ad marker metadata`.

### Task 2: Wire marker observations into active playback sessions

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/sessions.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/relay.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/models.py`
- Test: `tests/test_relay.py`, `tests/test_icy.py`

**Interfaces:**
- `PlaybackSession.observe_icy_metadata(metadata, observed_at, monotonic_at)` emits the existing `metadata_observed` event and passes each complete title update to its `IcyMarkerTracker`.
- `PlaybackSession.latest_icy_candidate()` returns only the current session's latest bounded candidate, or `None`.
- `PlaybackSession.expire_icy_markers(monotonic_at)` checks timeout state; `SessionManager.expire()` calls it on its active session on every service loop tick.

- [x] **Step 1: Write failing relay integration tests** asserting existing metadata observations are retained, explicit/inferred break events are emitted once, candidate values are available for labeling, and normal track changes leave audio bytes unchanged.
- [x] **Step 2: Run the targeted relay/ICY tests** and verify they fail on missing session integration.
- [x] **Step 3: Wire `IcyDecoder` callbacks** through `PlaybackSession.observe_icy_metadata`; keep metadata removal and audio writes unchanged.
- [x] **Step 4: Extend the event detail allowlist** only for bounded marker format/confidence/duration/reason fields; keep station/session identity and existing redaction.
- [x] **Step 5: Run `python3 -m pytest tests/test_relay.py tests/test_icy.py -q`** and verify all cases pass.
- [x] **Step 6: Commit** as `feat: emit ICY break observations from sessions`.

### Task 3: Persist learned marker rules and expose authenticated control

**Files:**
- Create: `plugin.audio.kodi_donor_radio/resources/lib/icy_marker_store.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/sessions.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/relay.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/control.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/icy_markers.py`
- Test: `tests/test_icy_marker_store.py`, `tests/test_icy_markers.py`, `tests/test_relay.py`, `tests/test_kodi_runtime.py`

**Interfaces:**
- `IcyMarkerStore(profile_path).lookup(station_uuid, marker)` returns `start`, `end`, or `None`; `.learn(station_uuid, marker, action)` atomically stores an exact mapping.
- `ControlClient.current_icy_candidate()` returns `{session_id, station_uuid, marker}` or `None`.
- `ControlClient.label_icy_marker(session_id, marker, action, remember)` applies an exact label to the current session only when that marker is still the active candidate; persistence is optional.
- `SessionManager` owns the shared store and supplies exact lookups to new/active trackers.
- `IcyMarkerTracker.label(marker, action, observed_at, monotonic_at)` stores a session-only exact override and emits `icy_marker_labeled`; it updates break state without duplicating an unchanged transition. Persistent labels also write the station store.

- [x] **Step 1: Write failing store tests** for versioned JSON, atomic replacement, exact station/value matching, bounds (256 stations × 8 markers; marker <=256 UTF-8 bytes), and rejecting control characters, URLs, credential-like values, and invalid actions.
- [x] **Step 2: Run `python3 -m pytest tests/test_icy_marker_store.py -q`** and verify tests fail because the store does not exist.
- [x] **Step 3: Implement `IcyMarkerStore`** using the existing private profile directory and atomic temp-file/replace pattern.
- [x] **Step 4: Add authenticated control routes** for reading the active candidate and labeling its exact marker. A successful label updates the current session even when `remember` is false; when true it also persists the station rule. Require bearer authorization, reject Origin, cap JSON bodies at 4 KiB, and reject stale session/candidate values.
- [x] **Step 5: Add matching `ControlClient` methods** and instantiate the store from the add-on profile in `run_service()`.
- [x] **Step 6: Run store, relay, and runtime tests** and verify authorization, session ownership, learning, and next-metadata override.
- [x] **Step 7: Commit** as `feat: persist station-specific ICY marker labels`.

### Task 4: Add listener labeling action

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_kodi_runtime.py`

**Interfaces:**
- Add `action=label_icy_marker` to the plugin router.
- Add an always-present home-directory row; opening it shows the latest current-session candidate and a Start / End / Cancel choice.
- On Start/End, offer Remember for this station / This listening session only / Cancel; dismissal and Cancel apply nothing. Report stale/missing candidate without writing a rule.

- [x] **Step 1: Write failing UI routing tests** for active candidate display, start/end label submission, remember choice, stale candidate rejection, and no-op when there is no current candidate.
- [x] **Step 2: Run the targeted runtime tests** and verify they fail because the route/action is absent.
- [x] **Step 3: Implement the home row and dialog** using Kodi's existing plugin list/dialog patterns; do not modify the stock player OSD or skins.
- [x] **Step 4: Add localized labels/help** for the action and choices.
- [x] **Step 5: Run `python3 -m pytest tests/test_kodi_runtime.py -q`** and verify all UI-facing tests pass.
- [x] **Step 6: Commit** as `feat: let listeners label ICY ad markers`.

### Task 5: Broaden bounded marker-looking value recognition

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/icy_markers.py`
- Test: `tests/test_icy_markers.py`

- [x] **Step 1: Add failing tests** for wrapped explicit commands such as `#AD_START#`, bounded unknown tokens visibly framed by marker punctuation, bare `-`/`#`/`*` remaining direction-neutral candidates until a sequence-based session inference or manual label, and ordinary artist/title metadata that contains marker words not being classified as a marker. TildeATS support is restricted to categories `4` (ad break) and `0` (content); category `11` and others are unsupported.
- [x] **Step 2: Implement exact wrapper normalization and bounded marker-like detection** using token boundaries and a strict value length; preserve the original bounded metadata as evidence. Unknown values remain candidates/inferred only, never explicit cues or replacement triggers.
- [x] **Step 3: Run `python3 -m pytest tests/test_icy_markers.py -q`** and verify the new positive/negative cases pass.
- [x] **Step 4: Commit** as `feat: recognize wrapped ICY marker values`.

### Task 6: Fixture coverage, documentation, and package verification

**Files:**
- Modify: `tools/fixture_server.py`
- Modify: `tests/test_fixtures.py`, `tests/test_ad_smoke.py`, and relevant fixture tests
- Modify: `docs/testing.md`, `README.md`, `docs/monetization-roadmap.md`

- [x] **Step 1: Add controlled ICY fixture cases** for explicit start/end, TildeATS category 4/category 0 and `##`, unknown `-`/`#`/`*`, inferred return-to-track and paired-marker ends, and unresolved five-minute break.
- [x] **Step 2: Run the focused marker/relay/runtime suite** and verify one event per transition and no ICY replacement/VAST beacon.
- [x] **Step 3: Run `python3 -m pytest -q`**, `python3 -m compileall -q plugin.audio.kodi_donor_radio tests`, `git diff --check`, and the repository package-check command; record exact results.
- [x] **Step 4: Update docs** with supported explicit conventions, inference rules, the five-minute/64-entry bounds, listener learning action, and ICY replacement limitation.
- [ ] **Step 5: Verify Kodi UI manually when the Mac is available**; this remains a pending acceptance item. If it is locked/unavailable, leave the plan incomplete and do not claim end-to-end validation.
- [x] **Step 6: Commit** controlled fixtures and documentation, preserving separately requested anchor/spec work.
