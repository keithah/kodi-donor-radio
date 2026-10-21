# Kodi radio: discovery, stream observation, and monetization

## Intent and decisions

Build a Kodi radio addon using Radio Browser for general station discovery. The product goal is preroll and midroll advertising using our own VAST tag, followed by listener-controlled ad frequency and optional buffering. The first deliverable is a working discovery/playback addon that observes and correctly logs explicit ad cues without changing the audio.

The user identifies themselves as a Kodi board member and directs this project to proceed on the basis of an exception for monetization of this addon. This is a project assumption, not independently verified repository approval. Monetization remains the product goal; the normal published restriction is not a reason to remove it from the design.

Use an open-source Python relay packaged within the addon and executed by Kodi. No separately installed proxy, executable, or hosted media relay is required. Begin with general discovery and controlled test streams rather than a curated set of commercial stations.

## Architecture

One addon contains a music-plugin entry point for navigation and a service entry point for session lifetime and relay handling. Shared pure-Python modules implement discovery, playlist parsing, stream forwarding, cue normalization, and session state; they remain testable outside Kodi.

Playback flow: Radio Browser station → resolved upstream URL → service session → loopback playback URL → Kodi player. Discovery uses server discovery/failover, a descriptive User-Agent, bounded requests, and station UUIDs. Search and country/tag browsing use pagination; station selections report the documented click event once per selection. Avoid downloading the complete directory on startup.

The service binds an OS-assigned port on 127.0.0.1. A session-scoped opaque URL identifies a registered upstream; the relay must not expose an arbitrary URL-fetch endpoint. Its connection descriptor lives in the addon profile with restrictive permissions where supported. Stop/cancel signals close upstream requests and release session resources. Kodi shutdown uses the monitor abort mechanism and bounded thread joins.

All relayed requests use timeouts and bounded buffers. Validate redirect targets and upstream schemes, limit redirects, and block loopback/private-network destinations for general discovery. Controlled test streams use an explicit test mode with tightly scoped local endpoints. Never forward upstream credentials or cookies to a different origin automatically. Redact tokens, query strings, and sensitive metadata in diagnostics.

## Initial supported playback

Support clear HTTP(S) MP3/AAC streams with ICY metadata and ordinary live HLS audio streams. Resolve supported M3U/PLS station indirection with bounded depth and loop detection. Identify protocols from response content as well as URLs.

For continuous streams, inspect metadata from the same upstream connection whose audio is relayed. Preserve or correctly remove metadata framing according to the downstream request; never leak ICY metadata bytes into the audio payload.

For HLS, relay master/media playlists and segments. Rewrite supported URI references to session URLs, resolve relative paths against the final upstream playlist URL, and retain query parameters internally. Inspect the actual playlist responses served to Kodi rather than a separate polling session. Preserve segment payload bytes and byte-range semantics. Initial support excludes encrypted/DRM media, low-latency HLS extensions, and parsing in-band transport-stream SCTE-35 or ID3 events. Reject unsupported cases clearly rather than partially rewriting a playlist and silently losing coverage.

The initial relay observes playback; it does not transcode, switch creatives, or claim sample-accurate playback timing. Its manifest control is the foundation for later replacement, not evidence that seamless insertion already works.

## Cue interpretation and event model

Initial HLS detectors recognize SCTE-35-bearing EXT-X-DATERANGE fields and common EXT-X-CUE-OUT, EXT-X-CUE-OUT-CONT, and EXT-X-CUE-IN forms. Generic date ranges and discontinuities alone are not ads. Preserve bounded raw cue evidence; distinguish a signaled opportunity from proof that an advertisement aired. Validate SCTE-35 payload syntax where supported, and do not invent decoded splice semantics from opaque payloads.

ICY metadata is recorded as metadata observation. A changed StreamTitle, a blank title, or text containing “advertisement” is not a confirmed break. Provider-specific interpretation can be added later through explicit adapters and fixtures.

Normalized records include schema version, session ID, station UUID, protocol, event kind, source tag, cue/break ID when present, evidence classification, observed UTC time, monotonic receive time, media sequence/discontinuity context, media timeline position when derivable, duration when provided, and bounded sanitized evidence. Unknown values remain absent rather than becoming zero.

Event kinds include metadata_observed, cue_observed, break_start, break_update, break_end, and break_unresolved. Repeated playlist windows do not emit duplicate events. Updated duration/end attributes may emit an update. Deduplication is scoped by session and timeline epoch, with bounded retention. Reconnects and discontinuities must not merge unrelated breaks merely because an ID repeats. DATERANGE IDs retain playlist/epoch identity across discontinuities per RFC 8216; generic CUE association crosses only an explicitly signaled consecutive boundary, as detailed in `docs/implementation-decisions.md`.

