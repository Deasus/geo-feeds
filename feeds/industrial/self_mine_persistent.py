#!/usr/bin/env python3
"""Self-mine persistent thermal sources from the firms feed's own 90-day archive.

NASA's "Type" field in its monthly archive comes from a 90-day trailing-window
clustering of FIRMS detections. This replicates that on our own archive:

1. Read the firms feed's rolling daily archive (persistence/index.json and one
   file per day, written by feeds/firms/archive_day.py).
2. Each day file holds the 0.004° (~375 m, VIIRS pixel) cells that had a detection.
3. Any cell seen on >= MIN_DISTINCT_DAYS distinct days is a persistent source
   (gas flares, burn pits, unregistered industry) and becomes a
   `persistent_other` feature in industrial_sites.geojson.

The archive starts empty. Until it holds MIN_ARCHIVE_DAYS days, the previous stage
file (seeded from the earlier 90-day mining) is kept unchanged, so deconfliction
never loses its persistent layer while the archive fills.

Output: data/_stage_firms_persistent.json (same contract as before).
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

ARCHIVE_BASE = os.environ.get('GEO_FEEDS_PERSISTENCE_URL',
                              'https://raw.githubusercontent.com/Deasus/geo-feeds/firms/persistence/')
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
LOOKBACK_DAYS = 90
MIN_DISTINCT_DAYS = int(os.environ.get('MIN_DISTINCT_DAYS', '6'))
MIN_ARCHIVE_DAYS = int(os.environ.get('MIN_ARCHIVE_DAYS', '30'))
GRID_DEG = 0.004
OUT_JSON = os.path.join('data', '_stage_firms_persistent.json')
HTTP_TIMEOUT = 60


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        index = _get(ARCHIVE_BASE + 'index.json')
    except Exception as e:
        sys.stderr.write(f'[self_mine] archive index unavailable ({e}); keeping previous stage\n')
        return 0
    days = sorted(index.get('days') or [])[-LOOKBACK_DAYS:]
    if len(days) < MIN_ARCHIVE_DAYS:
        sys.stderr.write(f'[self_mine] archive holds {len(days)} days (< {MIN_ARCHIVE_DAYS}); '
                         'keeping previous stage\n')
        return 0

    grid = defaultdict(lambda: {'days': set(), 'n': 0, 'frp': 0.0})
    for day in days:
        try:
            cells = _get(ARCHIVE_BASE + f'{day}.json').get('cells', {})
        except Exception as e:
            sys.stderr.write(f'[self_mine] skip {day}: {e}\n')
            continue
        for key, (n, frp) in cells.items():
            c = grid[key]
            c['days'].add(day)
            c['n'] += n
            c['frp'] += frp * n

    clusters = []
    for key, c in grid.items():
        if len(c['days']) < MIN_DISTINCT_DAYS:
            continue
        lat, lng = (float(v) for v in key.split(','))
        d = sorted(c['days'])
        clusters.append({'lat': round(lat, 5), 'lng': round(lng, 5),
                         'n_distinct_days': len(d), 'n_detections': c['n'],
                         'first_seen': d[0], 'last_seen': d[-1],
                         'mean_frp': round(c['frp'] / c['n'], 1) if c['n'] else 0})
    sys.stderr.write(f'[self_mine] {len(grid):,} cells over {len(days)} days -> {len(clusters):,} persistent\n')

    payload = {
        'generated_utc':     now_iso,
        'source':            'self-mine of the firms feed 90-day daily archive',
        'algorithm':         f'{GRID_DEG:.4f}° grid quantization + ≥{MIN_DISTINCT_DAYS} distinct-day threshold',
        'lookback_days':     LOOKBACK_DAYS,
        'sampled_commits':   len(days),     # field name kept for the merge step; = archive days
        'grid_cells_total':  len(grid),
        'min_distinct_days': MIN_DISTINCT_DAYS,
        'grid_deg':          GRID_DEG,
        'count':             len(clusters),
        'clusters':          clusters,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
