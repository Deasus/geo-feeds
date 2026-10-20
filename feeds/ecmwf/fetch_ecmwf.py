"""
ECMWF IFS 0.25 degree 10 m wind for the fire regions outside the US (2026-10-16).

Wind split across feeds:
  * HRRR 3 km             -> CONUS + Alaska            (hrrr feed)
  * ECMWF IFS 0.25 deg    -> the regional windows below (this script)
  * GFS 1 deg             -> everywhere else + the 850/700/500 mb levels (fetch_gfs.py)

ECMWF's 0.25 degree surface wind is ~4x finer than GFS 1 degree, which matters for
fire behaviour; it is only cut out where fires burn at scale, so the files stay
small (~3-4 MB per run in total instead of ~10 MB for one global surface field).

Source: ECMWF open data (IFS HRES, step 0 of the latest run), licensed CC BY 4.0;
credit "(c) ECMWF, CC BY 4.0" wherever the data is shown.
https://www.ecmwf.int/en/forecasts/datasets/open-data

Output (cambecc earth JSON, identical shape to the GFS / HRRR files the client reads):
  data/ecmwf/current-wind-10m-ecmwf-<region>.json   [ {header,data:U}, {header,data:V} ]
  data/ecmwf/meta.json                              run time, regions, bboxes, attribution
Rows run north -> south, columns west -> east; la1 = north edge, lo1 = west edge.
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

OUT_DIR = os.path.join('data', 'ecmwf')
GRIB = os.path.join(OUT_DIR, '_ecmwf_10m.grib2')

# key -> (min_lat, max_lat, min_lng, max_lng). Chosen to cover where large fires burn
# outside HRRR coverage; the client prefers HRRR inside the US, then these, then GFS.
REGIONS = {
    'canada':        (41.0, 72.0, -141.0, -52.0),
    'europe':        (34.0, 72.0, -25.0, 45.0),
    'south-america': (-56.0, 13.0, -92.0, -30.0),
    'africa':        (-36.0, 37.5, -26.0, 52.0),
    'asia':          (-11.0, 60.0, 45.0, 150.0),
    'oceania':       (-50.0, 0.0, 112.0, 180.0),
}
SOURCES = ['ecmwf', 'aws', 'azure', 'google']   # ECMWF first, then the cloud mirrors


def download():
    from ecmwf.opendata import Client
    last = None
    for src in SOURCES:
        try:
            client = Client(source=src, model='ifs', resol='0p25')
            res = client.retrieve(type='fc', step=0, param=['10u', '10v'], target=GRIB)
            print(f'  downloaded from {src}: run {res.datetime}')
            return res.datetime, src
        except Exception as e:       # one mirror down / not yet populated -> next
            last = e
            print(f'  {src} failed: {e}')
    raise RuntimeError(f'all ECMWF open-data sources failed: {last}')


def decode():
    """Return (lats[ny], lons[nx], u[ny,nx], v[ny,nx]) on a regular lat/lon grid,
    lats descending (north first), lons ascending in -180..180."""
    import eccodes
    fields = {}
    meta = None
    with open(GRIB, 'rb') as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, 'shortName')
                ni, nj = eccodes.codes_get(gid, 'Ni'), eccodes.codes_get(gid, 'Nj')
                lo1 = eccodes.codes_get(gid, 'longitudeOfFirstGridPointInDegrees')
                la1 = eccodes.codes_get(gid, 'latitudeOfFirstGridPointInDegrees')
                dx = eccodes.codes_get(gid, 'iDirectionIncrementInDegrees')
                dy = eccodes.codes_get(gid, 'jDirectionIncrementInDegrees')
                vals = eccodes.codes_get_values(gid).reshape(nj, ni)
                fields[short] = vals
                meta = (ni, nj, lo1, la1, dx, dy)
            finally:
                eccodes.codes_release(gid)
    if '10u' not in fields or '10v' not in fields:
        raise RuntimeError(f'GRIB missing 10u/10v (got {sorted(fields)})')
    ni, nj, lo1, la1, dx, dy = meta
    lats = la1 - dy * np.arange(nj)               # scanning north -> south
    lons = lo1 + dx * np.arange(ni)
    lons = np.where(lons > 180.0, lons - 360.0, lons)
    order = np.argsort(lons, kind='stable')         # normalise to -180..180 ascending
    u, v = fields['10u'][:, order], fields['10v'][:, order]
    return lats, lons[order], u, v


def cut(lats, lons, u, v, bbox):
    a, b, c, d = bbox
    ri = np.where((lats >= a - 1e-6) & (lats <= b + 1e-6))[0]
    ci = np.where((lons >= c - 1e-6) & (lons <= d + 1e-6))[0]
    sub_u = u[np.ix_(ri, ci)]
    sub_v = v[np.ix_(ri, ci)]
    return lats[ri], lons[ci], sub_u, sub_v


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    run_dt, src = download()
    lats, lons, u, v = decode()
    dx = float(abs(lons[1] - lons[0]))
    dy = float(abs(lats[0] - lats[1]))
    ref = run_dt.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
    meta = {'generated_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'ref_time': ref, 'step_hours': 0, 'model': 'ECMWF IFS HRES open data', 'resolution_deg': dx,
            'mirror': src, 'attribution': '(c) ECMWF, CC BY 4.0', 'regions': {}}
    for key, bbox in REGIONS.items():
        rl, rn, ru, rv = cut(lats, lons, u, v, bbox)
        header = {'nx': int(len(rn)), 'ny': int(len(rl)), 'lo1': float(rn[0]), 'la1': float(rl[0]),
                  'lo2': float(rn[-1]), 'la2': float(rl[-1]), 'dx': dx, 'dy': dy,
                  'refTime': ref, 'forecastTime': 0, 'surface1Type': 103, 'surface1Value': 10.0,
                  'centerName': 'ECMWF', 'source': 'ECMWF IFS 0.25 open data (CC BY 4.0)'}
        out = [
            {'header': {**header, 'parameterCategory': 2, 'parameterNumber': 2,
                        'parameterNumberName': 'U-component_of_wind', 'parameterUnit': 'm.s-1'},
             'data': [round(float(x), 1) for x in ru.ravel()]},
            {'header': {**header, 'parameterCategory': 2, 'parameterNumber': 3,
                        'parameterNumberName': 'V-component_of_wind', 'parameterUnit': 'm.s-1'},
             'data': [round(float(x), 1) for x in rv.ravel()]},
        ]
        path = os.path.join(OUT_DIR, f'current-wind-10m-ecmwf-{key}.json')
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(out, f, separators=(',', ':'))
        os.replace(tmp, path)
        meta['regions'][key] = {'bbox': list(bbox), 'file': os.path.basename(path), 'nx': header['nx'], 'ny': header['ny']}
        print(f'  {key}: {header["nx"]}x{header["ny"]} -> {os.path.getsize(path) / 1024:.0f} KB')
    with open(os.path.join(OUT_DIR, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    try:
        os.remove(GRIB)
    except OSError:
        pass
    print(f'ECMWF {ref} ({src}) -> {len(REGIONS)} regional 0.25 deg wind files')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print(f'::error::ECMWF wind failed: {e}')
        sys.exit(1)
