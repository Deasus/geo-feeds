#!/usr/bin/env python3
"""Himawari-9 fire hotspots over Australia, from Geoscience Australia's DEA Hotspots.

Source: DEA Hotspots public WFS (no account), layer public:hotspots_three_days.
Only the geostationary product (BRIGHT AHI on Himawari-9, ~10-min scans) is kept;
the same service also re-serves VIIRS/MODIS, which the firms feed already covers.
Licence: CC BY 4.0, Geoscience Australia. GA notes the data is not for
safety-of-life decisions.

Writes data/himawari_fire.json in the GOES feed's shape, deconflicted against the
industrial-sites reference (see _shared/geo_common.py).
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import geo_common as gc

WFS = 'https://hotspots.dea.ga.gov.au/geoserver/public/wfs'
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
WINDOW_MIN = 60          # DEA posts scans ~25-30 min after the slot; 60 min keeps ~3-4 scans
OUT = 'data/himawari_fire.json'


def _parse_time(s):
    return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)


def main() -> int:
    now = gc.now_utc()
    since = gc.iso(now - timedelta(minutes=WINDOW_MIN))
    params = {
        'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature',
        'typeName': 'public:hotspots_three_days', 'outputFormat': 'application/json',
        # Always CQL-filter: the unfiltered layer is ~200 MB.
        'CQL_FILTER': f"product = 'BRIGHT AHI' AND start_dt >= '{since}'",
    }
    req = urllib.request.Request(WFS + '?' + urllib.parse.urlencode(params), headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            fc = json.loads(r.read())
    except Exception as e:
        print(f'[himawari] DEA WFS failed: {e}', file=sys.stderr)
        return 1
    feats = fc.get('features') or []

    dets, scans = [], set()
    for f in feats:
        p = f.get('properties') or {}
        lat, lng = p.get('latitude'), p.get('longitude')
        if lat is None or lng is None or not p.get('start_dt'):
            continue
        scan = p['start_dt'][:19] + 'Z'
        scans.add(scan)
        conf = p.get('confidence')
        dets.append({'lat': float(lat), 'lng': float(lng), 'frp': p.get('power'),
                     'tempK': p.get('temp_kelvin'),
                     'conf': (float(conf) / 100.0) if conf is not None else None,
                     'sat': 'H9', 'obs': _parse_time(p.get('datetime') or p['start_dt']),
                     'scan': scan})

    merged = gc.merge_window(dets, now)
    shown, suppressed, meta = gc.deconflict(merged, min_buffer_m=2500)   # AHI 2 km pixel
    out = gc.payload(shown, suppressed, meta, now=now, scans=scans,
                     product='Himawari-9 AHI fire hotspots (BRIGHT AHI 1.86), 2 km, ~10-min scans',
                     coverage='Australia',
                     attribution='Geoscience Australia, Digital Earth Australia Hotspots',
                     licence='CC BY 4.0; not for safety-of-life decisions (Geoscience Australia)')
    gc.write_json(OUT, out)
    print(f'[himawari] {len(feats)} raw AHI rows, {len(scans)} scans -> {len(shown)} shown, '
          f'{len(suppressed)} suppressed {meta["by_flag"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
