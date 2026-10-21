# Validation record

## Runtime target and requirements

Target: **Kodi 21.3 Omega**, the latest stable release found in the official
[release directory](https://mirrors.kodi.tv/releases/windows/win64/) on
2026-10-21. Kodi 22.0 Piers RC1 is a release candidate, not the stable target.
The minimum declared dependency is **`xbmc.python` 3.0.1**, as supplied by the
[21.3 source metadata](https://github.com/xbmc/xbmc/blob/21.3-Omega/addons/xbmc.python/addon.xml).
This is a Kodi addon API version, not an embedded CPython version.

**Tested Kodi runtime: none.** This host has no Kodi application or `kodi`
command. Python tests use narrow Kodi boundary fakes; they do not establish
Kodi runtime compatibility. The development host uses Python 3.9.6. Source
uses a Python 3.8-compatible syntax subset; other Python installations have
not been exercised.

The same addon registers the music plugin and service. Official 21.3 sources
verify the [audio plugin extension](https://github.com/xbmc/xbmc/blob/21.3-Omega/addons/xbmc.python/pluginsource.xsd),
[service extension and modern settings format](https://github.com/xbmc/xbmc/tree/21.3-Omega/addons/service.xbmc.versioncheck),
[directory and resolved URL calls](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/ModuleXbmcplugin.h),
[Monitor abort/settings callbacks](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Monitor.h),
[player callbacks](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Player.h),
and [`xbmcvfs.translatePath`](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/ModuleXbmcvfs.h).
Addon metadata explicitly records that playback validation is pending.

## Automated adapter checks

Run `python3 -m pytest tests/test_kodi_runtime.py tests/test_relay.py tests/test_sessions.py -q`
for Kodi navigation/lifecycle and real loopback relay checks. Run
`python3 -m pytest -q` for the full suite. No Kodi fake modules ship in the addon.

Checks cover search/country/tag pages, cancellation and localized failures,
click reporting once per ordinary station selection, opaque loopback playback
URLs, unavailable service and startup deadline, late create cancellation,
restricted atomic descriptor publication and stale-instance-safe deletion,
player callbacks, actual station replacement and stale control token rejection,
settings restart/recovery, opt-in diagnostics and exact fixture-origin policy.
Profile modes 0700 and descriptor mode 0600 are checked on POSIX. Windows uses
its native file locking; Windows ACL behavior has not been runtime-tested.

## Controlled Kodi smoke test still required

Generate authored tone assets and start the developer fixture server as described
in [fixture instructions](../tools/README-fixtures.md). Configure **Enable test
mode** and set the exact origin, for example `http://127.0.0.1:8765`, matching the
server port. Only numeric `127.0.0.1`, HTTP and an explicit port are accepted;
no path, trailing slash or other local address is allowed. Other loopback ports
and private network targets remain blocked. Test mode and diagnostic logging
both default to disabled. Service restart applies setting changes and retires
old playback sessions.

In Kodi 21.3, install the ZIP when packaging is available, verify service startup,
browse/search stations, and play MP3, AAC, ICY and each HLS fixture. Confirm heard
audio, inspect sanitized logs against expected cue schedules, switch stations
rapidly, stop playback, restart the service, and quit Kodi. Record the exact Kodi
build, OS, installation outcome, audio behavior and cleanup timings. Verify
continuous playback when Kodi sends HEAD, Range or repeated GET requests: the
relay currently rejects HEAD/continuous Range and permits only one continuous
consumer. The adapter preserves Kodi content lookup and never forces MIME from directory
codec/HLS hints; the authoritative media type comes from the relay response.
Kodi content lookup and demux selection still need runtime validation.

Cancellation returns control to the Kodi UI promptly and stops a session when a
late create response arrives. An in-flight service resolver is bounded by its
network limits; if the control connection times out before returning a session
ID, an unclaimed session expires after 120 seconds. Kodi player callbacks do
not identify sessions: tracked media URLs and matching session IDs prevent an
old stop event from cancelling a newer session. If a startup error arrives before
Kodi exposes the media URL, idle expiry provides final cleanup.

No cues observed means no cues observed; it does not prove that no advertisements
exist. Observation time differs from the listener's audible playback time.

## Package and live verification — 2026-10-21

Development host: **macOS 26.6.2, build 25G83**, arm64; Python **3.9.6**.
No `kodi` command or `/Applications/Kodi.app` is installed. No Kodi installation,
audio playback or audible tone test was performed. The target remains Kodi 21.3;
**actual tested Kodi/embedded Python version: none**.

Reproduce the source-only distribution from the repository root:

```sh
python3 -m pytest -q
python3 tools/package_addon.py --output dist
python3 tools/check_package.py dist/plugin.audio.kodi_donor_radio-0.1.0.zip
```

The packaging test suite verifies the single addon root, both entry points,
metadata/API dependency, complete GPL v2 text, localization/artwork, approved
contents, 0644 regular files and byte-for-byte reproducibility despite source
mtime/mode changes. Tampered ZIP checks cover missing sources, invalid XML,
identity/license mismatch, missing localization, invalid artwork, secrets, extra
roots, traversal, executable members, symlinks and duplicates. No tests, generated
fixture audio, development tools, caches or virtual environments ship in the ZIP.
Final full suite: **270 passed in 25.15s**. Final package creation and
`check_package.py` both exited 0. Artifact:
`dist/plugin.audio.kodi_donor_radio-0.1.0.zip`; SHA-256:
`505232bc4518646195582153e6631ce2222bf392cc127dea375fa3eba7488361`.

The [official Kodi addon checker](https://github.com/xbmc/addon-check) documents
installation with `pip install kodi-addon-checker` and invocation against an addon
directory. Its current CLI also requires the target branch. Installed locally in
the ignored `.venv/checker/` directory; no application or checker was installed
globally. Exact version command and package-validation invocation:

```sh
.venv/checker/bin/kodi-addon-checker --version
# kodi-addon-checker 0.0.36
.venv/checker/bin/kodi-addon-checker --branch omega dist/checker/plugin.audio.kodi_donor_radio
```

`dist/checker/plugin.audio.kodi_donor_radio` is extracted from the final ZIP, not
a different source tree. The first run exited 0 and reported no problems and no
warnings; icon 512×512, JPEG fanart 1280×720 and PO validation passed. The final
run after the timestamp compatibility change also completed successfully with
`We found no problems and no warnings, please enjoy your day.` The host emits a
separate urllib3 warning because system Python uses LibreSSL 2.8.3; this is a
checker-development-environment warning, not an addon runtime result. The checker
is static validation and cannot establish Kodi playback compatibility.

### Live Radio Browser sample

Command: `python3 tools/smoke_radio_browser.py --limit 3`. Directory discovery and
each station have a 15-second deadline, a maximum 1 MiB finite-body cap and the
production public-address/redirect policy. No `/json/url/` click report is called.
Samples do not decode audio, report tracking or infer cue/advertisement coverage.
Only UUIDs, schemes, classifications and byte counts are recorded; upstream URLs
and directory names are omitted.

Initial run **2026-10-21 17:55:44 UTC**: discovery succeeded; two continuous streams
returned 512 bytes each, while the HLS sample produced a parser failure. Bounded
follow-up inspection identified a valid ISO 8601 basic timezone in
`EXT-X-PROGRAM-DATE-TIME`, for example `2026-10-21T17:58:58+0000`. The playlist master
parsed; its child media playlist used supported tags. Python 3.9's
`datetime.fromisoformat` rejected `+0000`. This was a supported-timestamp
compatibility defect, not a reachability failure or intentionally unsupported HLS
feature. The fix normalizes basic `+HHMM`/`-HHMM` offsets to extended form for
parsing only, retaining original playlist/evidence text. DATERANGE start/end
parsing shares that helper; invalid hour/minute values remain rejected.

Final run **2026-10-21 18:00:27 UTC**, exit **0**, discovery succeeded:

| Station UUID | Input scheme | Resolved protocol | Outcome | Bytes sampled |
| --- | --- | --- | --- | ---: |
| `c685c086-b716-4edd-b313-d4a7baf1084c` | HTTPS | HLS | reachable | 4,096 |
| `db93a00f-9191-46ab-9e87-ec9b373b3eee` | HTTP | continuous | reachable | 512 |
| `ffc0d701-f43b-4232-8be6-f7ff72add8ef` | HTTPS | continuous | reachable | 512 |

Final sample: **0 reachability failures, 0 parser failures, 0 unsupported/policy
rejections**. These three directory results are not a representative protocol,
codec, geographic, uptime or advertisement sample. “Reachable” means bytes were
received and sampled HLS syntax parsed; it does not mean heard/decoded playback.
No cues observed means no cues observed, never proof that no ads exist.

### Kodi acceptance status

| Required real Kodi check | Result on this host |
| --- | --- |
| Install final ZIP and start embedded service | Pending — Kodi unavailable |
| Search/browse and real-station playback | Pending — Kodi unavailable |
| Hear MP3/AAC tone and controlled ICY metadata/audio | Pending — Kodi unavailable |
| Hear HLS tone and match every fixture window/event timeline | Pending — Kodi unavailable |
| Stop, rapid station switch, settings restart, quit and resource cleanup | Pending — Kodi unavailable |
| Verify probing/HEAD/Range/reopen, buffering and audible boundaries | Pending — Kodi unavailable |

A future tester must record exact Kodi build, embedded Python version, OS, heard
audio and timings here. Pure-Python tests and the static checker do not replace
these acceptance checks. Generate fixture audio with FFmpeg into ignored
`tests/generated-audio/`, enable the exact local fixture origin in test mode, and
restart the fixture server between scenarios. Expected events are available at
`/expected/{scenario}.json`; compare ordered event kind/classification for all
three windows rather than assuming a skipped window was observed.
