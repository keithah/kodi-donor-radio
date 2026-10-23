# Kodi Radio POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver an installable Kodi radio addon that discovers stations, relays supported playback through embedded Python, and accurately logs stream cues, establishing the foundation for monetization.

**Architecture:** A music plugin registers playback sessions with a bundled Kodi service. The service relays the exact upstream stream consumed by Kodi and produces normalized observation events. Pure-Python parsing and transport modules are tested independently of Kodi, then exercised with controlled streams and actual Kodi playback.

**Tech Stack:** Python 3 standard library at runtime; Kodi Python APIs at the integration boundary; pytest for development tests; FFmpeg only as a development fixture-generation tool, never an addon runtime dependency.

**Spec:** [Approved design](../specs/2026-10-21-kodi-radio-design.md).

## Global Constraints

- “Use an open-source Python relay packaged within the addon and executed by Kodi.”
- “No separately installed proxy, executable, or hosted media relay is required.”
- “The service binds an OS-assigned port on 127.0.0.1.”
- “Use Python 3 and current supported Kodi APIs, including xbmcvfs.translatePath for special paths.”
- “Use GPL-2.0-or-later for project-authored code unless the owner chooses otherwise; dependencies and assets must be compatible.”
- “No remote analytics in the POC.”
- “Diagnostic logging is opt-in, bounded, and excludes secrets.”
- “Seeing no cues means no cues observed, never proof that no ads exist.”
- “A real Kodi playback smoke test is required to claim Kodi runtime compatibility; pure-Python tests alone are insufficient.”
- Preserve the user-directed monetization exception assumption. This plan implements the POC, not commercial ad insertion; the roadmap below retains that objective.
- Use lower-case distributable paths except required LICENSE.txt. Localize all user-visible strings. Keep developer tooling and tests outside the addon ZIP.

## Review Focus

1. DNS rebinding or redirects from a public station to a local address must not turn the relay into a private-network fetcher (Task 2).
2. A slow or disconnected Kodi client must not create unbounded buffering or leave upstream connections running (Task 6).
3. Concurrent HLS variant requests must not mix cue state across renditions or lose relative signed URLs (Tasks 3 and 6).
4. Station switching and service restart must not leave playable stale session URLs or overwrite a newer service descriptor (Task 7).
5. Malformed titles/cue attributes containing credentials or URL tokens must not leak through otherwise sanitized structured logs (Task 1).

## Scope and file map

The repository currently contains only the approved spec. Create `plugin.audio.kodi_donor_radio/` as the distributable root. Below, `A/` means this exact directory; `L/` means `plugin.audio.kodi_donor_radio/resources/lib/`. Tests add `L/` to sys.path using `tests/conftest.py`; no Kodi stubs leak into production modules.

| Files | Responsibility |
| --- | --- |
| `L/models.py`, `L/events.py` | Shared records, event sanitization and bounded logging |
| `L/http_client.py`, `L/url_policy.py` | Pinned-address HTTP(S), redirects, limits and cancellation |
| `L/radio_browser.py`, `L/station_resolver.py` | Directory operations and M3U/PLS indirection |
| `L/hls.py`, `L/cues.py`, `L/icy.py` | Playlist URI rewriting, cue state and ICY framing |
| `L/sessions.py`, `L/relay.py`, `L/control.py` | Session resources, loopback media routes and authenticated control |
| `L/kodi_runtime.py`, `A/default.py`, `A/service.py` | Kodi UI, lifecycle and settings adapters |
| `A/addon.xml`, `A/resources/settings.xml`, `A/resources/language/resource.language.en_gb/strings.po` | Kodi metadata and localized configuration |
| `A/LICENSE.txt`, `A/resources/icon.png`, `A/resources/fanart.jpg` | License and project-authored artwork |
| `tests/`, `tools/fixture_server.py`, `tools/generate_audio.py` | Unit/integration tests and controlled audio sources |
| `tools/package_addon.py`, `tools/check_package.py`, `tools/smoke_radio_browser.py` | Reproducible packaging and live verification |
| `README.md`, `docs/testing.md`, `docs/monetization-roadmap.md` | Installation, support matrix, validation record and next stages |

Implementation choices: use a Python 3.8-compatible syntax subset until the target stable Kodi runtime is verified in Task 7. Freeze the exact tested Kodi version there. Use stdlib dataclasses; no runtime pip installs. Initial version is `0.1.0`.

