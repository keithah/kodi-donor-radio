# Task 6 report — playback-evidenced ad tracking

## Result

`AdTracker(session_id, emit, enqueue)` accepts immutable `Presentation` snapshots and `PlaybackSample` observations. Fetch, preparation, and playlist reads cannot produce tracking success. An impression/start needs a mapped playing sample within 250 ms of the placement start, or a continuous forward crossing from before that start. Quartiles and completion require a forward crossing across a sample pair no more than 750 ms apart, with movement no faster than the observed interval plus 250 ms. Paused, buffering, seeking, unmapped, backwards, stale, duplicate, and discontinuous observations do not backfill milestones. `finish("ad_end")` can corroborate completion only when a continuously observed final sample is within its sample interval of the creative end; `finish("source_end")` cannot complete an ad. Cancellation prevents later completion.

`BeaconWorker(client, cancel, fixture_origin)` has one delivery thread and a 128-item queue. It accepts only same-origin HTTP(S) URLs without credentials or controls. Each request passes an absolute monotonic deadline of two seconds and `allowed_origin=fixture_origin`. It attempts each placement/event/URL at most once, including ambiguous failures; a later placement can use the URL. `close()` briefly drains fast requests, then cancels slow delivery and drops remaining queued requests. Public counters `failures` and `overflow` are diagnostic only.

`enqueue_ad_error(worker, placement_id, error)` sends deduplicated inherited VAST Error URLs with `[ERRORCODE]` substituted. It uses fixed VAST 4.2 codes and maps unknown codes to 900. The code mapping was checked against the [IAB Tech Lab VAST 4.2 specification](https://iabtechlab.com/wp-content/uploads/2019/06/VAST_4.2_final_june26.pdf), section 2.3.6.3. The dispatcher normalizes the broad error codes of the existing resolver/media parser before delivery:

| Existing error | Existing code | Beacon code | Basis |
| --- | ---: | ---: | --- |
| Unsupported VAST document | 100 | 900 | Resolver uses one reason for bad root and unsupported version; specific code cannot be inferred |
| Invalid advertising URL / Invalid VAST ad | 100 | 101 | Invalid VAST structure or URL |
| Advertising origin rejected | 100 | 900 | Undefined security rejection |
| Unsupported pod, required feature, or creative | 403 | 200 | Ad type cannot be played |
| Invalid VAST duration | 403 | 101 | Invalid response structure |
| No supported VAST media | 403 | 403 | No supported MediaFile |
| Media parser/preparation failure (for example, unsupported AAC profile) | 403 | 405 | MediaFile was found but cannot be displayed |
| Creative request or cache failure | 403 | 400 | Player cannot display the Linear Ad; no evidence of a missing file |
| Media origin rejected | 403 | 900 | Undefined security rejection |
| Wrapper following disabled | 302 | 300 | General Wrapper failure |
| Wrapper limit/cycle, no ad after wrappers | 302/303 | 302/303 | Specific Wrapper codes |
| Wrapper request failure/transport | 301 | 301 | Wrapper URI unavailable or timed out |

The resolver also uses 301 for a direct initial VAST request failure, but it has no inherited Error URL at that stage, so no beacon is dispatched. If direct-request Error URLs are later surfaced, that case should use 900 rather than the Wrapper-specific 301. XML parsing failure remains 100. For better specificity in the resolver, split `Unsupported VAST document` into wrong root (101) and unsupported version (102), and distinguish missing MediaFile (401) from a general creative request failure (400) where possible.

## RED / GREEN

- RED: `python3 -m pytest tests/test_ad_tracking.py -q` failed collection because `ad_tracking` was missing.
- RED: `python3 -m pytest tests/test_ad_tracking.py tests/test_events.py -q` reported two expected failures: post-unmapped near-start seek emitted an impression, and opaque diagnostic IDs were not yet allowlisted.
- RED: a timeout-bounded focused run of the new worker close and credential tests exceeded three seconds because `close()` waited indefinitely on in-flight delivery.
- RED: targeted tests for unsupported-ad, bad-media, and disabled-wrapper codes showed incorrect 403/302 macro substitutions.
- GREEN: `python3 -m pytest tests/test_ad_tracking.py tests/test_events.py tests/test_vast.py -q` — 43 passed (final rerun below).
- `git diff --check` and `python3 -m compileall -q` on changed library modules passed.

## Task 7 handoff

- `PlaybackSample` is defined in `ad_models.py` and re-exported from `ad_tracking.py`. Positions are presentation output seconds; clocks are monotonic seconds.
- `AdTracker.observe(presentation, sample)` and `finish(reason)` are pure state operations; `enqueue` is called as `(placement_id, event, url) -> bool`.
- `emit` receives one mapping for each confirmed milestone: `{kind: "ad_tracking", session_id, event, placement_id, creative_id, reason}`. The IDs are 16-character opaque SHA-256 prefixes, and reason is fixed (`queued` or `no_endpoint`). An adapter may map `event` to the `Event` evidence/source fields and the IDs/reason to details. No URL or raw VAST identifier enters this mapping.
- Use `finish("ad_end")` only for a callback known to mean creative end. A Kodi source end callback should use `finish("source_end")`; stop/cancel should use `finish("cancelled")`. The tracker cannot infer a creative end from a source end callback.
- A resolver failure must be surfaced by the calling layer to `enqueue_ad_error`; Task 5 currently catches preparation exceptions internally and returns no ad. That path needs a callback or equivalent handoff if production error beacons are desired. No production tracking requests were wired in this task.
