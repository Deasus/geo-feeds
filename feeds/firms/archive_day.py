#!/usr/bin/env python3
"""Keep a rolling 90-day archive of daily VIIRS detection cells for the
persistent-source miner (industrial feed, self_mine_persistent.py).

Runs after fetch_firms.py. Reads data/viirs.json (24-hour window), quantizes each
detection to a 0.004° cell (~375 m, the VIIRS pixel) and merges it into the file
for its acquisition day:

  data/persistence/<YYYY-MM-DD>.json  {"day", "grid_deg", "cells": {"<lat>,<lng>": [max_dets, max_frp]}}
  data/persistence/index.json         {"grid_deg", "keep_days", "days": [...]}

A detection reappears in every 15-minute snapshot for 24 hours, so a cell keeps
the MAX count and FRP seen in any one snapshot rather than a running sum. The
miner only needs presence per day. Day files older than KEEP_DAYS are deleted,
so the archive stays bounded (~200 KB/day) and needs no git history.
"""
import json
import os
from datetime import datetime, timedelta, timezone

GRID_DEG = 0.004
KEEP_DAYS = 92
SRC = os.path.join('data', 'viirs.json')
DIR = os.path.join('data', 'persistence')


def _write(path, payload):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, path)


def main() -> int:
    try:
        with open(SRC) as f:
            rows = json.load(f).get('detections') or []
    except (OSError, ValueError) as e:
        print(f'[archive] no usable {SRC}: {e}')
        return 0
    os.makedirs(DIR, exist_ok=True)

    by_day = {}
    for r in rows:
        day, lat, lng = r.get('acq_date'), r.get('lat'), r.get('lng')
        if not day or lat is None or lng is None:
            continue
        key = f'{round(lat / GRID_DEG) * GRID_DEG:.4f},{round(lng / GRID_DEG) * GRID_DEG:.4f}'
        cells = by_day.setdefault(day, {})
        n, frp = cells.get(key, (0, 0.0))
        cells[key] = (n + 1, max(frp, float(r.get('frp') or 0)))

    for day, snap in by_day.items():
        path = os.path.join(DIR, f'{day}.json')
        try:
            with open(path) as f:
                cells = json.load(f).get('cells', {})
        except (OSError, ValueError):
            cells = {}
        for key, (n, frp) in snap.items():
            old = cells.get(key, [0, 0.0])
            cells[key] = [max(old[0], n), round(max(old[1], frp), 1)]
        _write(path, {'day': day, 'grid_deg': GRID_DEG, 'cells': cells})

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    days = []
    for name in sorted(os.listdir(DIR)):
        if not name.endswith('.json') or name == 'index.json':
            continue
        if name[:-5] < cutoff:
            os.remove(os.path.join(DIR, name))
        else:
            days.append(name[:-5])
    _write(os.path.join(DIR, 'index.json'), {'grid_deg': GRID_DEG, 'keep_days': KEEP_DAYS, 'days': days})
    print(f'[archive] merged {len(rows)} detections into {len(by_day)} day file(s); archive holds {len(days)} days')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
