# Monetization follow-on roadmap

The observation POC establishes discovery, same-stream evidence and bounded
embedded relay behavior. It does not implement VAST, ad replacement, impression
tracking or listener ad budgets. No VAST tag is required for this POC. Each stage
below needs its own implementation plan and controlled acceptance evidence.

Proceed under the user's stated Kodi repository-policy exception for this addon;
that is a project assumption rather than independently verified repository
approval. Rights to replace/distribute particular station content and the demand
provider's arrangements are separate from that assumption. Radio Browser is a
directory, not an advertising-inventory grant. Preserve the monetization objective
while settling those arrangements for any production rollout.

## 1. VAST audio and preroll

Decide the demand endpoint and supported VAST version/audio media, wrapper limit,
request deadline, redirect policy, consent inputs, creative expiry and provider
caching/reuse rules. Specify codec validation and what happens on no-fill,
timeout, unsupported or malformed creative. Define the source of truth for
playback position and cancellation before implementing tracking.

Required evidence: local VAST fixtures for direct response, nested wrappers,
wrapper cycles/limit, no-fill, timeout, malformed XML and unplayable media; known
audio heard in the target Kodi runtime; impression/start/quartile/completion/error
records aligned to actual playback. Fetching or caching a creative must never
count as an impression. Stop/cancel must not emit completion, and retries must
not duplicate tracking. Successful no-fill must return to station playback.

## 2. HLS break replacement and fallback

Choose supported source/creative codecs, sample rates/channel layouts and segment
containers, including initialization data. Decide break under/overrun, clock and
discontinuity handling, partial-break entry, rendition switching, buffering and
rejoin behavior. Choose whether to pad, truncate or retain source when creative
duration differs from the declared break. No-fill or incompatible media must
preserve the original stream.

Required evidence: controlled playlists with known programme/ad tones and exact
media boundaries; explicit OUT/CONT/IN, DATERANGE updates, missing ends,
discontinuities, signed relative URLs and concurrent rendition requests; audible
Kodi output and player buffering/rejoin measurements. Align substitution to the
media timeline, not cue-download time. Prove no duplicated impressions across
playlist reloads, rendition changes and retries. Demonstrate fallback with
unavailable and incompatible demand. In-band cues and continuous streams have
separate coverage; absence of HLS cues does not prove absence of ads.

## 3. Continuous-stream replacement investigation

Determine whether a participating continuous source exposes sufficiently reliable
boundaries and whether audio can be switched/rejoined without corrupting MP3/AAC
frames or introducing unacceptable silence. ICY titles alone do not establish an
ad break. Decide supported framing/codecs, a boundary signal contract, latency,
no-fill behavior and whether demux/remux work fits the embedded-Python constraint.
Do not silently depend on an external executable or hosted relay.

Required evidence: controlled MP3/AAC/ICY streams with known tone transitions and
split metadata, measured audible boundary accuracy, stop/switch/restart and
network-failure cleanup, and a demonstrated source-preserving fallback. If the
boundary or codec requirements cannot be met, record that limitation and keep
continuous replacement disabled. Investigating in-band SCTE-35 or ID3 requires a
separate parser/coverage plan; the present observer makes no such claim.

## 4. Listener ad budget and programme buffering policy

Decide whether extra advertising increases playback delay, skips programme
content, or uses catch-up. Define an understandable listener setting (for example,
maximum ads/minutes per hour), maximum buffered duration/storage, eviction,
cancellation and crash recovery. Separate cached creatives from cached programme
audio and honor demand expiry/reuse rules. Buffering policy cannot be an implicit
side effect of increasing the budget.

Required evidence: known audio and a simulated clock showing budgets enforced
against actual played ads, including partial/cancelled creatives, retries,
no-fill, expired creative, restarts and buffer exhaustion. Measure playback delay,
programme loss/catch-up and resource bounds in Kodi. Verify storage cleanup and
recovery after failure. More requested ads do not guarantee available paid demand
or revenue; forecasts require measured demand, delivery and valid tracking.
