# Owned clock and loopback beacons — 2026-10-29 checkpoint

**Runtime acceptance: FAILED. Steward: NOT RUN. CodeRabbit: NOT RUN. Push: BLOCKED.**
Earlier task reviews and test passes do not satisfy those current review gates.
No production tracking or release acceptance is claimed.

## Implemented scope

Candidate `c33f6d020bb60c24073e21f762ea7b0048db089d`, built in Release mode,
uses the experimental owned renderer with a session-bound native clock and the
`videoplayer_video_v1` control path. It requires experimental FFmpegDirect addon
patches and is separate from packaged station playback. It plays the immutable, precomposed
180.032-second AAC/fMP4 fixture and uses the real tracker and HTTP beacon worker
against loopback fixture endpoints. This tests existing creative intervals and
tracking, not arbitrary VAST creative splicing or monetization of public stations.

The owned path can start preroll only from a real qualified near-start sample
with verified source zero. Run19 delivered its six preroll events. The legacy
continuous adapter still suppresses zero-start preroll success beacons; no zero
sample is injected or replayed to bypass that rule. Its earlier pause omission
remains documented in [the continuous-path investigation](../../testing-continuous-clock.md).

Kodi's normal pause controls remain the required interface. The Kodi runtime,
signed recorder, source fixture, and unrelated jobs were kept unchanged during
these comparisons; no Kodi-core audio/pause fix was introduced. A proposed HAL
output design is future work, not implemented or tested by this checkpoint.

## Run19: complete recording, failed audio gate

The diagnostic capture completed with **11,044,352 stereo frames at 48 kHz** and
**zero recorder discontinuities**. This is captured Kodi process audio, not a
measurement at the physical speakers. Native evidence contains 10,402 clock rows,
all **8,641,536 decoded and accepted source frames**, natural source EOF, an empty
queue, and no native failure. Four normal pause/resume cycles and the final stop
callback were recorded.

There were **34 tracking decisions and 34 actual HTTP204 receipts**, matching the
fixed event set: six each for preroll and clean A/C/D/E, four for interrupted B.
B did not backfill third-quartile or completion events. Counts alone do not prove
correct audible timing; a full clock/beacon timing audit was not started after
the waveform failure was established.

The recording has **929 consecutive exact stereo-zero frames**, or
**19.354166667 ms**, at capture **91.072062500–91.091416667 seconds**, before the
third pause callback. Both channels' sparse independent source matches show a
**+1,024-sample / 21.333333 ms output-time step** across it. This is an unexpected
gap; sparse matching alone does not prove every source sample was preserved at
the boundary. Later exact-zero intervals in this recording lie inside the
requested third pause.

Only native row **4469** queries inside the gap, and its runtime snapshot is
invalid/acquiring with no mapped position. Row 4467 had already reset for
`anchor_innovation`; qualified trigger row 4466 was sampled before the silence.
That conservative behavior does not erase the audio failure. More seriously,
the earlier **run14 row 5828 was runtime-qualified while its query and surrounding
1 ms of captured stereo audio were entirely silent**. Cached AudioEngine
accepted/cache/delay accounting is therefore not unconditional evidence of an
audibly presented position, even when surrounding samples match closely.

The estimator and acceptance limits remain unchanged: **1,024/48,000 seconds
(21.333333 ms)** clock error, **100 ms** freshness, **5 ms** maximum query width,
and acquisition from nine observations spanning at least 120 ms. Silence is not
assigned an interpolated source position. No clock offset was fitted, estimator
replayed, or threshold relaxed to obtain these findings.

## Recovered trace: supported observation, unresolved cause

An independent XML crosscheck resolved schema IDs, thread/process identities,
and context-switch boundaries. The recovered time-info gives Mach epoch
`94185449167378` ticks at `125/3` ns per tick. This independently places the
helper trigger at **228.343374333 ms** and captured zero onset at
**258.742416333 ms** relative to that epoch; no import wall time or fitted offset
was used.

| Kodi thread | Observed Runnable interval | Duration | Scheduler priority |
| --- | --- | --- | --- |
| ActiveAE | 180.149000–235.206083 ms | 55.057083 ms | 31 |
| AESink | 182.183625–236.724208 ms | 54.540583 ms | 31 |

These are nonzero events corroborated by Runnable/Running context switches, not
synthetic initial states. Both intervals overlap the trigger and end roughly
22–24 ms before captured silence. The IO thread continues near its nominal
10.667 ms cadence through the waits, but a later interval is **13.077750 ms**;
uninterrupted cadence throughout the slice is not established.

Actual retained nonzero scheduler events cover only **0.669375–274.395250 ms**.
They cover gap onset but end before its **278.096583333 ms** endpoint. A requested
five-second window is not proof of five seconds retained. Initial worker metadata
establishes identity only. The recovered state shows runnable scheduling delay,
not those workers blocked on a particular mutex. Scheduler policy, competing
load, and instrumentation effects remain unresolved; no particular job, guard
call, or lock is established as the cause. Diagnostic trace data cannot substitute
for two clean, uninstrumented acceptance recordings.

## Provenance and remaining gates

The compact identities below identify the tested artifacts, not a review pass.

| Artifact | SHA256 |
| --- | --- |
| Release native addon | `30ccd2f091b714e99faa1cedfb0e95678be77c937e9521d02cda980c2c542726` |
| Frozen estimator | `7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87` |
| Reviewed fractional evaluator | `c7d6f931e2aaba1f686dd3b2108df3fa99d289555af4e7308b5a719fd6647198` |
| Decoded fixture reference | `0b7f508c5534424c42d7b70231e664f208cb48bd0934d166b5949f5549c16524` |

All 35 installed identity hashes matched during the run19 audit. Original
recordings, failed runs, raw-row hashes, and trace exports remain local in the
`owned-clock-beacons-validation` evidence directory. These filenames are local
artifact references, not portable public links:

- `run-19-trigger/independent-trigger-quick-triage-v1.{json,md}` and
  `independent-trigger-gap-detail-v1.json`: waveform, original queries, controls,
  receipts, completion, and installed identities.
- `run-19-trigger/independent-trace-timing-crosscheck.md` and
  `independent-trace-timing-crosscheck-v1.json`: XML identities, exact conversion,
  boundaries, retained range, limitations, and source hashes.
- `run-14/gap-clock-audit-v1.{json,md}`: the qualified query inside silence.

Raw audio, traces, host environment dumps, and private session transport data are
not included in this repository checkpoint. Remaining work is an addon-only fix,
two complete clean captures with full source/seam/clock/beacon audits under the
frozen criteria, and fresh Steward primary/adversarial and CodeRabbit reviews of
the final candidate. **Neither reviewer has run on this checkpoint; pushing
remains blocked.**
