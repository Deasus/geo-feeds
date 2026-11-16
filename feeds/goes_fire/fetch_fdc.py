#!/usr/bin/env python3
"""
geo-feeds GOES fire pipeline — pulls the GOES-R ABI Level-2 Fire / Hot Spot
Characterization (FDC) product from NOAA's Open Data S3 buckets, decodes the
netCDF, merges GOES-East (G19) + GOES-West (G18) CONUS sectors, and writes a
slim JSON the frontend reads via raw.githubusercontent.com.

WHY THIS EXISTS — national, continuous fire detection between VIIRS passes.
geo-feeds already shows NASA FIRMS/VIIRS thermal hotspots, but those come from
POLAR-orbiting satellites: only ~2-4 overpasses/day, leaving multi-hour blind
windows during which a fire can blow up unseen. GOES ABI is GEOSTATIONARY — it
stares at the same hemisphere continuously and the FDC product refreshes every
~5 minutes (CONUS sector). Adding it gives operators near-continuous
Fire-Radiative-Power (FRP) growth curves nationwide — the single biggest
temporal-coverage gap in the current detection stack, and a direct blow-up /
spot-fire early-warning signal. This is a NATIONAL capability: G19 covers the
eastern + central US, G18 covers the western US + Alaska + Hawaii.

WHAT FDC IS (and isn't):
  • 2 km spatial resolution at nadir (coarser than VIIRS 375 m / Landsat 30 m).
    So FDC is the EARLY/CONTINUOUS detector; VIIRS/Landsat remain the precise
    locator. They complement — keep both layers.
  • Each granule carries per-pixel Mask (fire category), Power (FRP in MW),
    Temp (K), Area (m^2). We emit fire pixels + FRP/Temp.
  • Detection is by 3.9 µm / 11.2 µm brightness-temperature; heavy cloud blocks
    it (flagged cloud_contaminated). Sun-glint / high-zenith block-out zones
    exist near the disk edge — those pixels are simply absent, not wrong.
  • End-to-end latency strike→JSON ≈ 5-12 min (ABI scan + ground processing +
    S3 publish + our cron). Situational awareness, not a replacement for
    aircraft IR perimeter mapping.

OUTPUT: data/goes_fire.json
Shape: { "generated_at": ISO8601, "newest_granule": ISO8601, "oldest_granule": ISO8601,
         "window_minutes": N, "window_scans": S,      # rolling window actually merged
         "counts": {"total": N, "g19": N19, "g18": N18},
         "detections": [ {lat,lng,frp,tempK,tier,sat,age_sec,first_seen_sec,scans}, ... ] }
  age_sec = most recent sighting · first_seen_sec = oldest sighting in the window ·
  scans = how many scans (either satellite) saw this ~3 km cell. lat/lng/frp/tempK/tier/sat
  come from the strongest (max-FRP) sighting.

SOURCE (public, anonymous, no auth, no egress charge — same NODD program as GLM):
  s3://noaa-goes19/ABI-L2-FDCC/<YYYY>/<DDD>/<HH>/*.nc   (East/CONUS)
  s3://noaa-goes18/ABI-L2-FDCC/<YYYY>/<DDD>/<HH>/*.nc   (West/CONUS)
  FDCC = CONUS sector (~5 min). (FDCF = full disk ~10 min; FDCM = mesoscale.)

KNOWN SOURCES (2026-11-16): every published cell carries the deconfliction flag the
FIRMS feed gives polar detections (deconflict.py against the industrial-sites
reference: plants, flares, landfills, volcanoes, NASA static land sources,
persistent hotspots), with the reach widened by THIS pixel's footprint: a GOES pixel
over the western US is ~2.5 x 3.5 km, so a source anywhere in it explains the heat.
  pix_km = [east-west, north-south] ground size of the pixel (fixed grid, PUG vol 3)
  deconfliction_flag = clear | proximate_<class>; nearest_infra_name / _m / _class when flagged
Flagged cells are NOT dropped (life-safety default, as in the FIRMS feed): clients
decide. counts.by_flag and deconfliction{} say what the reference held.

Requires: boto3, botocore, netCDF4, numpy (scikit-learn for the reference index). No API key.
"""
from __future__ import annotations
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore import UNSIGNED
from botocore.config import Config
import netCDF4
import numpy as np