Shared limits: 5-second connect and 10-second read timeout, 5 redirects, 3 playlist-indirection levels, 1 MiB playlist/directory response cap, 64 KiB media chunks, 2,048-byte sanitized evidence cap. One active playback session; at most 8 simultaneous media requests, 2,048 registered resources and 4,096 deduplication keys per session. Idle sessions expire after 120 seconds. Event logs rotate at 1 MiB with two backups. These are POC defaults, not ad-serving policy.

## Task 1: Event records and safe diagnostics

**Files:** Create `L/models.py`, `L/events.py`, `tests/conftest.py`, `tests/test_events.py`, `pyproject.toml`, `.gitignore`.

**Interfaces:** Produce frozen `Station(uuid, name, url, codec, hls)`; `Event(kind, session_id, station_uuid, protocol, source, evidence, observed_at, monotonic_at, details)` with schema version 1 on serialization; `EventSink.emit(event: Event) -> None`; `sanitize(value: object) -> object`. Details holds optional cue ID, duration, media sequence, discontinuity/rendition context, media position, classification and reason. Missing fields are omitted. Clock callables are injected into detectors.

- [x] Write `test_unknown_timing_is_omitted`, asserting no `media_position` or `duration` is invented; `test_log_redacts_nested_credentials`, asserting userinfo, query values, Authorization/cookie values and URL fragments never appear in serialized nested metadata; and `test_rotation_is_bounded`, asserting no more than three log files and capped evidence.
- [x] Run `python3 -m pytest tests/test_events.py -q`; confirm failure due to missing project modules, not broken test infrastructure.
- [x] Implement records and `JsonEventSink(profile: Path, enabled: bool, debug_log: Callable[[str], None])`. Use whitelisted structured fields; replace unstructured URLs and credential-bearing fields before truncation. Disabled sinks perform no filesystem writes. Add test/development configuration and ignore generated artifacts.
- [x] Run `python3 -m pytest tests/test_events.py -q`; require all tests pass.
- [x] Commit as `feat: add normalized stream events and bounded diagnostics`.

## Task 2: Bounded transport and Radio Browser discovery

**Files:** Create `L/url_policy.py`, `L/http_client.py`, `L/radio_browser.py`, `L/station_resolver.py`, `tests/test_transport.py`, `tests/test_radio_browser.py`.

**Interfaces:** Produce `HttpClient.open(url: str, headers: dict, cancel: threading.Event) -> Response`; Response is a context manager exposing `status`, `headers`, `final_url`, `read(size: int) -> bytes`, and `close()`. `UrlPolicy(test_origins: frozenset[str])` checks every destination. `RadioBrowser.search(name: str, country: str, tag: str, offset: int, limit: int = 50) -> list[Station]`, `.countries() -> list[str]`, `.tags(offset: int, limit: int = 50) -> list[str]`, `.click(uuid: str) -> None`. `resolve_station(url: str, client: HttpClient, cancel: Event) -> ResolvedSource` returns final URL and protocol; any probed continuous response is transferred to the session, not discarded and reopened.

- [x] Write transport tests asserting file URLs, private IPv4/IPv6 and redirected private targets are rejected; test a resolver changing from public to private between validation and connection, asserting the connected address is the validated address. Test TLS hostname verification remains tied to the original hostname, redirect credential stripping, timeout, oversized response and cancellation.
- [x] Write directory tests asserting mirror failover, descriptive `kodi-donor-radio/0.1.0` User-Agent, `limit=50`, increasing offsets, Unicode search encoding, empty results, malformed result isolation, and click counted only once per selection without automatic retries after uncertain delivery. Test M3U/PLS recursion limit, loops and MIME/content sniffing.
- [x] Run `python3 -m pytest tests/test_transport.py tests/test_radio_browser.py -q`; confirm expected failures.
- [x] Implement stdlib HTTP transport with validated pinned socket destinations, correct HTTPS SNI/hostname verification and deterministic cleanup. Discover Radio Browser mirrors through its documented DNS discovery; verify the current documented endpoints during implementation. Use injected mirror lookup and transport in unit tests. Resolve supported station indirection without downloading entire media bodies.
- [x] Run the two test files; require all pass, including an assertion that a continuous probe's response is reused.
- [x] Commit as `feat: add bounded station discovery and stream resolution`.

## Task 3: HLS parsing, URI mapping and timeline observations

**Files:** Create `L/hls.py`, `tests/test_hls.py`, `tests/fixtures/hls/` text playlists.

