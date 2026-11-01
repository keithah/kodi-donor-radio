# Monetization follow-on roadmap

The branch implements Radio Browser discovery and controlled loopback-only VAST
preparation, preroll, HLS midroll replacement and guarded fixture tracking.
General Radio Browser playback remains outside ad insertion.

The separate native inputstream lab captured a complete 180.032-second HLS
programme through four normal Kodi pauses without detected gaps or replay in the
tested buffered profile. Treat that as a scoped historical result; its frozen
offline clock estimator passed all 171 qualified measurements, but the raw
player/delay clock, low-latency mode, startup and pause-tail timing remain
unqualified. On 2026-10-30, a fresh 360-second Kodi master capture of the
bounded 128-segment fixture aligned the controlled replacement tone with all
six loopback VAST success events, but then stalled near the fixture's expected
end at player time 260.408 seconds, produced 76.4 seconds of captured silence,
and did not reach natural EOF. No production success beacons are
enabled; `docs/superpowers/reports/kodi-owned-clock-beacons-2026-10-30.md` is the
current acceptance status. The native lab is preserved as optional source
patches; it is not installed or required by the shipped Python addon. The
buffered-profile capture details are in
`docs/superpowers/reports/kodi-owned-hls-final-capture-2026-10-28.txt`.

Proceed under the user's stated Kodi repository-policy exception for this addon;
that is a project assumption rather than independently verified repository
approval. Rights to replace/distribute particular station content and the demand
provider's arrangements are separate from that assumption. Radio Browser is a
directory, not an advertising-inventory grant. Preserve the monetization objective
while settling those arrangements for any production rollout.

## 1. VAST audio and preroll — implemented for loopback fixtures; acceptance pending

The current fixture implementation supports a bounded VAST audio subset, validates
AAC/TS creative media, and falls back on no-fill or unsupported content. Before
production demand, settle consent inputs, creative expiry/reuse, contractual
tracking semantics, and any broader VAST requirements. Do not expand advertising
origins or enable production success beacons until the Kodi clock mapping passes.

Remaining evidence: connect a qualified live source clock to the actual addon
playback session, then correlate ad placement and tracking across moving HLS
windows and both ad discontinuities. The offline native lab estimator does not
yet provide that runtime contract. Verify seek/reset/ambiguous-jump behavior and heard output
separately. Existing deterministic fixtures cover direct/wrapped VAST, cycles, no-fill, timeout,
malformed input and incompatible media. Fetching or caching a creative must never
count as an impression. Stop/cancel must not emit completion, and retries must
not duplicate tracking.

## 2. HLS break replacement and fallback — implemented for controlled fixtures

The current contract is clear single-rendition AAC-LC in MPEG-TS at 48 kHz stereo,
with exact frame-count matching, whole-break replacement and source-preserving
fallback. Do not silently broaden this to encrypted/variant/continuous streams or
pad/truncate mismatched media. Future codec and partial-break support needs separate
design and fixtures.

Required before release: verify the sample-zero media anchor against decoded
output and listen for programme/ad transitions, and retain the existing
relay-decoding evidence for cue boundaries, missing ends, discontinuities, cache
retention and fallback. In-band cues and continuous streams have separate
coverage; absence of HLS cues does not prove absence of ads.

## 3. Continuous-stream replacement investigation

Determine whether a participating continuous source exposes sufficiently reliable
boundaries and whether audio can be switched/rejoined without corrupting MP3/AAC
frames or introducing unacceptable silence. ICY titles alone do not establish an
ad break. Explicit Adswizz `adw_ad=true/false` metadata is now logged as an ICY
break observation, but does not enable ICY audio replacement or VAST beacons.
Decide supported framing/codecs, a boundary signal contract, latency, no-fill
behavior and whether demux/remux work fits the embedded-Python constraint.
Do not silently depend on an external executable or hosted relay.

Current AdsWizz HLS discovery is also observation-only: an `adswizzContext`
EXTINF comment paired with an `ad_*.aac` segment on the observed AdsWizz delivery
host creates segment-derived break boundaries. The opaque context and segment URL
are not written to diagnostics. A skipped sequence while a break is open is
reported unresolved; this evidence does not enable ad replacement or VAST beacons.

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
