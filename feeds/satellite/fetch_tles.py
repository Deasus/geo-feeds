#!/usr/bin/env python3
"""
geo-feeds satellite TLE pipeline.

Pulls Two-Line Element sets from CelesTrak for the 9 fire-relevant
polar-orbiting Earth-observation satellites, writes a slim JSON the
frontend consumes via satellite.js to compute ground positions.

Refresh cadence: daily (TLE drift after ~7 days starts adding km of
along-track error; daily is plenty for pass-prediction accuracy).

Output: data/tles.json
Shape: {
  "generated_at": ISO8601,
  "source": "celestrak.org",
  "satellites": [
    { "id": "terra", "name": "TERRA", "platform": "EOS",
      "sensor": "MODIS", "swath_km": 2330, "color": "#39d0d8",
      "norad": 25994, "tle1": "...", "tle2": "..." },
    ...
  ]
}
"""

from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
import requests

# Per-satellite metadata. NORAD catalog numbers are stable; CelesTrak's
# CATNR query returns the latest TLE for each.
SATELLITES = [
    # NASA EOS — MODIS sensors
    {"id": "terra",  "name": "TERRA",   "platform": "EOS",      "sensor": "MODIS",
     "swath_km": 2330, "color": "#39d0d8", "norad": 25994},
    {"id": "aqua",   "name": "AQUA",    "platform": "EOS",      "sensor": "MODIS",
     "swath_km": 2330, "color": "#2bb673", "norad": 27424},

    # NASA/NOAA Joint Polar Satellite System — VIIRS sensors
    {"id": "snpp",     "name": "SUOMI NPP", "platform": "JPSS", "sensor": "VIIRS",
     "swath_km": 3000, "color": "#ff8c42", "norad": 37849},
    {"id": "noaa20",   "name": "NOAA 20",   "platform": "JPSS", "sensor": "VIIRS",
     "swath_km": 3000, "color": "#ffaa00", "norad": 43013},
    {"id": "noaa21",   "name": "NOAA 21",   "platform": "JPSS", "sensor": "VIIRS",
     "swath_km": 3000, "color": "#ff5a3a", "norad": 54234},

    # ESA Copernicus — Sentinel-2 (MSI optical) and Sentinel-3 (OLCI/SLSTR)
    {"id": "sentinel2a", "name": "SENTINEL-2A", "platform": "Copernicus", "sensor": "MSI",
     "swath_km": 290,  "color": "#b388ff", "norad": 40697},
    {"id": "sentinel2b", "name": "SENTINEL-2B", "platform": "Copernicus", "sensor": "MSI",
     "swath_km": 290,  "color": "#9d6dff", "norad": 42063},
    {"id": "sentinel3a", "name": "SENTINEL-3A", "platform": "Copernicus", "sensor": "OLCI",
     "swath_km": 1270, "color": "#7a4fff", "norad": 41335},
    {"id": "sentinel3b", "name": "SENTINEL-3B", "platform": "Copernicus", "sensor": "OLCI",
     "swath_km": 1270, "color": "#5e3bff", "norad": 43437},
]

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"
HEADERS = {"User-Agent": "geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)"}


def fetch_tle(norad: int, session: requests.Session) -> tuple[str, str] | None:
    """Pull a single satellite's TLE from CelesTrak. Returns (line1, line2)
    or None on any failure — pipeline keeps going if one bird is unavailable."""
    url = CELESTRAK_URL.format(norad=norad)
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        lines = [ln.strip() for ln in r.text.strip().split("\n") if ln.strip()]
        # Response is "NAME\nLINE1\nLINE2" — sometimes 3 lines, sometimes just 2.
        # We want the two lines starting with "1 " and "2 ".
        tle1 = next((ln for ln in lines if ln.startswith("1 ")), None)
        tle2 = next((ln for ln in lines if ln.startswith("2 ")), None)
        if not tle1 or not tle2:
            print(f"  [{norad}] no TLE in response: {r.text[:120]!r}", flush=True)
            return None
        return tle1, tle2
    except Exception as e:
        print(f"  [{norad}] fetch failed: {e}", flush=True)
        return None


def main() -> int:
    print("=" * 60)
    print("geo-feeds Satellite TLE Pipeline")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Targets: {len(SATELLITES)}")
    print("=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)

    out_sats = []
    for sat in SATELLITES:
        # Be polite — 1s between hits even though CelesTrak is generous
        tle = fetch_tle(sat["norad"], session)
        if not tle:
            continue
        out_sats.append({
            **sat,
            "tle1": tle[0],
            "tle2": tle[1],
        })
        print(f"  [{sat['name']:14s}] OK", flush=True)
        time.sleep(1)

    if not out_sats:
        print("[FATAL] no TLEs fetched, aborting", flush=True)
        return 1

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "celestrak.org",
        "satellite_count": len(out_sats),
        "satellites": out_sats,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/tles.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n[write] data/tles.json — {len(out_sats)}/{len(SATELLITES)} satellites")
    return 0


if __name__ == "__main__":
    sys.exit(main())
