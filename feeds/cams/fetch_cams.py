"""
CAMS speciated aerosol fetcher.

Pulls Aerosol Optical Depth at 550nm for 7 species from the Copernicus
Atmosphere Monitoring Service (CAMS) global atmospheric composition forecasts:

  • sea_salt_aerosol_optical_depth_550nm   — marine aerosol (sea spray salt)
  • dust_aerosol_optical_depth_550nm       — Saharan / Mongolian / Australian dust
  • organic_matter_aerosol_optical_depth_550nm  — biomass burning + biogenic SOA
  • black_carbon_aerosol_optical_depth_550nm    — combustion soot
  • sulphate_aerosol_optical_depth_550nm   — fossil-fuel pollution + volcanic
  • nitrate_aerosol_optical_depth_550nm    — fertilizer / vehicle emissions
  • ammonium_aerosol_optical_depth_550nm   — agricultural / livestock

Why AOD instead of mass mixing ratios:
  Mass mixing ratios (aermr01..) are model-level fields — pulling surface values
  requires also pulling pressure/model levels, which blows up request size past
  ADS limits. AOD is single-level, dimensionless (0–~3), bounded, and exactly
  what NASA Worldview / Windy Pro use for aerosol visualization.

Auth:
  The CDS API token is read from one of (in priority):
    1. CDS_API_TOKEN env var (GHA secret path — preferred for CI)
    2. ~/.cdsapirc file (laptop dev path)

Output:
  data/current-aerosol.json — slimmed grid, ~5-10 MB, format:
    {
      "updated": "2026-06-09T11:34:00Z",
      "valid_time": "2026-06-09T00:00:00Z",
      "resolution_deg": 0.4,
      "species": ["sea_salt", "dust", "organic_matter", "black_carbon",
                  "sulphate", "nitrate", "ammonium"],
      "stats": {"sea_salt": {"max": 1.23, "mean": 0.04, "p95": 0.18}, ...},
      "grid": [[lat, lng, ss, du, om, bc, su, ni, am], ...]
    }

  Cells where ALL species are below MIN_AOD_THRESHOLD are dropped. Reduces file
  size ~70% by skipping ocean/desert backgrounds with no meaningful aerosol.
"""
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import cdsapi
import numpy as np
import xarray as xr

# Each species → CDS variable name + short tag for output JSON.
SPECIES = [
    ("sea_salt_aerosol_optical_depth_550nm",        "sea_salt"),
    ("dust_aerosol_optical_depth_550nm",            "dust"),
    ("organic_matter_aerosol_optical_depth_550nm",  "organic_matter"),
    ("black_carbon_aerosol_optical_depth_550nm",    "black_carbon"),
    ("sulphate_aerosol_optical_depth_550nm",        "sulphate"),
    ("nitrate_aerosol_optical_depth_550nm",         "nitrate"),
    ("ammonium_aerosol_optical_depth_550nm",        "ammonium"),
]

# AOD floor — below this, the species is "essentially zero" and the cell can
# be dropped. 0.05 ≈ 5% optical thickness, the lower bound of visually-relevant
# aerosol on the published color scales (NASA Worldview / Windy Pro both treat
# AOD < 0.05 as "clean air" and render no color).
MIN_AOD_THRESHOLD = 0.05

# Output resolution: ADS pre-interpolates to 0.4° regular lat/lon. We
# downsample 2× to 0.8° because DEEPWatch is a global dashboard — sub-0.4°
# detail isn't legible at globe-scale anyway, and 2× downsampling cuts the
# JSON ~4× without visible loss.
DOWNSAMPLE_FACTOR = 2

REPO_ROOT = pathlib.Path(__file__).parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
OUT_PATH = DATA_DIR / "current-aerosol.json"


def setup_cdsapi_creds():
    """Write ~/.cdsapirc from CDS_API_TOKEN env var if present (GHA path)."""
    token = os.environ.get("CDS_API_TOKEN", "").strip()
    if not token:
        # Fall back to existing ~/.cdsapirc (laptop dev).
        if not pathlib.Path.home().joinpath(".cdsapirc").exists():
            sys.exit("FATAL: no CDS_API_TOKEN env var and no ~/.cdsapirc file")
        return

    rc = pathlib.Path.home() / ".cdsapirc"
    rc.write_text(f"url: https://ads.atmosphere.copernicus.eu/api\nkey: {token}\n")
    rc.chmod(0o600)


def pick_run_date():
    """Pick the most recent CAMS run that's likely to be available.

    CAMS publishes 00Z and 12Z runs with ~10h delay (00Z cycle ready by 10:00 UTC,
    12Z ready by 22:00 UTC). We pick whichever cycle is most recently ready as of
    wall-clock 'now', NOT what's nominally most recent — that prevents the cron
    from chasing a cycle that hasn't been published yet.
    """
    now = datetime.now(timezone.utc)
    # If past 10:00 UTC today → today's 00Z is ready.
    # If past 22:00 UTC today → today's 12Z is ready (newer than 00Z).
    # Else → yesterday's 12Z.
    if now.hour >= 22:
        return now.strftime("%Y-%m-%d"), "12:00"
    if now.hour >= 10:
        return now.strftime("%Y-%m-%d"), "00:00"
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday, "12:00"


