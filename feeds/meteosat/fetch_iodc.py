#!/usr/bin/env python3
"""Meteosat-9 Indian Ocean (IODC) fire detections: India, Central Asia, eastern
Middle East and the western edge of South-East Asia, from EUMETSAT's LSA SAF.

Product: MSG-IODC FRP-PIXEL ListProduct (Meteosat-9 SEVIRI at 45.5°E, 15-min slots,
FRP in MW; HDF5, one file per slot, posted in hourly batches ~50-75 min late).
Access: directory listings are public; downloads need a free LSA SAF login
(HTTP basic auth, env LSASAF_USER / LSASAF_PASSWORD). Licence: CC BY 4.0, EUMETSAT.

Longitude clip: only detections at or east of 60°E are kept. West of that,
Meteosat-12 (fetch_meteosat.py, same feed) already covers Africa and the Middle East
with a sharper, more frequent FCI view, so keeping both would show every fire twice.

Each dataset is a flat array read as value / SCALING_FACTOR + OFFSET (attributes on
the dataset), with MISSING_VALUE masked. Fields used: LATITUDE, LONGITUDE, FRP,
FIRE_CONFIDENCE (0-1), BT_MIR (K) and ACQTIME (HHMM of the scan line, UTC).

Downloads the newest WINDOW_SLOTS slots (cached in _cache_iodc/, never published; a
separate folder because fetch_meteosat.py prunes everything in _cache/ that is not
its own), merges repeat sightings, deconflicts against the industrial-sites
reference, and writes:
  data/iodc_fire.json            the whole clipped disk, GOES feed shape
  data/iodc/<region>.json        the same split by region (firms feed regions)
"""
import base64
import bz2
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np

import geo_common as gc
from global_out import region_of

BASE = 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MSG-IODC/FRP-PIXEL/HDF5'
PREFIX = 'HDF5_LSASAF_MSG-IODC_FRP-PIXEL-ListProduct_IODC-Disk_'
NAME_RE = re.compile(re.escape(PREFIX) + r'(\d{12})')
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
WINDOW_SLOTS = 4                  # 4 x 15 min = one hourly batch
LNG_MIN = 60.0                    # west of this, Meteosat-12 covers it (see docstring)
CACHE = '_cache_iodc'
OUT = 'data/iodc_fire.json'
REGION_DIR = 'data/iodc'
ALWAYS_REGIONS = ('middle-east', 'asia')   # always written, so their URLs never go stale


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
            print(f'[iodc] listing {day:%Y-%m-%d} failed: {e}', file=sys.stderr)
            continue
        names += sorted(set(NAME_RE.findall(html)))
    return sorted(set(names))[-WINDOW_SLOTS:]


def _attr(ds, key, default):
    v = ds.attrs.get(key)
    return default if v is None else float(np.ravel(v)[0])


def _col(f, name):
    """Scaled float array with MISSING_VALUE -> NaN; all NaN if the dataset is absent."""
    if name not in f:
        n = f['LATITUDE'].shape[0]
        return np.full(n, np.nan)
    ds = f[name]
    raw = np.ravel(ds[()])
    out = raw.astype(float)
    miss = ds.attrs.get('MISSING_VALUE')
    if miss is not None:
        out[raw == np.ravel(miss)[0]] = np.nan
    return out / (_attr(ds, 'SCALING_FACTOR', 1.0) or 1.0) + _attr(ds, 'OFFSET', 0.0)


