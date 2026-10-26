# Radio Browser HLS Ad Marker Sampler

## Intent

Create a repeatable developer tool to find and observe ad-break evidence in
Radio Browser stations marked as HLS, with emphasis on `cdnstream1.com` sources.
The tool provides longer samples than the existing one-shot reachability probe
so playlist cues and observed AdsWizz HLS patterns have time to appear. It does
not insert ads and does not connect a production VAST tag.

## Goals

- Discover a bounded number of Radio Browser entries marked HLS and whose
  directory stream URL belongs to `cdnstream1.com` or a subdomain. Use the
  directory's raw-row continuation signal so malformed entries do not stop
  paging early.
- Sample each selected station for at least 1,200 seconds by polling HLS
  playlists, following a bounded master-to-media path, and using the existing
  URL policy and playlist parser. Read a response body only when its
  `Content-Type` is an allowlisted HLS playlist type; reject audio, unknown, or
  missing types before reading the body. Repeated reachability failures may
  end an observation early; report its actual elapsed duration.
- Reuse `CueTracker` to log explicit HLS cue and supported AdsWizz segment
  break events, including unresolved endings and sequence gaps.
- Emit bounded JSON Lines observations during sampling so partial results remain
  available if a run is interrupted; flush each record as it is emitted.
- Keep output useful for matching stations while omitting stream URLs, query
  strings, segment paths, raw AdsWizz context/IDs, and raw cue payloads.
- Clearly distinguish `marker_observed` from `no_marker_observed`; a 20-minute
  sample without a marker is not evidence that a station never carries ads.

## Non-goals and safety limits

- Do not replace programme audio, fetch media segments, issue VAST requests, or
  send success beacons.
- Do not change Kodi runtime behavior or Radio Browser click counts.
- Do not infer a break from silence or a marker-free time window.
- Do not broaden AdsWizz matching beyond evidence already supported by the HLS
  parser. New patterns require observed manifests and separate review.
- Do not scan the full Radio Browser directory without a page bound or run an
  unbounded number of station samplers.

## Approaches considered

1. **Add long-running options to `smoke_radio_browser.py`.** This keeps one
   command but mixes the existing fast reachability check with a 20-minute
   observation job.
2. **Add a dedicated developer sampler using existing discovery, transport,
   HLS parsing, and cue tracking modules.** This is the selected approach: it
   keeps the quick probe stable and avoids changes to the Kodi add-on runtime.
3. **Sample through the Kodi service.** This mirrors playback more closely but
   requires a Kodi session and affects runtime code even though the task is
   playlist discovery.

## Proposed behavior

Add `tools/sample_hls_ad_markers.py`. The command scans a caller-bounded number
of Radio Browser pages, filters entries with `hls` set and a directory URL host
equal to `cdnstream1.com` or beneath that domain, and caps the selected candidate
count. Candidate sampling runs with a small fixed upper bound on concurrent
stations. Each selected station receives its own full-duration sampling window;
the default and minimum window is 1,200 seconds.

Implementation bounds: scan 20 pages by default and at most 100; sample eight
candidates by default and at most 20; use four workers by default and at most
four; allow sample durations from 1,200 to 7,200 seconds; set a 15-second total
deadline for each HTTP request; and clamp playlist polling to 2–10 seconds using
half the target duration. Emit at most 1,024 marker events per station, with
station names bounded to 128 characters; a capped event stream reports that it
was truncated in the station summary.

Each worker requests the directory station URL through the existing transport
policy, follows at most the already supported HLS master depth, then refreshes
the selected media playlist on a bounded interval until the station's sampling
window ends. It requires an allowlisted HLS playlist `Content-Type` before
reading any response body; audio, unknown, or missing types are rejected
without a body read. This strict gate can exclude servers that mislabel HLS
responses. Polls are cancellation-aware, each request is bounded, and failures
affect only that station's result. Tolerate three consecutive reachability
failures during polling, resetting after a successful poll. A fourth ends the
station early with `reachability_failure`. Parser, unsupported, and policy
failures remain terminal and retain their failure outcomes. Earlier marker
and unresolved-break event records remain available after a failure; the
summary does not reclassify a failed sample as `marker_observed`.

The existing `CueTracker` remains the source of break transitions. A narrow
JSON Lines sink writes only an allowlist of event kinds and fields needed for
discovery: station UUID and sanitized directory name, protocol/source class,
media sequence, discontinuity, media position, duration, classification, and
reason. It never serializes `Event.evidence` or unapproved details. Event and
summary output contains no stream URL or opaque AdsWizz/cue value. The run ends
with a per-station coverage summary that reports elapsed sample time, poll
count, observed event count, and either `marker_observed`, `no_marker_observed`,
or a bounded failure outcome.

If discovery selects no candidates, emit one bounded `discovery_summary` JSON
Line with `no_candidates`, pages read, and `selected_count: 0`. Normal outcomes
exit 0. Discovery failure or any station policy, reachability, or parser failure
exits 1; Ctrl-C exits 130. Station failures do not cancel other workers.

## Constraints and implementation calls

- Reuse the standard library and the add-on's existing Python modules; add no
  runtime dependencies.
- Keep the sampler in `tools/`, outside the packaged add-on manifest.
- Bound directory pages, candidates, concurrency, playlist size, master depth,
  request timeout, polling interval, and output fields.
- Use the existing URL policy for every resolved playlist request, including
  redirects. Do not print request URLs in exceptions or output.
- Keep the output on stdout as JSON Lines so callers can save it where they
  choose. Do not create or overwrite files automatically.
- Unit tests use a local loopback HLS fixture and a controllable clock so test
  duration stays short while the command-line minimum remains 20 minutes.

## Acceptance

- Discovery selects only HLS entries on the configured `cdnstream1.com` domain
  and obeys all directory/candidate limits.
- A controlled manifest with explicit HLS cues emits exactly the expected
  start/end observations; AdsWizz fixture segments emit the supported observed
  segment events across overlapping windows.
- Playlist-only sampling never requests media segment URLs and never invokes
  Radio Browser's click endpoint; non-playlist response bodies are not read.
- Interrupted and failed samples retain already emitted JSON Lines and classify
  any still-open break as unresolved.
- Output omits test tokens, full URLs, segment paths, AdsWizz context/IDs, and
  raw cue payloads, including when hostile values appear in fixtures.
- A marker-free 20-minute sample reports only `no_marker_observed`.
- Existing add-on tests and package checks remain unaffected.
