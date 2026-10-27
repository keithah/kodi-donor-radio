# Kodi pause-boundary design proposal

Status: deferred, not approved. The user requested continued investigation of
supported addon and player paths before considering any Kodi-core change.
No core edits or build are authorized by this proposal.

The tested playback paths lose audio around pause. No supported addon-level
remedy has yet been verified for preserving the queued samples.
Captured speech alignment shows about 214 ms of missing content; ActiveAE's
single-stream pause flush releases pending output and then inserts synchronization
silence on resume. Removing the flush or using the existing stream drain alone
would not coordinate the already-frozen player clock with hardware output.

## Proposed scope

A separate Kodi-core change, not part of the radio addon or its ZIP:

1. Define a bounded pause request/acknowledgement between VideoPlayer's audio
   worker, its sink adapter and ActiveAE. Stop admitting new audio at a known
   boundary while preserving queued samples.
2. Let the clock advance with already-admitted audio until the sink confirms
   the last audible boundary; then freeze clock and demux. The existing stream
   drain is not a device-output acknowledgement and cannot be substituted alone.
3. Resume from that exact boundary. Seek, stop, device changes and failed sinks
   must cancel the pending pause safely. Concurrent GUI sounds must remain valid.
4. Keep platforms/backends without the new contract on their existing behavior
   until their own sink semantics are tested. Do not claim preservation there.

This can add a small bounded pause response delay equal to already queued audio,
while preventing lost or duplicated content. The API shape should be reviewed
against Kodi's threading and sink lifetime rules before coding.

## Acceptance

First add a deterministic fake sink consuming numbered 48 kHz PCM samples.
Check that pause acknowledgement identifies the audible tail, each sample appears
exactly once after resume, no silence is synthesized, and the clock freezes at
that same boundary. Cover empty/nonempty queues, repeated pauses, seek/stop during
pending pause and GUI audio. Then build isolated Kodi and capture 1/3/10-second
pauses on the same Mac; correlate decoded speech and sample markers with source,
clock and pause acknowledgements. Keep USB work and the approved recorder intact.

A full configured core build is needed; the current tested Kodi application has
no available reusable object cache. No core implementation is included in this
proposal, and no resume fix is claimed yet. The independent addon work can enable
qualified clean-break fixture beacons while suppressing pause-interrupted ads.