**Interfaces:** Consume Response and limits from Task 2. Produce `parse_playlist(text: str, final_url: str) -> Playlist` and `rewrite_playlist(playlist: Playlist, register: Callable[[str], str]) -> str`. Playlist contains source lines, URI references, media/discontinuity sequence, target duration, segment durations, and `CueObservation(source, attributes, sequence, discontinuity, media_position)` records. Media positions are relative only when derivable; do not manufacture absolute timestamps.

- [x] Write `test_rewrites_every_supported_uri`, asserting segment lines and URI attributes in MEDIA, MAP, and I-FRAME-STREAM-INF resolve against the redirected final URL and map to opaque routes. Include quoted commas, escaped values, root-relative URLs and signed relative URLs.
- [x] Write tests preserving byte-range tags, program-date-time, discontinuities and unchanged non-URI lines. Reject encryption, LL-HLS parts/preload/server-control, malformed playlists, oversized input and unsupported URI-bearing tags rather than leaking direct upstream references. Assert simultaneous renditions remain distinct.
- [x] Run `python3 -m pytest tests/test_hls.py -q`; confirm failures.
- [x] Implement parsers with explicit supported tags and attribute grammar. Return cue observations without interpreting advertisements. Preserve segment sequence/timeline context for the state machine and media bytes for the relay.
- [x] Run `python3 -m pytest tests/test_hls.py -q`; require all pass.
- [x] Commit as `feat: parse and rewrite observable HLS playlists`.

## Task 4: Cue lifecycle and deduplication

**Files:** Create `L/cues.py`, `tests/test_cues.py`; extend `tests/fixtures/hls/`.

**Interfaces:** Consume Event, EventSink and CueObservation. Produce `CueTracker(session_id: str, station_uuid: str, sink: EventSink, utc_now: Callable, monotonic_now: Callable)`, `.observe(playlist: Playlist, rendition_id: str, epoch: int) -> None`, and `.finish(reason: str) -> None`. Trackers serialize state updates under a lock. Rendition identity is included in event details; cross-rendition observations are not falsely merged into one inferred break.

- [x] Write tests for cue-out/in and SCTE35-OUT/IN DATERANGE updates, asserting one start/end per rendition and epoch despite repeated windows, updated attributes emit only meaningful updates, and malformed encoded payloads produce invalid cue observations rather than confirmed starts. SCTE35-CMD alone remains opaque evidence unless decoded semantics are implemented and tested.
- [x] Write `test_join_mid_break_does_not_invent_start`, `test_missing_end_is_unresolved`, `test_reused_id_after_epoch_is_distinct` and negative tests for generic date ranges, title changes and bare discontinuities. Assert expiry is unresolved with a reason, never an explicit end. A live playlist containing an old cue does not imply the listener heard that boundary.
- [x] Run `python3 -m pytest tests/test_cues.py -q`; confirm failures.
- [x] Implement bounded deduplication using rendition, epoch, stable source identity and normalized payload. Track contiguous media positions across overlapping windows; mark timing unknown across gaps. Detect starts only for explicit supported cue semantics. A repeated discontinuity in overlapping playlists must not create a fresh epoch each fetch.
- [x] Run cue and HLS tests; require all pass and verify all six event kinds have exercised semantics.
- [x] Commit as `feat: track explicit break cues without duplicate events`.

## Task 5: Continuous-stream ICY framing

**Files:** Create `L/icy.py`, `tests/test_icy.py`.

**Interfaces:** Produce `IcyDecoder(metaint: int, on_metadata: Callable[[dict], None])`, `.feed(chunk: bytes) -> bytes`, `.finish() -> None`. Input is upstream framed data; output is audio only. The relay requests `Icy-MetaData: 1` upstream and consistently strips metadata downstream, omitting icy-metaint from the downstream headers. Metadata is emitted through EventSink as `metadata_observed`.

- [x] Write split-point tests feeding the same framed stream at every possible boundary; assert output equals original audio and metadata is emitted exactly once per received block. Cover zero-length blocks, invalid metaint, invalid encoding, truncated blocks and maximum 255 × 16-byte metadata blocks.
- [x] Add a title containing “advertisement” and assert no break_start event is emitted.
- [x] Run `python3 -m pytest tests/test_icy.py -q`; confirm failures.
- [x] Implement incremental bounded framing with partial-state storage; treat no metaint as plain audio at the relay level. Never advertise the upstream framed Content-Length after stripping metadata.
- [x] Run ICY tests; require all pass.
- [x] Commit as `feat: inspect ICY metadata without altering audio`.

