# Radio map texture render timing — 2026-10-30

The synchronous miss path was timed to assess the Kodi GUI-thread suggestion.
The benchmark used the checked-in 113 KB Natural Earth geometry, decoded 127
polygons, and generated the default 1680×840 full-world PNG five times. Geometry
decode took 6 ms; render calls took 39–46 ms and produced a 27,988-byte PNG.
The PNG cache hit avoids geometry decoding and rasterization.

This measures the pure-Python decode/render functions on the current
`mbpdev.local` host. It excludes geometry file reads, cache writes/fsync, texture
loading and a Kodi window run, and does not measure a slower supported device,
so it is not a worst-device bound. The measured decode/render portion is about
45–52 ms. A background render still needs a
verified way to publish `ControlImage` updates on Kodi's GUI thread and reject
late results after the dialog closes. No thread change is made without that
dispatch/lifetime proof; the cache keeps subsequent identical views off the
render path.