try:   # the conductor copies feeds/firms/*.py beside this file; a local run reads it in place
    from deconflict import Deconflicter
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firms'))
    try:
        from deconflict import Deconflicter
    except ImportError:
        Deconflicter = None

# ── Config ───────────────────────────────────────────────────────────
# ROLLING WINDOW (2026-10-05). This used to publish only the SINGLE newest granule per
# satellite. On a quiet morning that showed 1 detection nationwide, while NGFS (30-min
# lookback) showed 15, and a fire that dipped below detection for one 5-min scan vanished
# and reappeared. We now merge the newest WINDOW_SCANS granules per satellite: ~30 min at
# the 5-min CONUS cadence. Replayed on 2026-10-05 13:26Z: 1 scan -> 1 location, 6 scans -> 4.
WINDOW_SCANS = int(os.environ.get("FDC_WINDOW_SCANS", "6"))
# Decoded granules are cached here, so the workflow's 4-iteration loop downloads only the
# NEW granule (~1 per satellite) each iteration instead of all 12.
CACHE_DIR = os.environ.get("FDC_CACHE_DIR", "/tmp/fdc_cache")
LOOKBACK_HOURS = 2          # search this many hours back for the window's granules
SATS = [("noaa-goes19", "G19", "g19"), ("noaa-goes18", "G18", "g18")]
PRODUCT_PREFIX = "ABI-L2-FDCC"     # CONUS sector
OUT_PATH = os.environ.get("FDC_OUT_PATH") or os.path.join(os.path.dirname(__file__), "data", "goes_fire.json")
# The reference, kept beside the code (not in data/) for 6 h: this feed runs every 2 min and the
# reference is rebuilt weekly.
REF_CACHE = os.environ.get("FDC_REF_CACHE", "_industrial_cache.json")
# GOES-R fixed grid, 2 km bands (FDC): the scan-angle step in radians (PUG vol 3, 56 µrad)
FDC_STEP_RAD = 56e-6
# GRS80 and the GOES-R perspective height, as the FDC files carry them in goes_imager_projection
R_EQ, R_POL, H_PERSP = 6378137.0, 6356752.31414, 35786023.0
SUB_LON = {"G18": -137.0, "G19": -75.0}   # longitude_of_projection_origin of each satellite's files

# GOES FDC Mask flag_values → a coarse confidence tier we surface to the
# frontend. (Full meanings verified from the live granule's flag_meanings attr.)
#   10/30 good · 11/31 saturated · 13/33 high-prob · 14/34 med-prob ·
#   15/35 low-prob · 12/32 cloud-contaminated.  (30s = temporally-filtered.)
FIRE_TIERS = {
    10: "good", 30: "good",
    11: "saturated", 31: "saturated",
    13: "high", 33: "high",
    14: "medium", 34: "medium",
    15: "low", 35: "low",
    12: "cloud", 32: "cloud",
}
# Tiers we DROP from the published layer (too speculative for an ops COP). Low-
# probability + cloud-contaminated stay OUT of the headline detections to avoid
# false positives; good/saturated/high/medium are kept. The frontend can still
# style by tier. Adjust here, not in the frontend.
KEEP_TIERS = {"good", "saturated", "high", "medium"}

S3 = boto3.client(
    "s3",
    config=Config(signature_version=UNSIGNED, read_timeout=30, retries={"max_attempts": 3}),
)


