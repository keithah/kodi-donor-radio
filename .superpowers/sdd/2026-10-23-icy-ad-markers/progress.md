# SDD ledger — plan: docs/superpowers/plans/2026-10-23-icy-ad-markers.md

## Workspace and baseline

- Worktree: `/Users/hermes/.codex/worktrees/radio-poc/kodi-donor-radio`
- Branch: `codex/vast-controlled-ads`
- BASE before Task 1: `a947ae0527ca0c146a3ec48b58cb15532dad8bfb`
- Existing unrelated edits from the prior anchor/spec task are present and must not be accidentally staged. Preserve untracked `scratch/`.
- The active branch is a linked worktree, not main/master.

## Pre-flight conflict scan

| Tasks | Shared file/interface | Check and ruling |
|---|---|---|
| 1 → 2 | `IcyMarkerTracker` from `icy_markers.py` consumed by `sessions.py` | Plan interface names match. Keep parser pure and session event emission in Task 2. |
| 1 ↔ 3 | `learned_lookup(marker)` consumed by `IcyMarkerTracker`, supplied by `IcyMarkerStore` in `SessionManager` | Lookup has a station-bound tracker closure; Task 3 returns only `start`, `end`, or `None` and does not alter parser ownership. |
| 2 ↔ 3 | `sessions.py`, `relay.py` | Task 2 establishes session observation; Task 3 adds store/control to that path. Keep Task 2 integration backward-compatible and inject store through `SessionManager`. |
| 3 ↔ 4 | `kodi_runtime.py`, `ControlClient` | Task 3 provides authenticated candidate/label methods; Task 4 only invokes those methods from plugin routing and dialog. |
| 2 ↔ 5 | Existing ICY fixture behavior and `tests/test_icy.py` | Task 2 proves framing/audio invariance; Task 5 adds controlled end-to-end marker fixtures without changing decoder semantics. |
| 4 ↔ 5 | Kodi route and UI fixture coverage | Task 4 unit-tests routing/dialog; Task 5 packages fixtures and records manual Kodi UI status. |

| Task | Internal consistency check and ruling |
|---|---|
| 1 | Parser/tracker module and tests align. Ruling: explicit `AD_START_<seconds>` duration is finite and capped at 300 seconds; exact punctuation and marker-vocabulary values are candidate-eligible, broad substring matches are not. |
| 2 | New session callback can preserve `IcyDecoder` audio path; tests cover actual relay output and events. Timeout must be ticked through the existing service loop even without new metadata. |
| 3 | Store/API boundaries align: authenticated loopback requests only, current session/candidate equality, bounded profile JSON. |
| 4 | Plugin home route and dialog use existing Kodi list/dialog APIs; it is reachable while background audio plays, not inserted into the stock OSD. |
| 5 | Fixture server, tests, docs and package checks align with all spec acceptance criteria; Kodi UI remains explicitly pending if locked. |

## Decisions / rulings

- Ruling: unknown markers remain observational and never enable replacement or success beacons — ICY metadata-to-audio alignment is unproven — if wrong, this delays monetization on ICY sources but avoids fabricated ad delivery.
- Ruling: a likely unknown marker starts an inferred break; a track-like return or same marker after intervening metadata ends it within 300 seconds; expiration is unresolved, never synthetic end — user requested bounded best-judgment tracking — if wrong, some provider conventions may require a station-specific learned mapping.

## Task progress

- Baseline: `python3 -m pytest -q` → 540 passed in 84.23s.
- Task 1: complete (commits a947ae0..b05f21e; initial review Important findings fixed in round 1; re-review approved)
- Task 2: complete (commits b05f21e..3a0cc1c; initial review Important finding fixed in round 1; re-review approved)
- Task 3: complete (commits 3a0cc1c..818bd13; both fix rounds independently re-reviewed and approved)
- Task 4: complete (commits 818bd13..01f1c8c; review and fix-round re-review approved)
- Task 5: complete (commit 587a4ae; bounded wrapped-token recognition, 87 focused marker tests pass)

- Task 1: minor (deferred): the full suite prints an intermittent background server exception banner without a traceback; exact source is unconfirmed, and implementer reports it on both Task 1 full-suite runs. Capture diagnostics on the next required full run if it recurs; do not attribute it to the marker code without evidence.
- Task 1: fix round 1/5 (2 addressed, 0 open — expire `_last_title` beyond the 300-second retention window; apply a newly learned exact label before duplicate suppression; commits d5d0753..b05f21e; re-review approved)
- Ruling: exact vocabulary alone is too narrow for the user's request to recognize unknown non-track-looking metadata and marker wrappers such as `#AD_START#`; Task 5 adds only bounded, visibly delimited patterns and wrapper normalization, while bare punctuation remains direction-neutral and ordinary track strings cannot match via substring — this preserves broad station compatibility without turning ordinary titles into global cues; if the strict syntax still misses provider conventions, listener labeling remains the fallback.
- Task 5: complete (commit 587a4ae; independent review approved)

- Task 2: fix round 1/5 (1 addressed, 0 open — reject oversized candidates to preserve exact learnable identity; commits b681abf..3a0cc1c; re-review approved)

- Task 6: complete (commits `79908df..cfd8ccf`; initial review findings fixed and independent re-review approved; 699-test full suite/package gates pass; manual Kodi UI check remains pending on user's local Mac)
- Task 6: fix round 1/5 (3 addressed, 0 open — add localized playable scenario entries using generated MP3, pace complete ICY frames, and verify observation-only behavior through RelayServer/SessionManager; commits `79908df..cfd8ccf`; scoped independent re-review approved)
- Ruling: listener labels must act on the current session even when persistence is declined; the first Task 3 API only stored permanent rules, which made the approved optional-remember UI inert. Extend the tracker/control path with a session-only exact override and a label observation, while storing a station mapping only when requested — this makes the UI promise true without polluting the profile; if session labels affect timing too strongly, they still remain observation-only and expire with the session.
- Task 3: fix round 1/5 (1 addressed, 1 open — optional persistence works, but failed remembered persistence was reported after applying the session label; commits d80d40b..eedc0a0)
- Task 3: fix round 2/5 (2 addressed, 0 open — persist remembered labels before applying, so failed saves have no session effect; commits eedc0a0..818bd13; re-review approved)
- Task 4: initial review requested one fix (distinguish remember-dialog dismissal from selecting “session only”)
- Task 4: fix round 1/5 (1 addressed, 0 open — dismissed/cancelled memory choice now causes no session or persistence effect; commits fdd23c1..01f1c8c; re-review approved)
