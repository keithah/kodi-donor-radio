# VAST preroll and HLS midroll replacement: controlled streams

Status: proposed implementation design, awaiting review.

## Intent and baseline

Continue the approved monetization roadmap: resolve our own VAST tag, play a
preroll, replace a marked source break with the selected creative, then resume
programme content. Start with deterministic local streams and ad responses.
Do not count downloading, caching, manifest delivery, or segment requests as
played advertising.

The discovery/observation POC was merged as PR #1 at 4687232. Its 399 tests and
package validation pass. The target runtime is unpatched Kodi master 434da3c
on the existing Mac; all eight observation fixtures and five cue schedules
passed there without a USB workaround. The USB patch is not a dependency.

## Selected approach and alternatives

Extend the embedded Python relay to publish a composed HLS presentation. Kodi
plays one stream; replacement decisions operate on segment identities and media
boundaries. This builds on sessions.py, relay.py, hls.py and the Kodi service.
Keep observation evidence separate from decisions to replace media.

Switching Kodi files on cue receipt is rejected because receipt precedes audible
playback by a variable buffer interval. Runtime transcoding would broaden codec
support but introduces an executable/deployment dependency; it is deferred.
Fixture generation may use the existing development FFmpeg tooling.

The initial insertion path is deliberately restricted to controlled HLS audio.
Existing MP3/AAC/ICY playback and general Radio Browser observation remain as
implemented. Continuous-stream insertion and general commercial VAST media
compatibility are follow-on work, not claimed by this milestone.

## Media contract

Use clear, single-rendition HLS with AAC-LC audio in MPEG-TS, 48 kHz stereo,
matching codec configuration and complete independently decodable segments.
Generate distinguishable programme, original-break and replacement tones plus
spoken identifiers for manual listening. Fixture generation records actual
packet/sample durations and independently validates assets with FFprobe.

The local VAST MediaFile points to a complete HLS creative using the supported
HLS MIME type. The addon fetches and validates a bounded media playlist and all
creative segments before committing replacement. Check transport/audio headers,
codec configuration and measured duration; do not trust MIME or VAST Duration
alone. Reject encryption, external audio renditions, variant switching,
byte-range creative media and unsupported codecs in the insertion path.

Use whole AAC frames. Duration equality is checked against the source break's
actual media duration within one AAC frame, with no accumulated boundary drift.
Fixtures use matching frame counts, not nominal decimal durations alone.
No padding, arbitrary truncation, catch-up or additional programme deletion.

## VAST resolver

Implement a documented subset of VAST 4.x audio Linear ads: one standalone ad,
InLine and bounded Wrapper resolution, Duration, supported MediaFile selection,
Impression, Error and start/quartile/complete tracking. Wrapper tracking is
inherited. Respect supported wrapper controls; reject unsupported ad pods,
interactive/executable creatives and required features that cannot be honored.
This is not a claim of full VAST compliance.

Use existing validated, cancellable HTTP transport for every VAST, creative,
redirect and tracking URL. Test mode permits only the configured fixture origin
for advertising requests. General station input cannot activate ads or supply
an arbitrary VAST endpoint. No production demand or tracking calls in this phase.

Initial bounds: five wrapper hops, cycle detection, 256 KiB XML per response,
five seconds total for VAST resolution, ten seconds for creative preparation,
32 MiB total session creative cache, 128 creative segments, 60 seconds maximum
creative duration. All nested operations share their overall deadline. Disable
DTD/entity expansion and bound XML depth, node count and URL lengths. Cancellation
interrupts preparation and releases resources. No session-to-session creative
reuse; delete cached files when the session closes and clean abandoned files on
service startup.

Support the macros used by the controlled fixtures, including ERRORCODE and
CACHEBUSTING, with explicit encoding tests. Reject unresolved required macros.
Do not invent consent, identifiers, supported capabilities or audibility claims.

## Preroll

On selection of an insertion-enabled test station, prepare one creative within
the bounded deadline. If ready, compose its segments before the source content,
with a discontinuity at the transition and stable output sequence numbers.
Choose and retain the initial source window after preroll preparation. Retain
bounded source segments needed to bridge the preroll so live-window expiry does
not lose the first programme segments. The listener acquires preroll-length live
delay; this milestone does not silently catch up by discarding programme.

On no-fill, timeout, invalid XML, incompatible or unavailable media, start normal
station playback. Stop or station change cancels the entire session, including
any pending transition. Never resume an old station after cancellation.

## Midroll

Only explicit CUE-OUT followed by a coherent CUE-IN at known segment boundaries
makes an opportunity eligible. The complete marked interval must be available
and its end established before its first segment is exposed to Kodi. A
controlled rolling fixture provides sufficient lookahead; this requirement is
not assumed to hold for arbitrary live stations.

