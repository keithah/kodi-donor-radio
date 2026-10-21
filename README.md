# Kodi Donor Radio

A GPL-2.0-or-later radio observation POC. Search Radio Browser, browse countries
and tags, and play supported stations through a Python relay embedded in the
addon service. Optional local diagnostics record ICY metadata and explicit HLS
cue evidence from the stream delivered to Kodi. The POC preserves programme
audio and does not insert ads, resolve VAST, or send remote analytics.

**Kodi playback validation is pending.** Kodi 21.3 Omega is the target, with
`xbmc.python` 3.0.1. Packaging, pure-Python integration tests and static checker
results do not establish audible playback or Kodi runtime compatibility. See
[the validation record](docs/testing.md) for evidence and outstanding checks.

## Build and install

The addon requires only Kodi's Python APIs and the Python standard library.
No external proxy, hosted relay, executable, FFmpeg or pip installation is needed
at runtime. The service binds an OS-assigned port on `127.0.0.1` and exposes
opaque session URLs; station URLs are never arbitrary loopback relay parameters.

From the repository root:

```sh
python3 -m pytest -q
python3 tools/package_addon.py --output dist
python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip
```

`pytest` is a development dependency (`python3 -m pip install pytest` in a local
virtual environment if needed). Packaging/checking use the standard library.
The ZIP has one `plugin.audio.kodi_donor_radio/` root. It contains the approved
source, metadata, localization, full GPL v2 license and project-authored geometric
artwork. Tests, development tools, generated audio, caches and secrets are excluded.
Fixed ordering, timestamps and mode 0644 produce byte-identical ZIPs for identical
source, independent of filesystem metadata. New addon source files must be added
to the explicit manifest in `tools/check_package.py`.

Copy the ZIP to the Kodi device. In Kodi, enable **Settings → System → Add-ons →
Unknown sources**, then choose **Add-ons → Install from zip file** and select
`plugin.audio.kodi_donor_radio-0.1.0.zip`. Find **Kodi Donor Radio** under music
add-ons. Search or browse, select a station, and use Kodi's normal playback stop
controls. Keep the bundled service enabled. Changing addon settings restarts the
service and retires existing playback sessions.

## Stream scope

| Input | POC behavior |
| --- | --- |
| Public HTTP/HTTPS continuous MP3/AAC | Bounded streaming relay; codec/demux playback needs Kodi validation |
| ICY metadata | Remove framing without changing audio; observe metadata on the same upstream connection |
| M3U/PLS station indirection | Follow at most three indirections, with loop/size/redirect limits |
| Ordinary clear HLS master/media playlists | Rewrite supported playlist/segment/map references; preserve signed relative URLs and rendition state |
| Explicit HLS CUE-OUT/CONT/IN and DATERANGE evidence | Observe and classify cautiously; receipt time is distinct from audible playback time |
| Encrypted HLS/DRM, low latency HLS, content steering, variable substitution, unsupported URI-bearing extensions | Fail closed; no direct upstream playback fallback |
| Non-HTTP(S), credential-bearing, private/loopback station URLs | Reject; developer fixtures have a separate exact-origin test-mode exception |

A directory entry is a hint, not proof of codec or protocol support. Broken,
geo-restricted, certificate-invalid and unavailable stations can fail independently
of the parser. HEAD and continuous Range are currently rejected; continuous
streams have one consumer and retire on disconnect. Kodi's probing/reopening
behavior still needs runtime validation. Supported HLS syntax is defined by
`resources/lib/hls.py`; in-band SCTE-35/ID3/codec parsing is outside this POC.

## Diagnostics and controlled verification

Diagnostics default off. Enable **Diagnostic logging** in addon settings to write
sanitized JSON lines to
`special://profile/addon_data/plugin.audio.kodi_donor_radio/events.jsonl` and
Kodi's debug logger. Local files rotate at 1 MiB with two backups; evidence fields
are bounded to 2,048 bytes. The service descriptor in that profile is transient
control state and must not be shared. Event logs suppress URL query strings,
credentials and sensitive title/cue fields; inspect any diagnostic export before
sharing it. Radio Browser receives normal directory reads and one non-retried
click report when a listener selects a real station; no click is reported by the
smoke tool or fixture navigation.

Generate playable authored 440 Hz tones and run the developer fixture server:

```sh
python3 tools/generate_audio.py --ffmpeg /opt/homebrew/bin/ffmpeg
python3 tools/fixture_server.py --assets tests/generated-audio --port 8765
```

Use the actual developer FFmpeg path on your system. Generated MP3/AAC/HLS assets
are ignored under `tests/generated-audio/` and excluded from the ZIP. In addon
settings enable test mode, set the exact origin `http://127.0.0.1:8765` without a
trailing slash, and enable diagnostics. Open **Controlled test streams**. Restart
the fixture server between scenarios so its three manifest windows begin fresh.
Compare events with `/expected/{scenario}.json`, and record heard tone audio,
stop/switch/restart behavior and cleanup timings in [docs/testing.md](docs/testing.md).
See [fixture routes and timelines](tools/README-fixtures.md).

For a bounded live read without synthetic click counts:

```sh
python3 tools/smoke_radio_browser.py --limit 3
```

Discovery and each station have a 15-second deadline by default; up to three
HLS manifest levels and one 4 KiB segment sample are read per station. Continuous
samples use the resolver's initial bytes, at most 512 bytes. Output contains UUID,
scheme, protocol and outcome, never upstream URLs or metadata. Exit 0 means every
sample returned bytes, 1 means discovery/reachability/policy/unsupported/empty-sample
failure, and 2 identifies a parser failure. This is no proof of decoded/audible
media, cue coverage, advertisement presence or absence, paid demand, or revenue.

## License and next stages

Project-authored source, tools, tests, synthetic tone generation and the radio
icon/fanart are licensed under GPL-2.0-or-later. The complete GPL version 2 text is
in [LICENSE.txt](plugin.audio.kodi_donor_radio/LICENSE.txt); you may choose any later
GPL version. Artwork is original geometry with no third-party imagery, fonts or
Kodi logo. Radio Browser entries and third-party streams retain their respective
rights; this license does not relicense station content.

Monetization remains the product goal under the user-directed repository-policy
exception assumption. This assumption is not independently verified repository
approval. [The staged roadmap](docs/monetization-roadmap.md) retains VAST/preroll,
HLS replacement, continuous-stream investigation, and listener ad budgets with
explicit decisions and acceptance evidence before each implementation phase.