Joining an ongoing break produces an in-progress observation rather than an invented original start. A missing end stays unresolved; a declared duration is an expected end, not an observed cue-in. Log explicit end and inferred expiry separately. Seeing no cues means no cues observed, never proof that no ads exist.

Receive time and Kodi playback time are distinct. Logs describe upstream media timing when available; Kodi buffering prevents treating receipt as the audible boundary. Later replacement must happen on the media timeline, not via a timer started when a cue is downloaded.

## Kodi integration and packaging

Use Python 3 and current supported Kodi APIs, including xbmcvfs.translatePath for special paths. Provide addon.xml, localized settings and labels, source, LICENSE.txt, and required artwork before repository submission. Use GPL-2.0-or-later for project-authored code unless the owner chooses otherwise; dependencies and assets must be compatible.

Store settings, descriptors, and optional rotating structured diagnostic logs within the addon profile. Route Kodi diagnostics through xbmc.log at debug level. No remote analytics in the POC. Diagnostic logging is opt-in, bounded, and excludes secrets. Provide search, country/tag browsing, playback, and a clearly labeled test-stream entry available only in test mode.

Service failure yields a clear playback error and cleans up the session. Do not silently bypass observation with direct playback while claiming inspection is active. Discovery failures offer retry without hanging the Kodi UI.

## Controlled validation

Use deterministic local HTTP fixtures with valid playable audio and rolling HLS playlists. Author or synthesize fixture audio; no commercial stream recordings are required. Keep development fixture tooling outside the distributable addon.

Test continuous audio with metadata split across arbitrary network reads; HLS cue-out/in; date-range updates; repeated windows; joining mid-break; missing cue-in; discontinuities; reused IDs; redirects; relative URLs; byte ranges; malformed cues; disconnects; cancellation; and resource bounds. Negative fixtures include ordinary titles, generic date ranges, and discontinuities without cues.

Integration tests must prove that inspected playlists are those delivered downstream, that continuous playback uses one upstream connection per session, and that audio payload is unchanged. Compare emitted events with expected fixture timelines. Test opaque session routing and rejection of arbitrary upstream requests.

Live Radio Browser checks validate discovery and a small sample of reachable stations, without requiring those stations to expose ads. A real Kodi playback smoke test is required to claim Kodi runtime compatibility; pure-Python tests alone are insufficient. Report unavailable Kodi execution explicitly.

Acceptance: installable POC package; station discovery and supported playback; deterministic correct fixture event logs with no duplicates or false confirmed breaks; bounded cleanup; documented protocol coverage; and evidence of actual Kodi testing when available.

## Monetization stages

After the POC, implement VAST audio resolution and preroll: wrapper limits, request timeouts, supported media selection, creative validation, and playback-driven impression/quartile/completion/error reporting. A fetched or cached creative is not an impression. Test against controlled VAST responses before enabling real demand.

Next implement HLS midroll replacement for explicit, well-defined opportunities. Select compatible media or introduce a separately reviewed conversion strategy; align segment boundaries, timestamps, codec parameters, discontinuities, and break duration. Define underrun/overrun behavior and resume original content reliably. On unavailable or invalid demand, preserve the original stream. Continuous MP3/AAC replacement needs separate frame/timeline work and is not implied by HLS support.

Later listener settings express an ad budget, such as maximum ads/minutes per hour. Separate creative prefetch from buffering programme audio. More inserted advertising either increases delay, displaces programme content, or requires catch-up; choose that user-visible policy before implementation. Creative reuse, expiry, and tracking must follow the selected demand provider's contract. Requesting more ads does not guarantee available paid demand or revenue.

Repository-policy exception and rights to replace/distribute particular station content are separate matters. Radio Browser is a directory, not a grant of advertising inventory. Production rollout needs the relevant station and demand arrangements; this does not block controlled fixtures or the observation POC.

## Sources

- Radio Browser API: https://docs.radio-browser.info/
- Kodi addon rules: https://kodi.wiki/view/Add-on_rules
- HLS and SCTE-35 date ranges: https://www.rfc-editor.org/rfc/rfc8216.html
- VAST audio support and specifications: https://iabtechlab.com/standards/vast/

## Review status

User approved this design on 2026-10-21 and selected subagent-driven execution. The POC implementation is committed on feat/radio-poc. Actual Kodi playback validation remains pending; see the implementation plan and docs/testing.md.
