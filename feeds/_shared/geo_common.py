"""Shared helpers for the geostationary fire feeds (Himawari via DEA, Meteosat via LSA SAF).

Output matches the GOES feed's shape so a client renders all of them the same way:
  {generated_at, newest_granule, oldest_granule, window_minutes, window_scans, product,
   coverage, attribution, licence, counts: {total, suppressed, by_flag, by_tier},
   detections: [{lat, lng, frp, tempK, tier, sat, conf, age_sec, first_seen_sec, scans}],
   suppressed: [[lat, lng, flag, nearest_infra_name], ...]}

Deconfliction, as in the FIRMS feed: every merged detection is checked against the
industrial-sites reference. Anything within the buffer of a known industrial site,
gas flare, volcano, nuclear plant, solar farm or persistent hotspot moves to
`suppressed` (with its reason) instead of being shown as a fire.
"""
import json
import os
from datetime import datetime, timezone

KEEP_TIERS = {'good', 'saturated', 'high', 'medium'}
CELL_DEG = 0.027          # ~3 km merge cell, as in the GOES feed
SHOWN_FLAGS = {'clear', 'low_confidence'}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def tier(conf01):
    """Detection confidence (0-1) -> the GOES feed's tier vocabulary."""
    if conf01 is None:
        return 'medium'
    if conf01 >= 0.8:
        return 'good'
    if conf01 >= 0.5:
        return 'high'
    if conf01 >= 0.3:
        return 'medium'
    return 'low'


def merge_window(dets: list, now: datetime) -> list:
    """dets: [{lat, lng, frp, tempK, conf (0-1), sat, obs (datetime), scan (str)}].
    Merges repeat sightings of one ~3 km cell across scans: newest sighting wins,
    first_seen_sec keeps the oldest, scans counts distinct scans."""
    cells = {}
    for d in sorted(dets, key=lambda d: d['obs']):
        t = tier(d.get('conf'))
        if t not in KEEP_TIERS:
            continue
        key = (round(d['lat'] / CELL_DEG), round(d['lng'] / CELL_DEG))
        age = max(0, int((now - d['obs']).total_seconds()))
        rec = {'lat': round(d['lat'], 4), 'lng': round(d['lng'], 4),
               'frp': round(d['frp'], 1) if d.get('frp') is not None else None,
               'tempK': round(d['tempK'], 1) if d.get('tempK') else None,
               'tier': t, 'sat': d['sat'],
               'conf': round(d['conf'] * 100) if d.get('conf') is not None else None,
               'age_sec': age}
        c = cells.get(key)
        if c is None:
            cells[key] = {**rec, 'first_seen_sec': age, '_scans': {d['scan']}}
        else:
            first = max(c['first_seen_sec'], age)
            c.update(rec)
            c['first_seen_sec'] = first
            c['_scans'].add(d['scan'])
    out = []
    for c in cells.values():
        c['scans'] = len(c.pop('_scans'))
        out.append(c)
    return out


def deconflict(dets: list, min_buffer_m: int = 0):
    """Split merged detections into (shown, suppressed, meta) using the shared
    industrial-sites reference (deconflict.py, copied in from the firms feed).

    The reference buffers (500 m for industrial, nuclear and persistent sources) are
    sized for VIIRS's 375 m pixels. A geostationary pixel is 2-3.5 km across with
    km-scale geolocation error, so a flare or refinery routinely lands outside a
    500 m buffer. min_buffer_m raises every buffer to at least the pixel scale."""
    from deconflict import Deconflicter
    dc = Deconflicter()
    dc.load()                                   # best effort; never raises
    rows = [{'lat': d['lat'], 'lng': d['lng'],
             'confidence': str(d['conf']) if d.get('conf') is not None else None} for d in dets]
    dc.enrich_many(rows)
    shown, suppressed, by_flag = [], [], {}
    for d, r in zip(dets, rows):
        flag = r.get('deconfliction_flag') or 'clear'
        dist, cls = r.get('nearest_infra_m'), r.get('nearest_infra_class')
        if flag == 'clear' and min_buffer_m and dist is not None and dist <= min_buffer_m and cls:
            flag = f'proximate_{cls}'
        by_flag[flag] = by_flag.get(flag, 0) + 1
        if flag in SHOWN_FLAGS:
            shown.append(d)
        else:
            suppressed.append([d['lat'], d['lng'], flag, r.get('nearest_infra_name')])
    meta = {'reference_loaded': dc.loaded, 'n_sources': dc.n_sources, 'by_flag': by_flag,
            'min_buffer_m': min_buffer_m}
    return shown, suppressed, meta


def payload(shown, suppressed, meta, *, now, scans, product, coverage, attribution, licence, extra=None):
    scans = sorted(scans)
    by_tier = {}
    for d in shown:
        by_tier[d['tier']] = by_tier.get(d['tier'], 0) + 1
    out = {
        'generated_at': iso(now),
        'newest_granule': scans[-1] if scans else None,
        'oldest_granule': scans[0] if scans else None,
        'window_minutes': None,
        'window_scans': len(scans),
        'product': product,
        'coverage': coverage,
        'attribution': attribution,
        'licence': licence,
        'deconfliction': {'reference_loaded': meta['reference_loaded'], 'n_sources': meta['n_sources'],
                          'min_buffer_m': meta['min_buffer_m']},
        'counts': {'total': len(shown), 'suppressed': len(suppressed),
                   'by_flag': meta['by_flag'], 'by_tier': by_tier},
        'detections': sorted(shown, key=lambda d: -(d.get('frp') or 0)),
        'suppressed': suppressed,
    }
    if scans:
        first = datetime.strptime(scans[0], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        out['window_minutes'] = round((now - first).total_seconds() / 60)
    out.update(extra or {})
    return out


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'))
    os.replace(tmp, path)