Prepare demand while programme audio plays. Commit only if a compatible,
fully cached creative fits the established interval. Replace the source break
segments in the outgoing playlist, insert the necessary discontinuities, and
resume at the original CUE-IN content segment. Never substitute programme
segments outside the marked interval. Maintain a session output-sequence and
discontinuity mapping that remains stable across playlist reloads and sliding
windows; preserve source identity separately for diagnostics.

Once any affected output segment is published, its identity and bytes are
immutable. A late creative cannot alter an already advertised interval. Repeated
playlist/segment requests do not create additional opportunities or impressions.
Cache entries remain pinned while referenced by an active output window.

Partial-break entry, missing or inconsistent end, malformed cues, duration
mismatch, no-fill, late demand or resource-limit exhaustion retain original
source audio for the entire opportunity. DATERANGE/SCTE-35 observation continues,
but replacement for those forms is deferred until explicit boundary support is
implemented. An early source mutation invalidates an uncommitted opportunity.
After commitment, already cached creative bytes are authoritative; a subsequent
unrecoverable player error terminates the session without claiming completion.

## Playback clock and tracking

Add a Kodi service adapter for player callbacks and position samples, scoped to
the active relay session and its output presentation. Pure modules own VAST
resolution, composition and tracking state. The relay exposes an internal
immutable output timeline; no extra public arbitrary-fetch endpoint is added.

Calibrate Kodi's getTime position against the composed presentation with
controlled media. Confirm the mapping across preroll, both discontinuities,
rolling windows, pause/resume and rebuffering before enabling successful
playback beacons. Receipt time or a timer started by downloading a cue is never
the replacement clock. If mapping cannot be established reliably, suppress
success beacons and report tracking unavailable rather than fabricate delivery.

For a mapped playing interval, emit impression/start on observed entry, quartiles
on forward playback through the thresholds, and completion only with evidence
that the creative ended normally. Pausing does not advance progress. Stop,
station change, errors or seeking past an ad must not produce completion or
fabricated quartiles. Do not infer missing playback history from a large position
jump. Document sampling accuracy; this is not sample-accurate speaker measurement.

Track each placement independently, deduplicating events and identical inherited
URLs within that placement. A bounded worker delivers test beacons without
blocking audio. Avoid automatic retries after an ambiguous network delivery so
one event cannot be billed twice by an endpoint without idempotency. Record
attempt/success/failure separately; a failed tracking call does not interrupt
playback. No persistent cross-session resend queue in this phase.

## Settings and diagnostics

Insertion is off by default and requires test mode plus explicit advertising
fixture settings. Separate preroll and midroll toggles permit isolated tests.
Invalid settings fail safely to observation-only playback. Apply changes at the
next session; never mutate an active presentation's policy.

Add bounded sanitized events for demand result, creative preparation,
opportunity eligibility, replacement commitment/fallback, playback progress and
tracking delivery. Use opaque placement and creative identifiers; redact URLs,
query strings, tokens and raw VAST. Keep cue observations unchanged so tests can
compare observed evidence against replacement decisions.

## Acceptance and execution sequence

1. Implement local VAST, beacon collector and distinct-audio fixtures, including
   wrapper cycles, empty responses, malformed XML, slow responses, failed media,
   codec and duration mismatches, and expired source windows.
2. Build and unit-test the resolver, bounded cache and tracking state machine
   against those fixtures without Kodi dependencies.
3. Compose preroll and eligible midroll presentations through the existing relay.
   Independently decode output and verify the sample/tone order: preroll,
   programme A, replacement, programme B. Original-break audio must be absent
   only when replacement is committed. Programme boundaries must be preserved.
4. Integrate Kodi playback feedback and demonstrate accurate test-beacon timing,
   including prefetch without playback, pause, buffering, stop, switch, seeks,
   repeated fetches and errors. If timing evidence fails, keep success tracking
   disabled and fix the clock mapping before accepting the milestone.
5. Run existing tests/package checks, new failure and concurrency tests, and actual
   Kodi master playback. Verify controlled beacon counts and record measured
   transition behavior. Actual audible-output validation must be reported
   separately from decoded-media and player-clock evidence.
6. Obtain Steward and CodeRabbit review before the next implementation PR, using
   the user's selected subagent-driven workflow for independent implementation
   tasks and review. Do not merge automatically without user authorization.

The first follow-on after this milestone is wider VAST creative support and
well-defined live break contracts. Listener ad budgets, extra-ad programme
buffering, production demand, external reporting and continuous-stream splicing
remain later roadmap stages.

## References

- Existing approved design: 2026-10-21-kodi-radio-design.md
- Existing follow-on contract: ../../monetization-roadmap.md
- IAB VAST 4.3: https://iabtechlab.com/wp-content/uploads/2022/09/VAST_4.3.pdf
- HLS media/discontinuity rules: https://www.rfc-editor.org/rfc/rfc8216.html
