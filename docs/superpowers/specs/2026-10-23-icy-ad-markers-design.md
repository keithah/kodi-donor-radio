# ICY ad marker discovery and learning

## Intent

Recognize ad-break signals carried in ICY metadata and make unknown station
markers learnable by the listener. This helps identify replacement opportunities
without mistaking ordinary artist/title changes for ad breaks.

## Goals

- Parse explicit ICY marker formats, including `AD_START`, `AD_START_<seconds>`,
  `AD_STOP`/`AD_END`, and documented provider-specific forms.
- Support Triton-style TildeATS category `4` as an ad-break signal and its `##`
  end-break convention, clearly scoped to that format.
- Surface unusual ICY titles, including exact `-`, `#`, or `*` values and
  wrapped/unknown marker-like values, as candidates, and use a bounded
  five-minute metadata history to infer likely start/end transitions from
  marker-like values and a return to track-like data.
- Mark heuristic transitions as inferred, not explicit. Candidate classification
  alone must never replace content or emit a success beacon.
- Let a listener label the current or latest candidate as a break start or end.
  The label applies to the current session whether or not the listener remembers
  that exact marker for the station.
- Store learned mappings per station with bounded, safe profile storage.
- Preserve current metadata observation and audio bytes exactly.
- Keep existing HLS cue parsing and HLS-only replacement eligibility unchanged.

## Non-goals and safety limits

- Do not treat arbitrary titles containing words such as “ad”, “promo”, or
  “commercial” as confirmed markers.
- Do not infer the direction of a bare punctuation marker globally. `-`, `#`, and
  `*` may only become active after explicit format semantics or listener labeling.
- Do not keep unbounded metadata history or infer an end after the five-minute
  observation window expires.
- Do not replace live ICY audio in this phase. ICY observations arrive at metadata
  block boundaries, whose alignment with audible samples is not proven by this
  adapter; the current ad composer replaces only eligible HLS intervals.
- Do not add Kodi core or skin modifications.

## Approaches considered

1. **Treat every suspicious title as an automatic ad cue.** Rejected because
   ordinary station titles and punctuation can cause false replacement.
2. **Support only hard-coded provider marker strings.** Safe for known conventions,
   but leaves station-specific markers undiscoverable.
3. **Parse explicit conventions and learn unknown exact strings per station.**
   Selected: it provides immediate compatibility while letting users teach local
   marker semantics without silently guessing start/end meaning.

## Proposed behavior

The ICY relay continues to emit its existing metadata observation. A marker parser
classifies only exact recognized formats and emits bounded marker events with
station and session identity. Explicit known markers can be logged as signaled
break-start/end observations. Unknown non-track-looking values become candidates.
Recognition may strip surrounding marker punctuation for exact command parsing
(for example `#AD_START#`) or identify bounded marker-like token patterns, but
must not use broad substring matching or assign a global direction to bare
punctuation.
Keep at most 64 recent metadata observations and expire them after five minutes.
When an unknown marker-like value appears outside a break, log an inferred start;
when ordinary track-like metadata returns within five minutes, or the same marker
reappears after intervening metadata, log an inferred end. If no return occurs
within the window, log the break as unresolved and do not invent an end. A repeated
identical consecutive title is a duplicate, not a second marker. These transitions
are observational only.

Expose a `Label latest ICY marker` add-on action that remains reachable while
background audio continues. It shows the current candidate and lets the listener
label it as start or end. The label changes behavior for the current session even
if it is not remembered; choosing remember also stores that exact value for the
station. The stock Kodi player OSD is not modified. The canonical marker value is
the complete `StreamTitle` after trimming surrounding whitespace and uppercasing;
punctuation and wrapper characters are preserved. Parser output, current-session
candidate checks, label requests, and station-store lookups use this same value.
Thus `#AD_START#` and `AD_START` are distinct learned keys even though both parse
as the same explicit start command. Current-session labels expire with the
session; remembered marker records are scoped to station UUID and exact
normalized metadata. No broad substring matching is used.
Duplicate metadata blocks are deduplicated, and a repeated start/end cannot create
repeated transitions.

For this phase, confirmed ICY boundaries are discovery data only. They must not
feed the HLS composer or VAST success tracker because the add-on has no proven
sample-accurate ICY replacement clock. HLS `CUE-OUT`/`CUE-IN`, continuation, and
SCTE-35 `DATERANGE` behavior remain as implemented.

## Implementation choices

- Use an explicit parser for `AD_START[_<seconds>]`, `AD_STOP`, and `AD_END`.
- Parse the documented TildeATS structure only when all required fields and the
  terminating caret are present; category `4` identifies an ad break, a following
  category `0` song indicates content resumed, and the `##` end-break interpretation
  applies only to this provider format.
- Flag punctuation-only and otherwise non-track-looking metadata as candidates;
  infer a start/end only from the bounded sequence rule above. Marker-looking
  recognition uses exact token boundaries and bounded syntax; ordinary artist or
  title text containing marker words is not enough. A bare `-`, `#`, or `*` is
  never assigned a global start/end direction.
- Retain at most 64 observations in a five-minute monotonic window. Unresolved
  break windows expire without a synthetic end event.
- Store station marker mappings as versioned JSON below the add-on profile, with
  strict size/count/value limits and atomic writes.
- Expose a Kodi plugin home action that opens a labeling dialog while the current
  audio continues; the stock Kodi player OSD is not modified.
- Add controlled ICY fixtures for each explicit format, unknown-marker review,
  learned start/end mappings, duplicate suppression, and proof that no ICY marker
  activates ad replacement.

## Acceptance

- Known explicit marker formats produce exactly one correctly scoped observation.
- Wrapped known commands and bounded, visibly delimited unknown marker values are
  recognized without broad substring matching. Bare `-`, `#`, and `*` remain
  direction-neutral candidates.
- Unknown `-`, `#`, `*`, and suspicious title values produce inferred events only
  under the five-minute marker-to-track transition rule; manual labels take
  precedence for that exact station and value.
- Learned markers apply only to the matching station and exact marker value.
- A listener label applies to the current session whether or not it is remembered;
  declining persistence writes no station rule.
- A normal track-title change never activates a break.
- ICY audio output remains byte-identical after metadata stripping.
- No ICY signal enters ad composition, ad success tracking, or monetization beacons.
- Metadata history never exceeds 64 entries or five minutes, and expiration never
  fabricates an end marker.
- Existing tests and package checks pass; Kodi UI behavior is verified on target.

## References

- [SharpStream metadata trigger guidance](https://support.sharp-stream.com/support/solutions/articles/48001242530-metadata-ad-trigger-best-practices)
- [Triton Digital Icecast metadata and TildeATS](https://help.tritondigital.com/docs/icecast-metadata)
- [RFC 8216, HTTP Live Streaming](https://www.rfc-editor.org/rfc/rfc8216.html)
