This is a source-only copy of the diagnostic harness. The build, generated
media and raw logs remain in the external experiment directory at
`/private/tmp/kodi-audio-validation-20261027/owned-hls-validation/ffmpeg-hls-recovery`.
The following record describes that directory; binary artifacts are not shipped.

# Private FFmpeg 9.0.2 HLS EOF recovery qualification

This directory contains an isolated APFS copy of the existing configured FFmpeg
source and build. No Kodi core, shared FFmpeg source, shared dependency archive,
addon CMake settings, or installed addon was changed by this work.

The final private archive is `build/libavformat/libavformat.a`, SHA256:
`878b90b409c959bdb89f2a6ea1e8f03360a370d1aa9a5be285507456f73bc0e6`.
It is ARM64 and uses the existing macOS 11 deployment target / macOS 26.5 SDK
configuration. Its only replaced archive member is the rebuilt `hls.o`.

## Fix and evidence

[`../../ffmpeg-9.0.2-hls-recovery.patch`](../../ffmpeg-9.0.2-hls-recovery.patch)
(preserved as `artifacts/ffmpeg-9.0.2-hls-recovery.patch` in the external evidence folder) modifies only `libavformat/hls.c`:

1. Count and enforce playlist holds only while the exact next sequence remains
   unavailable. Reset the hold state when that sequence becomes available.
2. Preserve `input_read_done` across reload errors until the next input actually
   opens or swaps in. Clearing it at callback entry causes retry to read the old
   exhausted input and skip the newly available segment.

The test first failed against the original archive: each recovery scenario
stopped at 94 of 189 packets / 96,256 of 193,536 decoded frames. The hold-counter
change alone then resumed at segment 03, skipping 02; that intermediate failure
is preserved in `logs/probe-patched-*.json` and `logs/green-patched.log`.
The final two-change patch passes all cases. No reader reopen, seek, decoder
reset, timestamp offset adjustment, or manual private-state mutation occurs.

Each passing recovery delivers all 189 packets and 193,536 decoded frames with
exact packet PTS/DTS/duration/payload SHA256 sequence and decoded stereo PCM
SHA256 equal to the uninterrupted original-library reference:
`0df4b7a67d16a09d9336bd4d2e700ed4b74595e8a66cb791cc3c455bac489232`.

Cases cover HTTP/1.0, HTTP/1.1 persistent connections and prefetch, nonpersistent
HTTP, two separate hold-exhaustion/recovery cycles, genuine ENDLIST EOF, and
interruption of a held native read. The fixture publishes the exact next segment
only after observing public `av_read_frame` EOF. A ten-second test deadline
allows two complete HLS polling/hold cycles; the interruption case has its own
two-second native deadline. The original six-second repeated-recovery attempt
expired before the second scheduled refresh; this did not relax any media
identity, sequence, or frame assertions.

`make -j4 fate` passed all 203 tests available in the existing configuration.
No external FATE sample corpus was configured; this is not a claim to have run
sample-dependent configurations or platforms.

## Reproduction on this Mac

Run commands from this directory:

```sh
python3 tests/generate_fixture.py
python3 tests/build_probe.py artifacts/libavformat-stock.a artifacts/probe-stock
python3 tests/test_hls_recovery.py artifacts/probe-stock
python3 tests/build_probe.py build/libavformat/libavformat.a artifacts/probe-final
python3 tests/test_hls_recovery.py artifacts/probe-final
```

The stock run must fail the four recovery cases; the final run must pass.
The fixture was generated using Homebrew FFmpeg 9.0.1; the reader and decoder
are statically linked from the copied Kodi FFmpeg 9.0.2 libraries. The original
configured `hls.c` exactly matches the downloaded FFmpeg 9.0.2 release archive.
Media hashes and all original/shared before-and-after hashes are in
`manifest.json`. Each probe's complete compiler/link argv is saved under
`logs/*-link.json`.

To rebuild the private changed member with the original configured flags:

```sh
make -C build V=1 libavformat/hls.o
/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ar r build/libavformat/libavformat.a build/libavformat/hls.o
/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ranlib -D build/libavformat/libavformat.a
```

The copied build's Makefile, source symlink and SRC_PATH refer to the private
source; its install prefix was also redirected defensively. No install target
was run. Other linker dependencies are read-only shared archives. Source build
commands and diagnostics are recorded in `logs/private-hls-final-build.log`;
FATE output is in `logs/fate.log`.

## Scope

This fixes retry of the still-retained exact next HLS sequence after a playlist
hold limit. It does not reconstruct expired/unavailable media. FFmpeg's existing
expired-window and segment-failure skip behavior is unchanged, so gapless callers
must independently detect/refuse those losses. Seek/discontinuity equivalence,
physical speaker timing, and integrated Kodi pause/clock behavior are not proven
by this library regression. The addon must be relinked against this private
archive and captured separately before claiming an integrated playback result.
