"""
Fetch latest GFS wind data from NOAA NOMADS and convert to JSON.
Outputs JSON in the same format as nullschool/cambecc earth project.

Levels fetched:
  • surface (10 m AGL) — near-surface wind for fire behavior
  • 850 mb  (~5,000 ft AMSL) — smoke transport, low-level jets
  • 700 mb  (~10,000 ft AMSL) — plume rise, mid-level flow
  • 500 mb  (~18,000 ft AMSL) — long-range spotting, upper flow

All levels are queried by the frontend's query_wind tool via a level_mb
parameter. Fire behavior analysts use the aloft levels for smoke
transport forecast and long-range spotting hazard.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
import requests

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

NOMADS_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_1p00.pl"

def get_gfs_cycles():
    """Yield recent GFS cycles to try, newest first."""
    now = datetime.now(timezone.utc)
    for hours_ago in range(4, 30, 6):
        cycle_time = now - timedelta(hours=hours_ago)
        cycle_hour = (cycle_time.hour // 6) * 6
        cycle_dt = cycle_time.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
        yield cycle_dt

def download_grib(cycle_dt, level_param, output_path):
    """Download filtered GFS GRIB2 from NOMADS."""
    date_str = cycle_dt.strftime("%Y%m%d")
    hour_str = cycle_dt.strftime("%H")
    params = {
        "file": f"gfs.t{hour_str}z.pgrb2.1p00.f000",
        level_param: "on",
        "var_UGRD": "on",
        "var_VGRD": "on",
        "dir": f"/gfs.{date_str}/{hour_str}/atmos"
    }
    print(f"  Trying cycle {date_str}/{hour_str}z...")
    try:
        resp = requests.get(NOMADS_BASE, params=params, timeout=30)
        if resp.status_code == 200 and len(resp.content) > 500:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            print(f"  Downloaded {len(resp.content):,} bytes")
            return True
        print(f"  HTTP {resp.status_code}, {len(resp.content)} bytes — skipping")
    except Exception as e:
        print(f"  Error: {e}")
    return False

def grib2_to_json_cfgrib(grib_path):
    """Convert GRIB2 to JSON using cfgrib (xarray backend)."""
    try:
        import cfgrib
        import numpy as np
        
        ds = cfgrib.open_datasets(grib_path)
        
        u_data = v_data = None
        header = {}
        
        for d in ds:
            for var_name in d.data_vars:
                var = d[var_name]
                if 'u' in var_name.lower() or var_name in ('u10', 'u'):
                    u_data = var.values.flatten()
                    lats = var.coords['latitude'].values
                    lons = var.coords['longitude'].values
                    header = {
                        "nx": len(lons),
                        "ny": len(lats),
                        "lo1": float(lons[0]),
                        "la1": float(lats[0]),
                        "lo2": float(lons[-1]),
                        "la2": float(lats[-1]),
                        "dx": float(abs(lons[1] - lons[0])),
                        "dy": float(abs(lats[1] - lats[0]))
                    }
                elif 'v' in var_name.lower() or var_name in ('v10', 'v'):
                    v_data = var.values.flatten()
        
        if u_data is not None and v_data is not None:
            return [
                {"header": {**header, "parameterNumber": 2, "parameterNumberName": "U-component_of_wind", "parameterUnit": "m.s-1"},
                 "data": [round(float(x), 2) for x in u_data]},
                {"header": {**header, "parameterNumber": 3, "parameterNumberName": "V-component_of_wind", "parameterUnit": "m.s-1"},
                 "data": [round(float(x), 2) for x in v_data]}
            ]
    except ImportError:
        print("  cfgrib not available")
    except Exception as e:
        print(f"  cfgrib error: {e}")
    return None

def grib2_to_json_eccodes(grib_path):
    """Convert GRIB2 to JSON using eccodes."""
    try:
        import eccodes
        
        records = []
        with open(grib_path, "rb") as f:
            while True:
                msgid = eccodes.codes_grib_new_from_file(f)
                if msgid is None:
                    break
                try:
                    param_name = eccodes.codes_get(msgid, "shortName")
                    nx = eccodes.codes_get(msgid, "Ni")
                    ny = eccodes.codes_get(msgid, "Nj")
                    lo1 = eccodes.codes_get(msgid, "longitudeOfFirstGridPointInDegrees")
                    la1 = eccodes.codes_get(msgid, "latitudeOfFirstGridPointInDegrees")
                    lo2 = eccodes.codes_get(msgid, "longitudeOfLastGridPointInDegrees")
                    la2 = eccodes.codes_get(msgid, "latitudeOfLastGridPointInDegrees")
                    dx = eccodes.codes_get(msgid, "iDirectionIncrementInDegrees")
                    dy = eccodes.codes_get(msgid, "jDirectionIncrementInDegrees")
                    values = eccodes.codes_get_values(msgid)
                    
                    param_num = 2 if param_name in ("10u", "u") else 3 if param_name in ("10v", "v") else -1
                    if param_num > 0:
                        records.append({
                            "header": {
                                "parameterNumber": param_num,
                                "parameterNumberName": f"{'U' if param_num==2 else 'V'}-component_of_wind",
                                "parameterUnit": "m.s-1",
                                "nx": nx, "ny": ny,
                                "lo1": lo1, "la1": la1, "lo2": lo2, "la2": la2,
                                "dx": dx, "dy": dy
                            },
                            "data": [round(float(v), 2) for v in values]
                        })
                finally:
                    eccodes.codes_release(msgid)
        
        if len(records) >= 2:
            u = next((r for r in records if r["header"]["parameterNumber"] == 2), None)
            v = next((r for r in records if r["header"]["parameterNumber"] == 3), None)
            if u and v:
                return [u, v]
    except ImportError:
        print("  eccodes not available")
    except Exception as e:
        print(f"  eccodes error: {e}")
    return None

def grib2_to_json_wgrib2(grib_path):
    """Convert GRIB2 to JSON using wgrib2 command line tool."""
    import subprocess
    import csv
    import io
    import numpy as np
    
    try:
        # Check if wgrib2 is available
        subprocess.run(["wgrib2", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("  wgrib2 not available")
        return None
    
    try:
        result = subprocess.run(
            ["wgrib2", grib_path, "-csv", "/dev/stdout"],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            print(f"  wgrib2 error: {result.stderr}")
            return None
        
        # Parse CSV output
        reader = csv.reader(io.StringIO(result.stdout))
        u_vals = {}
        v_vals = {}
        
        for row in reader:
            if len(row) < 7:
                continue
            var_name = row[2].strip().strip('"')
            lat = float(row[4])
            lon = float(row[5])
            val = float(row[6])
            
            key = (lat, lon)
            if "UGRD" in var_name:
                u_vals[key] = val
            elif "VGRD" in var_name:
                v_vals[key] = val
        
        if not u_vals or not v_vals:
            print("  No UGRD/VGRD data found in wgrib2 output")
            return None
        
        # Build grid
        all_lats = sorted(set(k[0] for k in u_vals.keys()), reverse=True)
        all_lons = sorted(set(k[1] for k in u_vals.keys()))
        ny, nx = len(all_lats), len(all_lons)
        
        u_data = []
        v_data = []
        for lat in all_lats:
            for lon in all_lons:
                u_data.append(round(u_vals.get((lat, lon), 0), 2))
                v_data.append(round(v_vals.get((lat, lon), 0), 2))
        
        header = {
            "nx": nx, "ny": ny,
            "lo1": all_lons[0], "la1": all_lats[0],
            "lo2": all_lons[-1], "la2": all_lats[-1],
            "dx": round(all_lons[1] - all_lons[0], 2) if nx > 1 else 1,
            "dy": round(all_lats[0] - all_lats[1], 2) if ny > 1 else 1
        }
        
        return [
            {"header": {**header, "parameterNumber": 2, "parameterNumberName": "U-component_of_wind", "parameterUnit": "m.s-1"}, "data": u_data},
            {"header": {**header, "parameterNumber": 3, "parameterNumberName": "V-component_of_wind", "parameterUnit": "m.s-1"}, "data": v_data}
        ]
    except Exception as e:
        print(f"  wgrib2 conversion error: {e}")
    return None

def grib2_to_json_pygrib(grib_path):
    """Convert GRIB2 to JSON using pygrib."""
    try:
        import pygrib
        
        grbs = pygrib.open(grib_path)
        records = []
        
        for grb in grbs:
            if grb.shortName in ('10u', '10v', 'u', 'v'):
                values = grb.values.flatten()
                lats, lons = grb.latlons()
                lat_col = lats[:, 0]
                lon_row = lons[0, :]
                
                param_num = 2 if grb.shortName in ('10u', 'u') else 3
                records.append({
                    "header": {
                        "parameterNumber": param_num,
                        "parameterNumberName": f"{'U' if param_num==2 else 'V'}-component_of_wind",
                        "parameterUnit": "m.s-1",
                        "nx": len(lon_row), "ny": len(lat_col),
                        "lo1": float(lon_row[0]), "la1": float(lat_col[0]),
                        "lo2": float(lon_row[-1]), "la2": float(lat_col[-1]),
                        "dx": float(abs(lon_row[1] - lon_row[0])),
                        "dy": float(abs(lat_col[1] - lat_col[0]))
                    },
                    "data": [round(float(v), 2) for v in values]
                })
        
        grbs.close()
        
        if len(records) >= 2:
            u = next((r for r in records if r["header"]["parameterNumber"] == 2), None)
            v = next((r for r in records if r["header"]["parameterNumber"] == 3), None)
            if u and v:
                return [u, v]
    except ImportError:
        print("  pygrib not available")
    except Exception as e:
        print(f"  pygrib error: {e}")
    return None

def convert_grib_to_json(grib_path, json_path):
    """Try multiple GRIB2 conversion methods."""
    print(f"  Converting GRIB2 to JSON...")
    
    # Try each method in order of preference
    for name, func in [
        ("cfgrib", grib2_to_json_cfgrib),
        ("eccodes", grib2_to_json_eccodes),
        ("pygrib", grib2_to_json_pygrib),
        ("wgrib2", grib2_to_json_wgrib2),
    ]:
        print(f"  Trying {name}...")
        result = func(grib_path)
        if result:
            with open(json_path, "w") as f:
                json.dump(result, f, separators=(",", ":"))
            size_kb = os.path.getsize(json_path) / 1024
            print(f"  ✓ Converted with {name}: {size_kb:.0f} KB, grid {result[0]['header']['nx']}×{result[0]['header']['ny']}")
            return True
    
    print(f"  ✗ All conversion methods failed")
    return False

def fetch_level(level_param, output_name, level_label):
    """Fetch and convert one level of wind data."""
    print(f"\n{'='*50}")
    print(f"Fetching {level_label} wind...")
    
    grib_path = os.path.join(DATA_DIR, f"{output_name}.grib2")
    json_path = os.path.join(DATA_DIR, f"{output_name}.json")
    
    for cycle_dt in get_gfs_cycles():
        if download_grib(cycle_dt, level_param, grib_path):
            if convert_grib_to_json(grib_path, json_path):
                # Clean up GRIB2 file — don't commit binary files
                os.remove(grib_path)
                return True
            else:
                print("  Conversion failed, trying older cycle...")
    
    # Clean up any leftover GRIB2 file
    if os.path.exists(grib_path):
        os.remove(grib_path)
    
    print(f"  ✗ Failed to fetch {level_label}")
    return False

def main():
    print("=" * 60)
    print("geo-feeds Wind Data Updater")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    
    # Install available GRIB libraries
    import subprocess
    print("\nInstalling GRIB libraries...")
    for pkg in ["cfgrib", "eccodes", "pygrib"]:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], 
                         capture_output=True, timeout=60)
            print(f"  ✓ {pkg}")
        except Exception:
            print(f"  ✗ {pkg} (skipped)")
    
    results = {}
    
    if fetch_level("lev_10_m_above_ground", "current-wind-surface", "Surface (10m)"):
        results["surface"] = "ok"
    else:
        results["surface"] = "failed"

    if fetch_level("lev_850_mb", "current-wind-850mb", "850mb (~5,000 ft AMSL, smoke)"):
        results["850mb"] = "ok"
    else:
        results["850mb"] = "failed"

    if fetch_level("lev_700_mb", "current-wind-700mb", "700mb (~10,000 ft AMSL, plume rise)"):
        results["700mb"] = "ok"
    else:
        results["700mb"] = "failed"

    if fetch_level("lev_500_mb", "current-wind-500mb", "500mb (~18,000 ft AMSL, long-range spotting)"):
        results["500mb"] = "ok"
    else:
        results["500mb"] = "failed"

    # Write metadata
    meta = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "levels": results,
        "level_details": {
            "surface": {"height_agl_m": 10, "typical_use": "fire behavior / surface flow"},
            "850mb":   {"altitude_amsl_ft": 5000,  "typical_use": "smoke transport, low-level jet"},
            "700mb":   {"altitude_amsl_ft": 10000, "typical_use": "plume rise, mid-level flow"},
            "500mb":   {"altitude_amsl_ft": 18000, "typical_use": "long-range spotting, upper flow"},
        },
        "source": "NOAA NOMADS GFS 1p00 (analysis f000)",
    }
    with open(os.path.join(DATA_DIR, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    
    print(f"\n{'='*60}")
    print("Results:")
    for level, status in results.items():
        print(f"  {'✓' if status=='ok' else '✗'} {level}: {status}")
    
    # Verify no GRIB2 files are left (they shouldn't be committed)
    for f in os.listdir(DATA_DIR):
        if f.endswith('.grib2'):
            os.remove(os.path.join(DATA_DIR, f))
            print(f"  Cleaned up: {f}")

if __name__ == "__main__":
    main()
