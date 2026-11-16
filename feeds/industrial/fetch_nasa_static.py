#!/usr/bin/env python3
"""NASA's own static land sources, from the last complete year of the FIRMS standard archive.

The near-real-time FIRMS rows the firms feed reads carry no "type"; NASA types each detection
only in the standard (archive) product: 0 presumed vegetation fire, 1 active volcano, 2 other
static land source, 3 offshore. This script turns NASA's type-2 detections of the previous
calendar year into reference points, with the method FIRMS documents for its Static Thermal
Anomalies mask (FIRMS FAQ, "Static Thermal Anomalies"): detection centroids summarised on a
400 m grid, every cell with 5 or more detections over the year kept. Here only detections NASA
itself typed 2 are counted, so recurring vegetation fire (farm burning, prescribed fire) is not
turned into a "known source".

Each kept cell becomes one source at the mean position of its detections. Its buffer (set in
build_industrial_sites.py) is 550 m: the cell's half-diagonal (283 m) plus half a VIIRS pixel
at nadir (265 m), because cells are filled by detection centres that sit up to half a pixel
from the source. The detection's own pixel is added when a detection is tested
(deconflict.effective_buffer_m).

Inputs: https://firms.modaps.eosdis.nasa.gov/data/country/<product>/<year>/<product>_<year>_<Country>.csv
        products viirs-snpp, viirs-jpss1, modis; countries NASA_STATIC_COUNTRIES (default United_States)
        NASA_STATIC_LOCAL_DIR = read <dir>/<product>_<year>_<Country>.csv instead (local runs)
        NASA_STATIC_YEAR = force the year (default: last year, else the year before if not yet published)
Output: data/_stage_nasa_static.json. A year already staged with the same countries is kept
without downloading again; a failed download keeps the previous stage (the merge carries it).
"""
import csv
import io
import json
import math
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = 'https://firms.modaps.eosdis.nasa.gov/data/country/{p}/{y}/{p}_{y}_{c}.csv'
PRODUCTS = ['viirs-snpp', 'viirs-jpss1', 'modis']
COUNTRIES = [c for c in os.environ.get('NASA_STATIC_COUNTRIES', 'United_States').split(',') if c]
LOCAL_DIR = os.environ.get('NASA_STATIC_LOCAL_DIR')
GRID_M = 400.0
MIN_DETECTIONS = 5
OUT_JSON = os.environ.get('NASA_STATIC_OUT') or os.path.join('data', '_stage_nasa_static.json')
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
M_PER_DEG = 111195.0


def _lines(product: str, year: int, country: str):
    if LOCAL_DIR:
        path = os.path.join(LOCAL_DIR, f'{product}_{year}_{country}.csv')
        with open(path, newline='') as f:
            yield from f
        return
    req = urllib.request.Request(BASE.format(p=product, y=year, c=country), headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        yield from io.TextIOWrapper(r, encoding='utf-8', newline='')


def cell_of(lat: float, lng: float) -> tuple:
    """The 400 m cell: rows of GRID_M in latitude, columns of GRID_M along each row's parallel."""
    i = math.floor(lat * M_PER_DEG / GRID_M)
    row_lat = (i + 0.5) * GRID_M / M_PER_DEG
    j = math.floor(lng * M_PER_DEG * math.cos(math.radians(row_lat)) / GRID_M)
    return i, j


def mine(rows) -> list:
    """rows: iterable of dicts with latitude, longitude, type, acq_date. → sources, strongest first."""
    cells = defaultdict(lambda: [0, 0.0, 0.0, None, None])
    for r in rows:
        if str(r.get('type', '')).strip() != '2':
            continue
        try:
            lat, lng = float(r['latitude']), float(r['longitude'])
        except (KeyError, TypeError, ValueError):
            continue
        c = cells[cell_of(lat, lng)]
        c[0] += 1
        c[1] += lat
        c[2] += lng
        d = r.get('acq_date')
        if d:
            c[3] = d if c[3] is None or d < c[3] else c[3]
            c[4] = d if c[4] is None or d > c[4] else c[4]
    out = [{'lat': round(c[1] / c[0], 5), 'lng': round(c[2] / c[0], 5), 'n': c[0], 'first': c[3], 'last': c[4]}
           for c in cells.values() if c[0] >= MIN_DETECTIONS]
    out.sort(key=lambda s: -s['n'])
    return out


def _published(year: int) -> bool:
    if LOCAL_DIR:
        return os.path.exists(os.path.join(LOCAL_DIR, f'{PRODUCTS[0]}_{year}_{COUNTRIES[0]}.csv'))
    try:
        req = urllib.request.Request(BASE.format(p=PRODUCTS[0], y=year, c=COUNTRIES[0]), method='HEAD', headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    now = datetime.now(timezone.utc)
    years = [int(os.environ['NASA_STATIC_YEAR'])] if os.environ.get('NASA_STATIC_YEAR') else [now.year - 1, now.year - 2]
    prev = None
    if os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    year = next((y for y in years if _published(y)), None)
    if year is None:
        sys.stderr.write(f'[nasa_static] no archive year among {years} is reachable; keeping the previous stage\n')
        return 0
    if prev and prev.get('year') == year and prev.get('countries') == COUNTRIES and prev.get('grid_m') == GRID_M:
        sys.stderr.write(f'[nasa_static] {year} already staged ({prev.get("count")} sources); nothing to do\n')
        return 0

    counts = {}

    def rows():
        for p in PRODUCTS:
            for c in COUNTRIES:
                n = 0
                for r in csv.DictReader(_lines(p, year, c)):
                    n += 1
                    yield r
                counts[f'{p}:{c}'] = n
    try:
        sources = mine(rows())
    except Exception as e:  # a partial year must not replace a complete one
        sys.stderr.write(f'[nasa_static] {year} download failed ({e}); keeping the previous stage\n')
        return 0
    payload = {'generated_utc': now.isoformat().replace('+00:00', 'Z'), 'year': year, 'countries': COUNTRIES, 'products': PRODUCTS,
               'rows_read': counts, 'method': 'FIRMS Static Thermal Anomalies method on NASA type-2 detections: 400 m grid, >= 5 detections in the year',
               'grid_m': GRID_M, 'min_detections': MIN_DETECTIONS, 'count': len(sources), 'sources': sources}
    os.makedirs(os.path.dirname(OUT_JSON) or '.', exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[nasa_static] {year}: {sum(counts.values()):,} rows → {len(sources):,} static sources\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
