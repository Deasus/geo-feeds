#!/usr/bin/env python3
"""Meteosat Third Generation fire detections (Europe, Africa, Middle East, eastern
South America), from EUMETSAT's LSA SAF.

Product: LSA-509 MTG FRP-PIXEL (Meteosat-12 FCI full disk, 10-min slots, FRP in MW).
Access: directory listings are public; downloads need a free LSA SAF login
(HTTP basic auth, env LSASAF_USER / LSASAF_PASSWORD). Licence: CC BY 4.0, EUMETSAT.

Downloads the newest WINDOW_SLOTS slots (cached in _cache/, never published), merges
repeat sightings, deconflicts against the industrial-sites reference (gas flares
matter a great deal here), and writes:
  data/meteosat_fire.json            the whole disk, GOES feed shape
  data/meteosat/<region>.json        the same split by region (firms feed regions)
"""
import base64
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import netCDF4

import geo_common as gc
from global_out import region_of

BASE = 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/NATIVE'
NAME_RE = re.compile(r'LSA-509_MTG_MTFRPPIXEL_MTG-FD_(\d{12})\.nc')
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
WINDOW_SLOTS = 4                  # 4 x 10 min, like the GOES feed's 30-min window
CACHE = '_cache'
OUT = 'data/meteosat_fire.json'
REGION_DIR = 'data/meteosat'


def _auth_header():
    user, pw = os.environ.get('LSASAF_USER', ''), os.environ.get('LSASAF_PASSWORD', '')
    if not user or not pw:
        return None
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{pw}'.encode()).decode()}


def _get(url, headers=None, timeout=90):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _list_slots(now):
    names = []
    for day in (now - timedelta(days=1), now):
        try:
            html = _get(f'{BASE}/{day:%Y/%m/%d}/').decode('utf-8', 'replace')
        except Exception as e:
            print(f'[meteosat] listing {day:%Y-%m-%d} failed: {e}', file=sys.stderr)
            continue
        names += sorted(set(NAME_RE.findall(html)))
    return sorted(set(names))[-WINDOW_SLOTS:]


def _read(path, slot):
    ds = netCDF4.Dataset(path)
    try:
        g = ds.groups['ListProduct']
        ref = datetime.strptime(ds.getncattr('image_reference_time')[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        col = lambda n: np.asarray(g.variables[n][:].filled(np.nan) if hasattr(g.variables[n][:], 'filled') else g.variables[n][:], dtype=float)
        lat, lng, frp = col('LATITUDE'), col('LONGITUDE'), col('FRP')
        conf, bt, acq = col('FIRE_CONFIDENCE'), col('BT_MIR'), col('ACQTIME')
    finally:
        ds.close()
    scan = f'{slot[:4]}-{slot[4:6]}-{slot[6:8]}T{slot[8:10]}:{slot[10:12]}:00Z'
    out = []
    for i in range(lat.size):
        if not (np.isfinite(lat[i]) and np.isfinite(lng[i])):
            continue
        obs = ref + timedelta(seconds=float(acq[i]) if np.isfinite(acq[i]) else 0.0)
        out.append({'lat': float(lat[i]), 'lng': float(lng[i]),
                    'frp': float(frp[i]) if np.isfinite(frp[i]) else None,
                    'tempK': float(bt[i]) if np.isfinite(bt[i]) else None,
                    'conf': float(conf[i]) if np.isfinite(conf[i]) else None,
                    'sat': 'MTG-I1', 'obs': obs, 'scan': scan})
    return out, scan


def main() -> int:
    headers = _auth_header()
    if headers is None:
        print('[meteosat] LSASAF_USER / LSASAF_PASSWORD not set; skipping')
        return 0
    now = gc.now_utc()
    slots = _list_slots(now)
    if not slots:
        print('[meteosat] no slots listed', file=sys.stderr)
        return 1
    os.makedirs(CACHE, exist_ok=True)
    keep = {f'LSA-509_MTG_MTFRPPIXEL_MTG-FD_{s}.nc' for s in slots}
    for name in os.listdir(CACHE):
        if name not in keep:
            os.remove(os.path.join(CACHE, name))

    dets, scans = [], set()
    for slot in slots:
        name = f'LSA-509_MTG_MTFRPPIXEL_MTG-FD_{slot}.nc'
        path = os.path.join(CACHE, name)
        if not os.path.exists(path):
            try:
                data = _get(f'{BASE}/{slot[:4]}/{slot[4:6]}/{slot[6:8]}/{name}', headers)
            except Exception as e:
                print(f'[meteosat] download {name} failed: {e}', file=sys.stderr)
                continue
            with open(path + '.part', 'wb') as f:
                f.write(data)
            os.replace(path + '.part', path)
        try:
            rows, scan = _read(path, slot)
        except Exception as e:
            print(f'[meteosat] parse {name} failed: {e}', file=sys.stderr)
            os.remove(path)
            continue
        dets += rows
        scans.add(scan)
    if not scans:
        return 1

    merged = gc.merge_window(dets, now)
    shown, suppressed, meta = gc.deconflict(merged, min_buffer_m=3000)   # FCI 1-3.5 km2 pixel
    common = dict(now=now, scans=scans,
                  product='LSA SAF MTG FRP-PIXEL (LSA-509), Meteosat-12 FCI, 10-min slots, FRP in MW',
                  attribution='EUMETSAT LSA SAF (IPMA)', licence='CC BY 4.0, EUMETSAT')
    gc.write_json(OUT, gc.payload(shown, suppressed, meta, coverage='Meteosat 0° full disk', **common))

    by_region, sup_region = {}, {}
    for d in shown:
        by_region.setdefault(region_of(d['lat'], d['lng']), []).append(d)
    for s in suppressed:
        sup_region.setdefault(region_of(s[0], s[1]), []).append(s)
    for region in sorted(set(by_region) | set(sup_region)):
        gc.write_json(os.path.join(REGION_DIR, f'{region}.json'),
                      gc.payload(by_region.get(region, []), sup_region.get(region, []), meta,
                                 coverage=region, **common))
    print(f'[meteosat] {len(dets)} raw pixels, {len(scans)} slots -> {len(shown)} shown, '
          f'{len(suppressed)} suppressed {meta["by_flag"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