def _read(path, slot):
    with h5py.File(path, 'r') as f:
        lat, lng, frp = _col(f, 'LATITUDE'), _col(f, 'LONGITUDE'), _col(f, 'FRP')
        conf, bt = _col(f, 'FIRE_CONFIDENCE'), _col(f, 'BT_MIR')
        acq = np.ravel(f['ACQTIME'][()]) if 'ACQTIME' in f else np.full(lat.size, -999)
    ref = datetime.strptime(slot, '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
    midnight = ref.replace(hour=0, minute=0)
    scan = gc.iso(ref)
    out = []
    for i in range(lat.size):
        if not (np.isfinite(lat[i]) and np.isfinite(lng[i])) or lng[i] < LNG_MIN:
            continue
        hh, mm = divmod(int(acq[i]), 100)
        if 0 <= hh < 24 and 0 <= mm < 60:
            obs = midnight + timedelta(hours=hh, minutes=mm)
            if obs < ref - timedelta(hours=1):          # 23:45 slot finishing after midnight
                obs += timedelta(days=1)
        else:
            obs = ref
        out.append({'lat': float(lat[i]), 'lng': float(lng[i]),
                    'frp': float(frp[i]) if np.isfinite(frp[i]) else None,
                    'tempK': float(bt[i]) if np.isfinite(bt[i]) else None,
                    'conf': float(conf[i]) if np.isfinite(conf[i]) else None,
                    'sat': 'MSG-IODC', 'obs': obs, 'scan': scan})
    return out, scan


def main() -> int:
    headers = _auth_header()
    if headers is None:
        print('[iodc] LSASAF_USER / LSASAF_PASSWORD not set; skipping')
        return 0
    now = gc.now_utc()
    slots = _list_slots(now)
    if not slots:
        print('[iodc] no slots listed', file=sys.stderr)
        return 1
    os.makedirs(CACHE, exist_ok=True)
    keep = {f'{PREFIX}{s}.h5' for s in slots}
    for name in os.listdir(CACHE):
        if name not in keep:
            os.remove(os.path.join(CACHE, name))

    dets, scans = [], set()
    for slot in slots:
        name = f'{PREFIX}{slot}'
        path = os.path.join(CACHE, name + '.h5')
        if not os.path.exists(path):
            try:
                data = _get(f'{BASE}/{slot[:4]}/{slot[4:6]}/{slot[6:8]}/{name}', headers)
                if data[:3] == b'BZh':
                    data = bz2.decompress(data)
            except Exception as e:
                print(f'[iodc] download {name} failed: {e}', file=sys.stderr)
                continue
            with open(path + '.part', 'wb') as f:
                f.write(data)
            os.replace(path + '.part', path)
        try:
            rows, scan = _read(path, slot)
        except Exception as e:
            print(f'[iodc] parse {name} failed: {e}', file=sys.stderr)
            os.remove(path)
            continue
        dets += rows
        scans.add(scan)
    if not scans:
        return 1

    merged = gc.merge_window(dets, now)
    shown, suppressed, meta = gc.deconflict(merged, min_buffer_m=4000)   # SEVIRI 3 km nadir, ~4 km over India
    common = dict(now=now, scans=scans,
                  product=f'LSA SAF MSG-IODC FRP-PIXEL, Meteosat-9 SEVIRI, 15-min slots, FRP in MW, '
                          f'longitudes >= {LNG_MIN:g}°E only',
                  attribution='EUMETSAT LSA SAF (IPMA)', licence='CC BY 4.0, EUMETSAT')
    gc.write_json(OUT, gc.payload(shown, suppressed, meta,
                                  coverage=f'Meteosat-9 IODC (45.5°E), east of {LNG_MIN:g}°E', **common))

    by_region, sup_region = {}, {}
    for d in shown:
        by_region.setdefault(region_of(d['lat'], d['lng']), []).append(d)
    for s in suppressed:
        sup_region.setdefault(region_of(s[0], s[1]), []).append(s)
    regions = sorted(set(by_region) | set(sup_region) | set(ALWAYS_REGIONS))
    for region in regions:
        gc.write_json(os.path.join(REGION_DIR, f'{region}.json'),
                      gc.payload(by_region.get(region, []), sup_region.get(region, []), meta,
                                 coverage=region, **common))
    for name in os.listdir(REGION_DIR):                  # drop regions that emptied out
        if name.endswith('.json') and name[:-5] not in regions:
            os.remove(os.path.join(REGION_DIR, name))
    print(f'[iodc] {len(dets)} raw pixels >= {LNG_MIN:g}°E, {len(scans)} slots -> {len(shown)} shown, '
          f'{len(suppressed)} suppressed {meta["by_flag"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
