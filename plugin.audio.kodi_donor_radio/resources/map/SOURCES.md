# Bundled land geometry

`natural_earth_110m_land.json` is derived from the official Natural Earth
1:110m physical land polygons, including major islands. Made with Natural Earth.

- Dataset: https://www.naturalearthdata.com/downloads/110m-physical-vectors/
- Official AWS-hosted ZIP: https://naturalearth.s3.amazonaws.com/110m_physical/ne_110m_land.zip
- Public-domain terms: https://www.naturalearthdata.com/about/terms-of-use/
- Retrieved: 2026-10-26
- Archive version: **4.1.0**, from `ne_110m_land.VERSION.txt` inside the ZIP.
  The download page currently labels the Land link **4.0.0**; the archive's
  embedded version is authoritative for this bundled copy.
- ZIP size: 69,700 bytes
- ZIP SHA-256: `1926c621afd6ac67c3f36639bb1236134a48d82226dc675d3e3df53d02d2a3de`
- Derived JSON SHA-256: `19aa351d4738c8e38190db0caa09fdc9b2b83e3cd3c98bda77c117e516557908`

Natural Earth's map data is in the public domain and may be modified and
redistributed for personal, educational, and commercial purposes. No attribution
is required; the credit above is included voluntarily.

## Reproduce

From the repository root, download the source separately, then run the offline
standard-library converter against that local ZIP:

```sh
curl --fail --location --output /tmp/ne_110m_land-world-map.zip https://naturalearth.s3.amazonaws.com/110m_physical/ne_110m_land.zip
python3 tools/convert_natural_earth_land.py /tmp/ne_110m_land-world-map.zip plugin.audio.kodi_donor_radio/resources/map/natural_earth_110m_land.json
```

The converter itself performs no network requests. It reads only the local ZIP's
Polygon shapefile and version marker; attributes and other ZIP members are not
retained or extracted. It preserves polygon record and ring-part order and rounds
coordinates to six decimal places, normalizing machine-epsilon overshoots at
longitude +/-180 and latitude -90. The result contains only schema/source
metadata and closed `[longitude, latitude]` rings: 127 polygons, 5,143 points,
115,993 bytes. Input/output geometry is limited to 2 MiB and 100,000 points.

The add-on loads this bundled JSON and renders with the Python standard library.
It does not fetch map tiles, contact Natural Earth, or require GIS/imaging packages.