def _newest_granule_keys(bucket: str, n: int) -> list[str]:
    """The n most recent FDCC .nc keys (oldest first), across hour boundaries."""
    now = datetime.now(timezone.utc)
    found: list[str] = []
    for h in range(LOOKBACK_HOURS + 1):
        t = now - timedelta(hours=h)
        prefix = f"{PRODUCT_PREFIX}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
        token = None
        keys = []
        while True:
            kw = dict(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
            if token:
                kw["ContinuationToken"] = token
            r = S3.list_objects_v2(**kw)
            keys.extend(o["Key"] for o in r.get("Contents", []))
            if r.get("IsTruncated"):
                token = r.get("NextContinuationToken")
            else:
                break
        # filenames sort lexically == chronologically (start-time encoded)
        found = sorted(keys) + found
        if len(found) >= n:
            break
    return found[-n:]


def _granule(bucket: str, key: str, sat_label: str, now: datetime):
    """Decoded (records, scan-start) for one granule, via the per-job cache."""
    path = os.path.join(CACHE_DIR, key.replace("/", "_") + ".json")
    if os.path.exists(path):
        with open(path) as f:
            c = json.load(f)
        gen = datetime.strptime(c["gen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = int((now - gen).total_seconds())
        for r in c["recs"]:
            r["age_sec"] = age
        return c["recs"], gen
    raw = S3.get_object(Bucket=bucket, Key=key)["Body"].read()
    recs, gen = _decode(raw, sat_label, now)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"gen": gen.strftime("%Y-%m-%dT%H:%M:%SZ"), "recs": recs}, f)
    except OSError:
        pass
    return recs, gen


def _geo_latlon(proj, x_rad: np.ndarray, y_rad: np.ndarray):
    """GOES-R fixed-grid scan angles → geodetic lat/lon (PUG vol3 algorithm)."""
    H = proj.perspective_point_height + proj.semi_major_axis
    req = proj.semi_major_axis
    rpol = proj.semi_minor_axis
    lon0 = np.radians(proj.longitude_of_projection_origin)
    sinx, cosx = np.sin(x_rad), np.cos(x_rad)
    siny, cosy = np.sin(y_rad), np.cos(y_rad)
    a = sinx ** 2 + (cosx ** 2) * (cosy ** 2 + (req ** 2 / rpol ** 2) * siny ** 2)
    b = -2 * H * cosx * cosy
    c = H ** 2 - req ** 2
    disc = b * b - 4 * a * c
    good = disc >= 0
    rs = np.full_like(a, np.nan)
    rs[good] = (-b[good] - np.sqrt(disc[good])) / (2 * a[good])
    sx = rs * cosx * cosy
    sy = -rs * sinx
    sz = rs * cosx * siny
    lat = np.degrees(np.arctan((req ** 2 / rpol ** 2) * (sz / np.sqrt((H - sx) ** 2 + sy ** 2))))
    lon = np.degrees(lon0 - np.arctan(sy / (H - sx)))
    return lat, lon, good


class _Proj:
    """The projection constants of goes_imager_projection, for a satellite with no granule open."""
    def __init__(self, lon0):
        self.perspective_point_height = H_PERSP
        self.semi_major_axis = R_EQ
        self.semi_minor_axis = R_POL
        self.longitude_of_projection_origin = lon0


def _scan_angles(proj, lat_deg, lon_deg):
    """Geodetic lat/lon → GOES-R fixed-grid scan angles (x, y) in radians (PUG vol 3, the inverse)."""
    req, rpol = proj.semi_major_axis, proj.semi_minor_axis
    H = proj.perspective_point_height + req
    e2 = (req ** 2 - rpol ** 2) / req ** 2
    lat = np.radians(lat_deg)
    dlon = np.radians(lon_deg) - np.radians(proj.longitude_of_projection_origin)
    phic = np.arctan((rpol ** 2 / req ** 2) * np.tan(lat))
    rc = rpol / np.sqrt(1 - e2 * np.cos(phic) ** 2)
    sx = H - rc * np.cos(phic) * np.cos(dlon)
    sy = -rc * np.cos(phic) * np.sin(dlon)
    sz = rc * np.sin(phic)
    return np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2)), np.arctan(sz / sx)


def _gc_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * np.arcsin(np.minimum(1, np.sqrt(a)))


def pixel_km(proj, x_rad, y_rad, step=FDC_STEP_RAD):
    """Ground size (km) of the pixels centred at scan angles (x, y): east-west across x ± step/2,
    north-south across y ± step/2. Arrays in, arrays out."""
    x_rad, y_rad = np.asarray(x_rad, dtype=float), np.asarray(y_rad, dtype=float)
    la0, lo0, _ = _geo_latlon(proj, x_rad - step / 2, y_rad)
    la1, lo1, _ = _geo_latlon(proj, x_rad + step / 2, y_rad)
    la2, lo2, _ = _geo_latlon(proj, x_rad, y_rad - step / 2)
    la3, lo3, _ = _geo_latlon(proj, x_rad, y_rad + step / 2)
    return _gc_km(la0, lo0, la1, lo1), _gc_km(la2, lo2, la3, lo3)


