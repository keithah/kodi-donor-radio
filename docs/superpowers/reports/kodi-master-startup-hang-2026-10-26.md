# Fresh Kodi master startup retry — 2026-10-26

## Setup

- Add-on package: `plugin.audio.kodi_donor_radio` 0.1.0, including the speed
  guard at commit `1d0bc99`.
- Kodi: unpatched master `434da3cdb7974dbc40d6796fd5f96c10f12a0c6e`,
  23.0-ALPHA1, on the local Mac.
- Isolated portable profile and fixture origin: `127.0.0.1` only.
- The existing USB network adapter remained connected. No system or Kodi USB
  workaround was applied.

## Observed result

Two clean launches stopped before Kodi's JSON-RPC listener opened and before the
add-on route could be invoked. In both process samples the `XBMCMainThread` was
blocked through:

```text
CPeripheralBusUSB::DeviceAttachCallback
  -> IOCreatePlugInInterfaceForService
    -> IOUSBInterfaceClass::start
      -> IOServiceOpen / mach_msg2_trap
```

The active portable-profile log reached skin initialization and ended at
`[Media Detection] starting service for optical media detection`. It did not
record addon playback or fixture events. The HTTP JSON-RPC endpoint on port
8080 was unavailable; the configured 9777 value is Kodi's event-server port,
not the HTTP JSON-RPC endpoint.

Process samples are retained outside the repository at
`/private/tmp/kodi-donor-radio-master-acceptance/kodi-speed-guard-startup.sample.txt`
and
`/private/tmp/kodi-donor-radio-master-acceptance/kodi-speed-guard-retry.sample.txt`.
The corresponding portable log is at
`/private/tmp/kodi-donor-radio-master-acceptance/Kodi.app/Contents/Resources/Kodi/portable_data/temp/kodi.log`.

## Conclusion

This is a reproducible Kodi-host startup block on the current device setup,
before addon execution. A previous successful master launch is recorded
separately and does not make this run pass. The add-on's post-speed-guard
master-runtime behavior remains unverified; no claim is made about audio output
or the absolute Kodi-clock-to-presentation anchor.

Source inspection found no profile setting that skips Kodi's USB peripheral bus
enumeration while preserving a USB network adapter. A viable workaround would
require a Kodi-side startup change or a device/runtime change; neither was
applied in this run.