## Task 6: Session relay and end-to-end controlled sources

**Files:** Create `L/sessions.py`, `L/relay.py`, `L/control.py`, `tools/fixture_server.py`, `tools/generate_audio.py`, `tests/test_relay.py`, `tests/test_sessions.py`.

**Interfaces:** Produce `SessionManager.create(station: Station) -> PlaybackSession`, `.stop(session_id: str) -> None`, `.close() -> None`; PlaybackSession exposes `id` and `playback_url`. `RelayServer.start() -> ServiceDescriptor` and `.close() -> None`; descriptor contains port, instance ID and random control token. Control routes require the token: POST `/sessions` registers a station and DELETE `/sessions/{id}` stops it. Media uses random session/resource IDs; no route accepts an arbitrary query URL. Expose a health route without sensitive data.

- [x] Write integration tests with an in-process fixture origin: assert a continuous session has one upstream connection and byte-identical downstream audio; an HLS client receives precisely the playlist version inspected; segment hashes match; range requests preserve 206/Content-Range and valid 416 responses. Assert upstream failures do not become misleading 200 responses.
- [x] Add tests for forged session/resource IDs, unauthorized control, excessive resource registration, slow consumers, disconnected consumers, worker limits, cancellation and concurrent rendition requests. Assert resource count stays within limits as rolling playlists advance and active requests are not evicted mid-transfer. POST creation replaces the previous session deterministically.
- [x] Run `python3 -m pytest tests/test_relay.py tests/test_sessions.py -q`; confirm failures.
- [x] Implement the bounded threaded loopback server, close-delimited continuous output and media streaming backpressure. Preserve supported headers and reject unsupported protocol transformations. Wire HLS observations and ICY metadata to the same-session sink before forwarding their corresponding content. Active network reads end by timeout or cancellation; shutdown joins finish within 12 seconds.
- [x] Implement deterministic fixture schedules (normal breaks, mid-break entry, missing end, malformed cue and no-cue), connection counters and expected JSON events. Generate short MP3/AAC/HLS audio from a synthetic tone with the developer's FFmpeg; document regeneration and keep generated fixture assets outside the addon ZIP. Unit tests use small framing bytes; real Kodi tests use playable generated audio.
- [x] Run relay/session tests plus all parser tests; require pass. Run `python3 tools/fixture_server.py --help` and verify fixture URLs and scenario selection are documented.
- [x] Commit as `feat: relay observed radio streams through managed sessions`.

## Task 7: Kodi navigation and service lifecycle

**Files:** Create `A/default.py`, `A/service.py`, `L/kodi_runtime.py`, `A/addon.xml`, `A/resources/settings.xml`, `A/resources/language/resource.language.en_gb/strings.po`, `tests/test_kodi_runtime.py`.

**Interfaces:** Consume RadioBrowser, SessionManager, RelayServer and ServiceDescriptor. Produce `run_plugin(argv: list[str]) -> None` and `run_service() -> None`. Kodi-specific imports live here or in entry points. The service owns the relay; the plugin reads an atomically published descriptor and uses authenticated control requests to resolve playback to the loopback URL.

- [x] Verify the current stable Kodi release and its Python addon metadata/API requirements using official sources; record the precise minimum dependency and tested runtime in addon.xml and docs/testing.md. Use music plugin and service extension points within the same package.
- [x] Write adapter tests for search/country/tag pagination, selection-to-resolved-URL flow, single click reporting, unavailable service, startup timeout, cancellation and localized error text. Assert no playback fallback bypasses the relay.
- [x] Write lifecycle tests asserting rapid station switches stop old streams, stale tokens fail after restart, an old service cannot delete a newer descriptor, debug logging is opt-in and test-stream navigation is absent by default. Test profile permissions where supported.
- [x] Run `python3 -m pytest tests/test_kodi_runtime.py -q`; confirm failures using narrow fake Kodi modules supplied only by tests.
- [x] Implement navigation, settings and lifecycle. Default settings: diagnostics disabled, test mode disabled; test fixture origin is accepted only in test mode and scoped to its exact loopback port. Use xbmc.Monitor abort handling, player stop/end/error callbacks and idle expiry for cleanup. Publish descriptors atomically and remove only the matching instance on exit.
- [x] Run adapter and relay tests; require all pass and XML parses. Check all displayed labels/errors map to strings.po entries.
- [x] Commit as `feat: integrate Kodi radio navigation and embedded service`.

