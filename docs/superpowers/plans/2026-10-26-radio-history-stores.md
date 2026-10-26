# Radio Search and Listening History Stores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the ten most recent station-name searches and bounded station listening counts in the add-on profile, following the approved Donor Radio spec.

**Architecture:** Reuse the existing versioned, private, atomic profile-store pattern in `radio_ui.py`. Record a search when a nonempty first-page search is submitted and record a station play at the existing successful play-resolution point. Do not add navigation views in this slice; the approved skin-first root listing has no Recent or History row.

**Tech Stack:** Python 3, Kodi add-on runtime, pytest, JSON profile files.

**Spec:** `/Users/hermes/.codex/attachments/de472828-01d6-4bb1-915d-aee4ae7427ee/donor-radio-spec.md` (Local state; Search; Playback and stream health).

## Global Constraints

- All four profile stores use the add-on's profile directory rather than `settings.xml`.
- Every store carries a `schema_version` integer from the first release.
- Store station UUIDs, never stream URLs.
- `recent_searches.json` contains query string, scope, timestamp; cap at 10, most recent first.
- `history.json` contains station UUID, play count, last played.
- Playback goes through `setResolvedUrl` on the `?mode=play` route; one code path records the click report and history update.
- Never let a profile-write failure revoke already-started playback.

## Review Focus

- Invalid or duplicate profile entries must be rejected without rewriting the user's file.
- Search pagination must not make one submitted query appear repeatedly or move its timestamp forward.
- Empty/cancelled searches must not enter recent searches.
- Failed playback resolution and test fixtures must not increment listening history.
- History updates must remain serialized and private across concurrent Kodi add-on processes.

---

### Task 1: Persistent Recent Search and Listening History

**Files:**
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/radio_ui.py`
- Modify: `plugin.audio.kodi_donor_radio/resources/lib/kodi_runtime.py`
- Test: `tests/test_radio_ui.py`
- Test: `tests/test_kodi_runtime.py`
- Update: `docs/testing.md`

**Interfaces:**
- `RecentSearchStore(profile_path).record(query, scope, timestamp)` stores a validated query; `.list()` returns newest first, maximum 10.
- `HistoryStore(profile_path).record_play(station_uuid, played_at)` increments one station's count and last-played timestamp; `.list()` returns most recently played first.
- Each JSON file uses the existing `{"schema_version": 1, "items": [...]}` envelope and existing locked atomic writer.
- Recent search fields are exactly `query`, `scope`, `timestamp`; query length is at most 256 and nonblank after trimming. Preserve submitted text; deduplicate by trimmed, casefolded query plus scope, replacing the stored spelling and timestamp on repeat.
- Recent search scopes are `name`, `tag`, `country`, or `language`; the current UI records only `name`.
- History fields are exactly `station_uuid`, `play_count`, `last_played`; timestamps are nonempty ISO-8601 values with timezone. Sort by parsed UTC instant descending, then station UUID for ties.

- [x] **Step 1: Write failing store tests.** Cover round-trip/schema, recent-search limit/order/deduplication, history increment/order/cap, and malformed or oversized records.
- [x] **Step 2: Verify the tests fail for missing store APIs.** Run the focused tests and preserve the expected missing behavior as the RED evidence.
- [x] **Step 3: Implement the two data stores.** Keep the recent-search scope to `name` for the current keyboard search route; allow the spec's `name`, `tag`, `country`, and `language` values in the store model. Set a 500-entry history cap and evict the least-recently played item when exceeded.
- [x] **Step 4: Add runtime integration tests and record submitted searches / resolved non-fixture plays.** Search pagination must not re-record the query. Profile errors must not affect playback or click reporting.
- [x] **Step 5: Run the full pytest suite, package checks, and `git diff --check`.** Kodi live acceptance remains a separate open gate because current Kodi master startup blocks before add-on execution on this Mac.
- [x] **Step 6: Commit the reviewed task.**
