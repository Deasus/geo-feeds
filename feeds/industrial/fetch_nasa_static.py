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
without downloading again. Exit 1 (2026-11-16 review) when a probe is refused or times out, when
no year is published, or when any product file fails or comes back empty: the stage on disk is
kept as it was (the merge carries it) and the feed's status shows the failure. A failure on the
newer year never falls back to the older one, and a staged newer year is never replaced by an
older one.
"""
import csv
import io
import json
import math
import os
import sys
import urllib.error
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
# NASA FIRMS's requested acknowledgement; NASA Earth science data are open, with no restriction on use
ATTRIBUTION = ("We acknowledge the use of data from NASA's Fire Information for Resource Management System (FIRMS) "
               "(https://earthdata.nasa.gov/firms), part of NASA's Earth Science Data and Information System (ESDIS).")
LICENCE = 'NASA Earth Science Data and Information Policy: open, free of charge, no restriction on use'


class FetchError(Exception):
    """Anything but "published" or "not published (404)": the run fails and the stage is kept."""


def _open(url: str, rng=None):
    headers = {'User-Agent': UA}
    if rng:
        headers['Range'] = f'bytes={rng[0]}-{rng[1]}'
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)


def _lines(product: str, year: int, country: str):
    if LOCAL_DIR:
        path = os.path.join(LOCAL_DIR, f'{product}_{year}_{country}.csv')
        with open(path, newline='') as f:
            yield from f
        return
    try:
        with _open(BASE.format(p=product, y=year, c=country)) as r:
            yield from io.TextIOWrapper(r, encoding='utf-8', newline='')
    except urllib.error.HTTPError as e:
        raise FetchError(f'{product} {year} {country}: HTTP {e.code}') from e
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f'{product} {year} {country}: {e}') from e


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


def probe(year: int) -> str:
    """'ok' when every product file of the year answers with a CSV header, 'missing' when NASA answers 404 (not published
    yet); anything else (a refused request, a timeout, a 5xx, a body that is not the CSV) raises FetchError. A ranged GET of
    the first kilobyte, not HEAD (servers refuse HEAD)."""
    for p in PRODUCTS:
        for c in COUNTRIES:
            if LOCAL_DIR:
                if not os.path.exists(os.path.join(LOCAL_DIR, f'{p}_{year}_{c}.csv')):
                    return 'missing'
                continue
            try:
                with _open(BASE.format(p=p, y=year, c=c), (0, 1023)) as r:
                    head = r.read(1024)
                    status = getattr(r, 'status', 200)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 'missing'
                raise FetchError(f'probe {p} {year} {c}: HTTP {e.code}') from e
            except (urllib.error.URLError, OSError) as e:
                raise FetchError(f'probe {p} {year} {c}: {e}') from e
            if status not in (200, 206) or not head.lstrip().lower().startswith(b'latitude'):
                raise FetchError(f'probe {p} {year} {c}: status {status}, not the CSV')
    return 'ok'


def main() -> int:
    """Exit 0: staged, or nothing better to stage. Exit 1: a failure (the stage on disk is kept as it was)."""
    now = datetime.now(timezone.utc)
    years = [int(os.environ['NASA_STATIC_YEAR'])] if os.environ.get('NASA_STATIC_YEAR') else [now.year - 1, now.year - 2]
    prev = None
    if os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    same_scope = bool(prev) and prev.get('countries') == COUNTRIES and prev.get('grid_m') == GRID_M and prev.get('complete', True)
    year = None
    for y in years:
        try:
            state = probe(y)
        except FetchError as e:   # never fall back to an older year on a failure: that would replace a good stage with a worse one
            sys.stderr.write(f'[nasa_static] {e}; keeping the previous stage\n')
            return 1
        if state == 'ok':
            year = y
            break
        sys.stderr.write(f'[nasa_static] {y} not published yet (404)\n')
    if year is None:
        sys.stderr.write(f'[nasa_static] no archive year among {years} is published; keeping the previous stage\n')
        return 1
    if same_scope and int(prev.get('year') or 0) > year:
        sys.stderr.write(f'[nasa_static] the stage holds {prev.get("year")}, newer than {year}; keeping it\n')
        return 0
    if same_scope and prev.get('year') == year:
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
                if n == 0:
                    raise FetchError(f'{p} {year} {c}: no rows')
                counts[f'{p}:{c}'] = n
    try:
        sources = mine(rows())
    except Exception as e:  # noqa: BLE001 — a partial year must not replace a complete one
        sys.stderr.write(f'[nasa_static] {year} download failed ({e}); keeping the previous stage\n')
        return 1
    if not sources and prev and prev.get('count'):
        sys.stderr.write(f'[nasa_static] {year} gave no static source (the stage holds {prev.get("count")}); keeping it\n')
        return 1
    payload = {'generated_utc': now.isoformat().replace('+00:00', 'Z'), 'year': year, 'countries': COUNTRIES, 'products': PRODUCTS,
               'rows_read': counts, 'complete': True,
               'method': 'FIRMS Static Thermal Anomalies method on NASA type-2 detections: 400 m grid, >= 5 detections in the year',
               'attribution': ATTRIBUTION, 'licence': LICENCE,
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
