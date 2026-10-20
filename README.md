# geo-feeds

Scheduled mirrors of public Earth-observation, weather, hazard and air-traffic feeds, normalized to JSON and served from `raw.githubusercontent.com` (CORS-open). Browser clients can read them without hitting the upstream sources directly.

**Where the data lives:** each feed publishes to a branch named after it. The branch holds **one commit** that is replaced on every update, so the repository never builds up data history.

```
https://raw.githubusercontent.com/Deasus/geo-feeds/<feed>/<file>
https://raw.githubusercontent.com/Deasus/geo-feeds/status/status.json   # health of every feed
```

**How it runs:** one GitHub Actions job (`conductor.py`) runs each feed on its own cadence, from every minute to weekly, then hands over to the next job.

## Feeds

| Branch | Contents | Upstream | Cadence |
| --- | --- | --- | --- |
| `firms` | VIIRS + MODIS hotspots (US + global per region), deconflicted; `persistence/` = rolling 90-day daily archive | NASA FIRMS | 15 min |
| `goes-fire` | GOES ABI fire detections | NOAA open data | 2 min |
| `ngfs` | Next Generation Fire System detections | UW–Madison SSEC/CIMSS | 2 min |
| `himawari` | Himawari-9 fire hotspots over Australia, deconflicted | Geoscience Australia, DEA Hotspots (CC BY 4.0) | 10 min |
| `meteosat` | Meteosat-12 fire detections: Europe, Africa, Middle East; per-region files; deconflicted | EUMETSAT LSA SAF (CC BY 4.0) | 10 min |
| `lightning` | GLM lightning | NOAA open data | 1 min |
| `wind`, `ecmwf`, `hrrr` | GFS, ECMWF and HRRR wind (+ HRRR temperature/RH) | NOAA, ECMWF open data | 1–3 h |
| `aqi`, `cams` | Air quality; speciated aerosol | Open-Meteo; Copernicus ADS | 30 min; 6 h |
| `potential`, `imsr` | 7-day fire potential; daily national situation report | NWCG; NICC | 3 h; 15 min |
| `cameras` | Wildfire camera catalog | AlertWest | 1 h |
| `aircraft` | Live ADS-B, classified | adsb.lol, adsb.fi, airplanes.live (+ ADS-B Exchange if keyed) | 5 min |
| `satellite` | Satellite orbit elements | CelesTrak | daily |
| `news` | Fire and hazard news | GDELT, Google News, ReliefWeb, GDACS and others | 30 min |
| `industrial` | Deconfliction reference: industrial sites, gas flares, volcanoes, nuclear plants, coastline, persistent hotspots | EPA FRS, WRI, OpenStreetMap, Smithsonian GVP, NASA GIBS, Natural Earth, own 90-day mining | weekly |

## Deconfliction

Each fire feed checks every detection against `industrial/industrial_sites.min.geojson`. Detections inside the buffer of a known non-fire heat source are flagged (FIRMS), or moved to a `suppressed` list with the reason (Himawari, Meteosat). The persistent-hotspot layer is mined each week from the FIRMS feed's own 90-day daily archive.

## Attribution

Each data file names its upstream source and licence. Credit the original providers when you reuse the data.