def pixel_km_at(sat_label, lat, lon):
    """The FDC pixel footprint at a published lat/lon (for a record decoded before pix_km existed)."""
    proj = _Proj(SUB_LON.get(sat_label, -137.0))
    x, y = _scan_angles(proj, lat, lon)
    ew, ns = pixel_km(proj, x, y)
    return float(ew), float(ns)


def deconflict_cells(detections):
    """Give each published cell the FIRMS feed's deconfliction flag, its reach widened by the
    cell's own pixel footprint (deconflict.effective_buffer_m). Never drops a cell; never raises."""
    meta = {"reference_loaded": False, "n_sources": 0, "by_flag": {},
            "rule": "source buffer_m + half the pixel diagonal (pix_km) + 100 m location error"}
    for d in detections:
        d["deconfliction_flag"] = "clear"
    if Deconflicter is None or not detections:
        meta["by_flag"] = {"clear": len(detections)} if detections else {}
        return meta
    try:
        dc = Deconflicter()
        dc.load(land=False, cache_path=REF_CACHE)
        meta["reference_loaded"], meta["n_sources"] = dc.loaded, dc.n_sources
        rows = []
        for d in detections:
            pk = d.get("pix_km") or [2.0, 2.0]
            rows.append({"lat": d["lat"], "lng": d["lng"], "confidence": None, "sensor": "ABI", "scan": pk[0], "track": pk[1]})
        if dc.loaded:
            dc.enrich_many(rows)
        for d, r in zip(detections, rows):
            flag = r.get("deconfliction_flag") or "clear"
            d["deconfliction_flag"] = flag
            if flag != "clear":
                d["nearest_infra_name"] = r.get("nearest_infra_name")
                d["nearest_infra_m"] = r.get("nearest_infra_m")
                d["nearest_infra_class"] = r.get("nearest_infra_class")
    except Exception as e:  # the fire layer must publish whatever the reference does
        print(f"[deconflict] GOES enrichment failed: {e}", file=sys.stderr)
    for d in detections:
        meta["by_flag"][d["deconfliction_flag"]] = meta["by_flag"].get(d["deconfliction_flag"], 0) + 1
    return meta


