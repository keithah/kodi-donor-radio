# Continuous-stream ad replacement: frame-aligned splice design

Status: guarded, default-off, fixture-only implementation. Roadmap section 3 in
[monetization-roadmap.md](monetization-roadmap.md). Evidence is in
[testing.md](testing.md) under "Continuous-stream frame splice".

## Scope and non-goals

The relay already strips ICY framing from Icecast/SHOUTcast-style continuous
streams and logs AdsWizz `adw_ad=true/false` metadata as break observations. This
design adds one thing: at an explicitly signalled break boundary, swap whole
compressed audio frames from a prepared replacement creative for whole source
frames, then rejoin the source at a frame boundary. Everything runs in the
embedded Python relay; there is no decoder, encoder, external executable or
hosted relay.

Not in scope, and still disabled:

- production advertising demand, origins or beacons. The splice path parses no
  tracking URLs, so it cannot fire Impression or quartile events;
- ICY `StreamTitle` heuristics, learned markers, TildeATS or `AD_START` titles as
  splice triggers. They stay observation-only;
- AdsWizz HLS (`adswizzContext` / `ad_*.aac`) replacement. HLS has its own
  segment-level path (roadmap section 2);
- in-band SCTE-35 or ID3 cues in continuous streams;
- transcoding, resampling, gain matching or cross-fading.

## Supported framing and codecs

| Container | Accepted | Rejected (stream stays source-only) |
| --- | --- | --- |
| MP3 | MPEG-1, MPEG-2 and MPEG-2.5 Layer III, any legal bitrate, mono or stereo | Layer I/II, free-format bitrate, reserved sample-rate or bitrate index |
| AAC | ADTS, any object type, 1 or 2 channels, 1–4 raw data blocks per frame | channel configuration 0 (PCE-defined), more than 2 channels, LATM/LOAS, raw AAC, MP4/fMP4 |
| ICY | `icy-metaint` interleaving, metadata split across any TCP read | — |

A frame is accepted only when its header is valid and, while acquiring sync,
the next header at `offset + frame_length` is also valid with the same format.
After lock, every frame must keep the locked format. Bytes that are not part of
a locked frame (an ID3 tag, garbage, a truncated frame) are carried as opaque
"raw" units.

**Format equality is the splice gate.** A creative may replace source frames only
if both have the same codec, MPEG version and layer (MP3) or audio object type
(AAC), sample rate, channel count and samples per frame. Bitrate may differ,
because MP3 and ADTS decoders accept a per-frame bitrate. With equal samples per
frame, replacing source frames one-for-one keeps the output's media duration
identical to the source, so there is no drift, buffering or catch-up. Output
bytes follow the creative's bitrate, so a read can leave as a larger or smaller
write than it arrived.

ADTS carries only a 2-bit profile, so implicitly signalled HE-AAC (SBR) or PS
cannot be distinguished from AAC-LC at the same ADTS rate. A creative must come
from the same encoder profile as the station; this is not detectable from the
headers and is recorded as a limitation.

## Boundary-signal contract

Only AdsWizz ICY metadata is a splice signal, and only its `adw_ad` field:

- `adw_ad='true'` starts a break. `adw_ad='false'` ends it. Values are
  case-insensitive; any other value is ignored.
- The boundary is the audio byte that immediately follows the metadata block in
  the de-framed stream. ICY places metadata only every `icy-metaint` audio bytes,
  so the provider's true ad start can precede the signal by up to one metadata
  interval. That provider-side quantisation is outside the relay's control and
  is recorded, not corrected.
- The splice starts at the first source frame whose first byte is at or after the
  boundary. A frame that straddles the boundary is programme audio. With a
  synchronised source the splice therefore lands within one frame (≤ 26.1 ms for 44.1 kHz MP3, ≤ 21.3 ms for
  48 kHz AAC) after the signalled byte, and never inside a frame. Raw
  (unsynchronised) bytes right after the boundary pass through as programme and
  can push the splice later; `boundary_error_bytes` reports the actual distance.
- A repeated `true` while a break is open, or a `false` with no open break, is
  ignored. Up to 4096 queued signals are kept in order, more than one 64 KiB
  read can carry, so a burst of metadata cannot crowd out a boundary.
- `durationMilliseconds`, when present and valid (1–300000), bounds the break:
  if no `false` arrives within that duration plus 10 seconds, the break is
  closed as unresolved. Without it, the bound is 300 seconds.

## State machine

```
           adw_ad=true at frame          adw_ad=false at frame
 PROGRAMME ───────────────────► REPLACING ──────────────────► PROGRAMME
     ▲            │ no creative /           │ creative exhausted /
     │            │ format mismatch         │ source format change
     │            ▼                         ▼
     └──────── BREAK_SOURCE ◄───────────────┘  (source passes through untouched)
                (adw_ad=false or timeout returns to PROGRAMME)
```

- **PROGRAMME** and **BREAK_SOURCE** forward every source byte unchanged,
  including raw units. Output is byte-identical to the de-framed source.
- **REPLACING** drops each source frame, and any raw unit inside the break, and
  emits the next creative frame in its place.
