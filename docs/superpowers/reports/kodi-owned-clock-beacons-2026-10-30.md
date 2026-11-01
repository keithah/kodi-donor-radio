# Owned clock and loopback beacons — 2026-10-30 capture

**Controlled midroll correlation observed in this fixture. Full HLS runtime acceptance: FAILED. Production success beacons: DISABLED.**

This was a captured Kodi process-audio run using Kodi master revision
`20261026-8091c14861`, the extended local HLS fixture, and the local synthetic
VAST tag. It does not test a public station, production VAST inventory, or
physical speaker output.

## Captured ad interval

The lossless recorder captured 360.085 seconds at 48 kHz stereo, with
17,284,096 frames, zero recorder timestamp gaps, and zero reported recorder
discontinuities. The controlled midroll replacement tone was identified at
880 Hz from **167.4–171.4 seconds** of capture time. The loopback VAST server
received one each of impression, start, first quartile, midpoint, third
quartile, and complete:

| Event | Receipt time from capture start |
| --- | ---: |
| Impression | 167.646 s |
| Start | 167.646 s |
| First quartile | 168.511 s |
| Midpoint | 169.645 s |
| Third quartile | 170.512 s |
| Complete | 171.597 s |

These local receipts track the controlled replacement tone. Completion arrived
about 0.20 seconds after the detected tone ended. This is evidence for the
fixture path only; no request was sent to a production tracking endpoint.

## Full-run failure

The captured output becomes silent at **283.6 seconds** and remains silent to
the end of the 360-second recording. The player clock stayed at **260.408
seconds** for **119.665 seconds** of continued observation. The clock analyzer
reports one such plateau with 76.278 seconds of captured silence. At the final
observation Kodi still reported an active, unpaused player with caching enabled;
its reported duration was 404.468 seconds. A manual Stop was required to leave
playback. Natural EOF was not observed.

The generated source has 128 segments of 2.005333 seconds each (256.683 seconds
of programme), plus a four-second preroll, so the observed 260.408-second
position is near the end of this bounded fixture. Its rolling playlist adds
`EXT-X-ENDLIST` only in the final window. Kodi's log reports a file-cache EOF
followed by `CVideoPlayerAudio::Process - stream stalled`; the source segment
127 was requested. This points to final-window/EOF handling in the controlled
fixture path. The exact composed playlist bytes delivered at that point were
not retained, and this run does not show that a public radio stream stalls.

Root cause (log review, 2026-11-01): the playlist and relay behaved as designed.
The relay sent every composed segment through source 127, then closed the
`Connection: close` response; `CFileCache` logged `Hit eof` at 21:48:04 local.
Playback used **stock** `inputstream.ffmpegdirect` 22.2.10 in `timeshift` mode
(`Num Props: 2`, no `terminal_eof` support in the installed addon). Its timeshift
reader read its last segment (id 21) at 21:48:07 and never reported EOF, so
VideoPlayer logged `stream stalled` at 21:48:20 and stayed in `caching state 1`
until the manual Stop at 21:50:44. The add-on now sets
`inputstream.ffmpegdirect.terminal_eof=true` on this buffered route; natural EOF
still requires the `timeshift-startup-eof` FFmpegDirect patch (or an upstream
equivalent) and a fresh capture to confirm.

The capture timing sidecar itself is clean, but that only says the recorder did
not lose timestamps. It does not make the silent audio acceptable. The RPC
observer collected 1,439 usable clock samples out of 1,474; its item-match flag
is not independent exact-session proof. The analyzer therefore keeps
`mapping_proven=false`. The stalled clock, long silent tail, and missing natural
EOF fail the full playback gate despite the successful fixture midroll.

## Production status and evidence

Production beacons remain disabled. The shipped addon still requires its
explicit test mode for ad insertion and the test VAST resolver/tracker accepts
the configured loopback fixture origin only. No production VAST tag was
provided, and this run did not contact any production host. Enabling production
tracking from these results would exceed the verified route and would not clear
the failed no-gap/EOF acceptance gate.

Kodi was stopped after evidence collection. No Kodi core change was made, and
unrelated jobs were left running. The recording and derived evidence remain
outside the repository at:

`/private/tmp/kodi-audio-validation-20261029/run-21-full-window/Kodi-2026-10-30T04-43-37Z-05F09C04-4747-448A-8973-3FFA046882E2/`

Key artifacts are `audio.caf`, `capture.json`, `measured-audio.json`,
`fixture-stats.json`, `kodi-clock-observation.json`, and
`kodi-end-observation.json`. The audio SHA-256 is
`6a7eef1a6eb169c180bbcafffe65d53ff3f3632c76c31e0c4e9b3c3d6eb8d3a1`.