## Task 8: Package, runtime validation and monetization handoff

**Files:** Create `A/LICENSE.txt`, `A/resources/icon.png`, `A/resources/fanart.jpg`, `tools/package_addon.py`, `tools/check_package.py`, `tools/smoke_radio_browser.py`, `README.md`, `docs/testing.md`, `docs/monetization-roadmap.md`, `tests/test_package.py`.

**Interfaces:** `python3 tools/package_addon.py --output dist` writes `dist/plugin.audio.kodi_donor_radio-0.1.0.zip`. `python3 tools/check_package.py <zip>` exits zero only when source, valid XML, localization, license, artwork dimensions and allowed contents are present. `python3 tools/smoke_radio_browser.py --limit 3` performs bounded discovery/stream checks without requiring ad cues or counting synthetic clicks.

- [x] Write package tests asserting a single addon root, both entry points, correct metadata, no tests/fixture audio/pycache/secrets, and no executable permissions. Run `python3 -m pytest tests/test_package.py -q`; confirm failure before implementing packaging.
- [x] Implement deterministic ZIP creation/checking; add the full GPL-2.0 license text and project-authored 512×512 opaque icon plus 1280×720 JPEG fanart. Keep artwork simple and verify dimensions; no licensed third-party imagery. Document install, supported/unsupported streams, diagnostics location, fixture setup and limits.
- [x] Run `python3 -m pytest -q`, package creation and package checks; require all pass. Run the official Kodi addon checker using its documented current invocation if available; record its exact version, command and results. Do not claim checker validation if it could not run.
- [x] Run the bounded live discovery smoke command; record date, sampled station UUIDs, protocols and outcomes without secret URLs. Reachability failures are reported separately from parser failures. Do not infer advertisement coverage from this sample.
- [x] Record partial Kodi 21.3 runtime playback with the process-local USB workaround; search, live playback, controlled ICY/HLS, stop and station switching are documented in docs/testing.md.
- [ ] Complete Kodi acceptance by exercising the ZIP-install dialog and settings-dialog restart, and confirming audible output. Pure-Python tests do not replace this check.
- [x] Write monetization-roadmap.md with sequential follow-on milestones: VAST audio/preroll and tracking; HLS break replacement/fallback with codec and duration compatibility; continuous-stream replacement investigation; listener ad budget and buffering policy. Each milestone lists its required acceptance evidence and decisions below.
- [x] Commit as `build: package radio POC and document validation`.

## Follow-on monetization decisions and acceptance evidence

These are separate implementation plans after the observation POC, not hidden work in Task 8. No VAST tag is required to implement this plan.

1. **VAST/preroll:** choose demand endpoint, supported VAST version/media, wrapper limit, consent inputs, creative expiry and provider caching rules. Test wrappers, no-fill, timeouts and malformed creatives using local VAST fixtures. Tracking must follow actual playback, never fetch/cache; cancellation must not emit completion.
2. **HLS replacement:** define supported source/creative codec combinations, break under/overrun and rejoin behavior. Prove boundaries with known audio and player buffering; preserve original stream on no-fill or incompatible media. Demonstrate no duplicate impressions across retries. In-band cues and continuous streams remain explicitly separate coverage.
3. **Listener ad budget:** settle whether extra advertising delays playback, skips content or uses catch-up. Specify maximum buffered duration/storage, cancellation and crash recovery; distinguish cached ads from cached programme audio. Verify budgets against actual played ads, with no revenue assumption for unavailable demand.

## Plan review and execution handoff

Coverage review: Tasks 1–2 cover diagnostics/discovery/transport; Tasks 3–6 cover same-stream parsing, cue semantics and controlled fixtures; Tasks 7–8 cover Kodi packaging and runtime evidence. Future monetization is retained as staged work with explicit entry decisions. All five Review Focus items have tests in their owning tasks. Product implementation is committed on feat/radio-poc. Kodi 21.3 playback activity was later recorded with the process-local USB workaround; ZIP installation, settings-dialog restart and audible output remain unverified.

Execution selected by user: subagent-driven. All eight task reviews passed; final review fixes added zero-offset range compatibility, safe HLS suffix routing, late cue-in association after inferred expiry, normalized HTTP errors and ISO timestamp fractions. Real FFmpeg fixture playback passed for MP3, ICY and HLS. Partial Kodi 21.3 playback activity is documented in docs/testing.md; full acceptance remains pending for the ZIP-install dialog, settings-dialog restart and audible output.