- A splice fault (source loses sync during a replacement, changes format, or
  the splicer raises) returns to source pass-through for the rest of the session and
  emits `continuous_splice_disabled`. A splicer exception drops at most the one
  pending frame the assembler held.

## Rejoin and decoder state

Splicing at frame boundaries keeps every emitted frame syntactically intact, but
two decoder-state effects remain and are measured rather than hidden:

- **MP3 bit reservoir.** A Layer III frame may borrow up to 511 bytes of main
  data from earlier frames (`main_data_begin`). The first creative frame comes
  from the creative's start, and a creative whose first frame has a non-zero
  `main_data_begin` is rejected as no-fill (`creative_reservoir`). The first source frame
  after rejoin may reference reservoir bytes that were dropped. The decoder then
  cannot reconstruct that one frame, and FFmpeg outputs it as silence or
  concealment. The splicer records `main_data_begin` of the rejoin frame
  (`rejoin_reservoir_bytes`) so this is visible per break.
- **MDCT overlap.** Both codecs overlap-add consecutive frames, so the first
  frame after each splice blends the previous content's tail. This is a
  one-frame transient, not frame corruption.

Measured on the fixtures (offline and in Kodi 22, see testing.md): 0–10 ms of
unclear audio at the splice-in and 40–60 ms at an MP3 rejoin; at most 10 ms at
an AAC rejoin.

## Latency budget

| Path | Added latency |
| --- | --- |
| Splice disabled (default) | none: the existing relay path is unchanged |
| Splice enabled, PROGRAMME | at most one frame held while the next header confirms sync (≤ 26.1 ms MP3, ≤ 21.3 ms AAC at 48 kHz) |
| Boundary decision | at most one frame after the signalled byte, as above |
| Signal versus provider | up to one `icy-metaint` interval, provider-defined and not controllable |

The splicer never buffers ahead of the source and never delays output to wait
for a creative. Pending bytes are bounded at 16 KiB; above that, unsynchronised
bytes are forwarded as raw.

## No-fill and fallback

- The creative is fetched once at session creation, before any break, from the
  exact loopback fixture origin with a 5-second deadline and a 4 MiB byte limit,
  and validated by the same frame parser. Missing, oversized, unparseable,
  multi-format or wrong-format creatives are **no-fill**: the session still
  starts, every break passes the source through (`BREAK_SOURCE`), and
  `continuous_replacement_skipped` records `no_fill` or `format_mismatch`.
- A creative shorter than the break rejoins the source when it runs out
  (`creative_exhausted`); the remainder of the source break is audible. The
  relay does not loop, pad or stretch a creative.
- A creative longer than the break is cut at the frame where `adw_ad=false`
  lands (`truncated`). Programme audio takes priority.
- Each break replays the creative from its first frame.

## Lifecycle and cleanup

The splicer is per-connection state inside the relay worker. Stop, station
switch, restart, a client disconnect and upstream EOF or failure all end the
worker loop. `finish()` then flushes or drops held bytes according to state
(held source bytes are flushed in PROGRAMME/BREAK_SOURCE; inside REPLACING they
are dropped), ends an active replacement with reason `stopped`, and releases
the creative reference. An open pass-through break ends silently. The creative is held in memory only, so no cache files
outlive the session.

## Enablement guard

Splicing is constructed only when all of these hold; otherwise the relay path is
byte-for-byte the existing one:

1. Settings: `test_mode`, `ad_enabled` and the new `ad_continuous` toggle (default
   off), plus a creative URL on the configured fixture origin.
2. The session source is continuous (not HLS), on the configured loopback
   fixture origin, and its station is a built-in `fixture-` station.
3. The source response declares a positive `icy-metaint` and a `Content-Type`
   of `audio/mpeg`, `audio/mp3`, `audio/aac`, `audio/aacp` or `audio/x-aac`;
   otherwise no creative is fetched.

The station check is the `fixture-` identifier prefix, as for the controlled
HLS path; the relay control API that creates sessions is bearer-token gated.

No tracking is fetched or fired. Lifting the fixture-origin restriction needs a
separate decision on demand, rights and beacon timing (roadmap sections 1 and 3).

## Events

All events use the existing sanitised event sink and carry no URLs, titles or
AdsWizz context:

- `continuous_replacement_start`: `signal_offset`, `splice_offset`,
  `boundary_error_bytes`, `start_sample`, `codec`, `sample_rate`.
- `continuous_replacement_end`: `reason` (`truncated` when `adw_ad=false`
  cuts the creative, `creative_exhausted`, `timeout`, `format_change`,
  `sync_lost`, `splicer_error`, `stopped`), `frames_replaced`, `samples_replaced`, `rejoin_offset`,
  `rejoin_sample` and, for MP3, `rejoin_reservoir_bytes`. `stopped` covers stop,
  switch, client disconnect and upstream EOF or failure; it has no rejoin.
- `continuous_replacement_skipped`: `reason` (`no_fill`, `format_mismatch`,
  `creative_unparseable`, `creative_too_large`, `creative_mixed_format`,
  `creative_reservoir`).
- `continuous_break_unresolved`: a pass-through break reached its bound.
- `continuous_splice_disabled`: `reason` (`unsupported_codec`,
  `format_change`, `sync_lost`, `splicer_error`).