def _decode(raw: bytes, sat_label: str, now: datetime):
    ds = netCDF4.Dataset("inmem", memory=raw)
    try:
        proj = ds.variables["goes_imager_projection"]
        x = ds.variables["x"][:]
        y = ds.variables["y"][:]
        mask = ds.variables["Mask"][:]
        power = ds.variables["Power"][:]
        temp = ds.variables["Temp"][:]
        tstart = ds.time_coverage_start
        gen = datetime.strptime(tstart[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        age = int((now - gen).total_seconds())

        fire_codes = np.fromiter(FIRE_TIERS.keys(), dtype=int)
        fy, fx = np.where(np.isin(np.asarray(mask), fire_codes))
        if fy.size == 0:
            return [], gen
        # vectorized lat/lon for just the fire pixels
        xr = np.asarray(x)[fx]
        yr = np.asarray(y)[fy]
        lat, lon, good = _geo_latlon(proj, xr, yr)
        step = float(abs(np.asarray(x)[1] - np.asarray(x)[0])) if np.asarray(x).size > 1 else FDC_STEP_RAD
        ew, ns = pixel_km(proj, xr, yr, step)
        out = []
        for n in range(fy.size):
            if not good[n]:
                continue
            j, i = int(fy[n]), int(fx[n])
            tier = FIRE_TIERS[int(mask[j, i])]
            if tier not in KEEP_TIERS:
                continue
            rec = {
                "lat": round(float(lat[n]), 4),
                "lng": round(float(lon[n]), 4),
                "tier": tier,
                "sat": sat_label,
                "age_sec": age,
                "pix_km": [round(float(ew[n]), 2), round(float(ns[n]), 2)] if np.isfinite(ew[n]) and np.isfinite(ns[n]) else None,
            }
            pv = power[j, i]
            if pv is not np.ma.masked and float(pv) >= 0:
                rec["frp"] = round(float(pv), 1)
            tv = temp[j, i]
            if tv is not np.ma.masked:
                rec["tempK"] = round(float(tv), 1)
            out.append(rec)
        return out, gen
    finally:
        ds.close()


def main() -> int:
    now = datetime.now(timezone.utc)
    counts = {}
    newest_gen = oldest_gen = None
    cells: dict = {}
    for bucket, sat_label, count_key in SATS:
        try:
            keys = _newest_granule_keys(bucket, WINDOW_SCANS)
            if not keys:
                print(f"[{sat_label}] no recent FDCC granule found", file=sys.stderr)
                counts[count_key] = 0
                continue
            sat_cells = set()
            for key in keys:
                recs, gen = _granule(bucket, key, sat_label, now)
                newest_gen = gen if newest_gen is None or gen > newest_gen else newest_gen
                oldest_gen = gen if oldest_gen is None or gen < oldest_gen else oldest_gen
                for d in recs:
                    # one record per ~3 km cell (~1.5 FDC pixels): also de-dups the G19/G18
                    # overlap over the central US
                    k = (round(d["lat"] / 0.03), round(d["lng"] / 0.03))
                    sat_cells.add(k)
                    c = cells.get(k)
                    if c is None:
                        cells[k] = {**d, "first_seen_sec": d["age_sec"], "_scans": {(sat_label, key)}}
                        continue
                    c["_scans"].add((sat_label, key))
                    c["first_seen_sec"] = max(c["first_seen_sec"], d["age_sec"])
                    last = min(c["age_sec"], d["age_sec"])
                    if d.get("frp", 0) > c.get("frp", 0):      # show the strongest sighting
                        for f in ("lat", "lng", "frp", "tempK", "tier", "sat", "pix_km"):
                            if f in d:
                                c[f] = d[f]
                            else:
                                c.pop(f, None)
                    c["age_sec"] = last                         # but date it by the latest
            counts[count_key] = len(sat_cells)
            print(f"[{sat_label}] {len(keys)} granules {keys[0].split('_s')[-1][:13]}..{keys[-1].split('_s')[-1][:13]}"
                  f" -> {len(sat_cells)} cells")
        except Exception as e:  # one satellite failing must not kill the other
            print(f"[{sat_label}] ERROR: {e}", file=sys.stderr)
            counts[count_key] = 0

    detections = []
    for c in sorted(cells.values(), key=lambda r: -(r.get("frp", 0))):
        c["scans"] = len(c.pop("_scans"))
        if not c.get("pix_km"):   # a granule decoded (and cached) before pix_km existed
            try:
                c["pix_km"] = [round(v, 2) for v in pixel_km_at(c.get("sat"), c["lat"], c["lng"])]
            except Exception:
                c["pix_km"] = None
        detections.append(c)
    dmeta = deconflict_cells(detections)
    window_min = (round((newest_gen - oldest_gen).total_seconds() / 60) + 5) if newest_gen else 0

    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newest_granule": (newest_gen.strftime("%Y-%m-%dT%H:%M:%SZ") if newest_gen else None),
        "oldest_granule": (oldest_gen.strftime("%Y-%m-%dT%H:%M:%SZ") if oldest_gen else None),
        "window_minutes": window_min,
        "window_scans": WINDOW_SCANS,
        "product": "GOES-R ABI L2 FDC (Fire/Hot Spot Characterization), CONUS sector, 2km",
        "counts": {"total": len(detections), **counts, "by_flag": dmeta["by_flag"]},
        "deconfliction": {k: dmeta[k] for k in ("reference_loaded", "n_sources", "rule")},
        "detections": detections,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {OUT_PATH}: {len(detections)} detections over {window_min} min "
          f"(G19={counts.get('g19',0)}, G18={counts.get('g18',0)}), newest granule {payload['newest_granule']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