def retrieve_grib(run_date: str, run_time: str, dest: pathlib.Path):
    """Pull the GRIB file from CAMS for the given run."""
    request = {
        "variable": [s[0] for s in SPECIES],
        "date": [f"{run_date}/{run_date}"],
        "time": [run_time],
        "leadtime_hour": ["0"],   # T+0 nowcast
        "type": ["forecast"],
        "data_format": "grib",
    }
    print(f"[CAMS] requesting {run_date} {run_time} T+0", flush=True)
    client = cdsapi.Client()
    client.retrieve("cams-global-atmospheric-composition-forecasts", request).download(str(dest))
    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"[CAMS] retrieved {size_mb:.1f} MB", flush=True)


def parse_and_slim(grib_path: pathlib.Path, run_date: str, run_time: str) -> dict:
    """Parse GRIB → slim JSON dict."""
    print("[CAMS] opening GRIB with cfgrib", flush=True)
    ds = xr.open_dataset(grib_path, engine="cfgrib")

    # Each AOD variable comes through cfgrib as a separate DataArray with names
    # like 'aod550' or species-specific names; let's introspect.
    print(f"[CAMS] variables in dataset: {list(ds.data_vars)}", flush=True)
    print(f"[CAMS] dims: {dict(ds.dims)}", flush=True)

    lat = ds.latitude.values
    lng = ds.longitude.values

    # Map data_vars (which cfgrib labels via GRIB short names) to our SPECIES tags.
    # ECMWF uses short names like 'aod550' for total AOD, but per-species uses
    # 'omaod550', 'duaod550', 'ssaod550', 'bcaod550', 'suaod550', 'niaod550',
    # 'amaod550' — verify by introspection and match.
    short_to_tag = {
        "ssaod550": "sea_salt",
        "duaod550": "dust",
        "omaod550": "organic_matter",
        "bcaod550": "black_carbon",
        "suaod550": "sulphate",
        "niaod550": "nitrate",
        "amaod550": "ammonium",
    }

    species_arrays = {}
    for var_name in ds.data_vars:
        tag = short_to_tag.get(var_name)
        if not tag:
            print(f"[CAMS] WARN unknown var {var_name}, skipping", flush=True)
            continue
        arr = ds[var_name].values
        if arr.ndim > 2:
            arr = arr.squeeze()
        species_arrays[tag] = arr.astype(np.float32)

    if not species_arrays:
        sys.exit("FATAL: no recognized aerosol species variables in GRIB output")

    # Order tags as in SPECIES (so the output 'grid' rows have predictable column order).
    ordered_tags = [t for _, t in SPECIES if t in species_arrays]

    # Optional downsample.
    if DOWNSAMPLE_FACTOR > 1:
        lat = lat[::DOWNSAMPLE_FACTOR]
        lng = lng[::DOWNSAMPLE_FACTOR]
        for tag in ordered_tags:
            species_arrays[tag] = species_arrays[tag][::DOWNSAMPLE_FACTOR, ::DOWNSAMPLE_FACTOR]

    ny, nx = species_arrays[ordered_tags[0]].shape

    # Per-species stats — useful for frontend color-scale auto-tuning.
    stats = {}
    for tag in ordered_tags:
        vals = species_arrays[tag]
        # Some cells may be NaN (over high topography or polar gaps); ignore.
        finite = vals[np.isfinite(vals)]
        if finite.size == 0:
            stats[tag] = {"max": 0.0, "mean": 0.0, "p95": 0.0}
            continue
        stats[tag] = {
            "max":  float(round(float(finite.max()), 4)),
            "mean": float(round(float(finite.mean()), 4)),
            "p95":  float(round(float(np.percentile(finite, 95)), 4)),
        }

    # Build the grid. Drop cells where all species < MIN_AOD_THRESHOLD.
    grid = []
    for j in range(ny):
        for i in range(nx):
            row = []
            keep = False
            for tag in ordered_tags:
                v = float(species_arrays[tag][j, i])
                if not np.isfinite(v):
                    v = 0.0
                v = round(v, 4)
                if v >= MIN_AOD_THRESHOLD:
                    keep = True
                row.append(v)
            if keep:
                grid.append([float(round(float(lat[j]), 2)), float(round(float(lng[i]), 2)), *row])

    print(f"[CAMS] grid cells kept: {len(grid)} / {ny * nx} "
          f"({100 * len(grid) / max(1, ny * nx):.1f}%)", flush=True)

    out = {
        "updated":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_time":     f"{run_date}T{run_time}:00Z",
        "resolution_deg": float(round(float(abs(lat[1] - lat[0])), 2)),
        "species":        ordered_tags,
        "stats":          stats,
        "grid":           grid,
        "attribution":    "Generated using Copernicus Atmosphere Monitoring Service Information 2026.",
    }
    return out


def write_json(payload: dict):
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    print(f"[CAMS] wrote {OUT_PATH} ({size_mb:.2f} MB)", flush=True)


def main():
    setup_cdsapi_creds()
    run_date, run_time = pick_run_date()
    with tempfile.TemporaryDirectory() as tmp:
        grib = pathlib.Path(tmp) / "cams.grib"
        retrieve_grib(run_date, run_time, grib)
        payload = parse_and_slim(grib, run_date, run_time)
    write_json(payload)


if __name__ == "__main__":
    main()
