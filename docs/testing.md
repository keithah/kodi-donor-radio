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
