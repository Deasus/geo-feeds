#!/usr/bin/env python3
"""
geo-feeds Fire Potential pipeline — pulls the NWCG Predictive Services
7-Day Significant Fire Potential polygons from
fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer,
server-side geometry-simplified, and writes a slim JSON the frontend reads
via raw.githubusercontent.com.

WHY THIS EXISTS — the NWCG endpoint responds with ~30 MB of GeoJSON per
day at native resolution and routinely blows past the browser's 30 s
timeout (verified 2026-07-10: 29 MB / 30 s / partial). Under the old
direct-fetch path the frontend caught the timeouts silently and the map
rendered "NO OUTLOOK ISSUED" while the country actually had 18 CRITICAL
zones + 1 IGNITION polygon on Day 1 and 22 IGNITION zones on Day 5.
Bridging through this GHA-cron pipeline decimates geometry once
server-side (maxAllowableOffset=0.05° ≈ 5 km — invisible loss at
national scale, GACC boundaries still snap tight) and publishes a slim
~1 MB combined JSON. Every viewer then reads from raw.githubusercontent
with CDN cache — cold-start goes from empty-map to ~200 ms.

If NWCG is briefly down the pipeline commits nothing and viewers keep
the last-known-good outlook until the next successful publish.

OUTPUT: data/potential.json
Shape:
  {
    "generated_at":     ISO8601 UTC,
    "source":           "fsapps.nwcg.gov/psp .../npsg/outlooks_forecast",
    "resolution_deg":   0.05,                # server decimation offset
    "counts": {
      "total_features": N,                   # summed across all 7 days
      "valid_features": N,                   # isvalid == 1
      "critical":       N,                   # type == 'CRITICAL'
      "ignition":       N,                   # type == 'IGNITION'
      "by_day": { "1": {"total":..,"valid":..,"critical":..,"ignition":..}, ... }
    },
    "days": {
      "1": [ {"attrs":{"drynesscode":.,"type":..,"isvalid":.,"gacc":..,"nat_code":..,"timestampdate":..},
              "rings":[[[lng,lat],...],...]},
             ...
           ],
      "2": [...], ..., "7": [...]
    },
    "attribution": "NWCG Predictive Services · Significant Fire Potential Outlook · public domain"
  }

SOURCE (public, no auth):
  https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer/0..6/query
  (0 = Day 1 today, 1 = Day 2, ..., 6 = Day 7)

DEDIC. FRESHNESS GATE: NWCG publishes once a day (typically morning MT).
So a 3-hour cron is deliberate over-fetching for resilience, not a
cadence-vs-source-mismatch. The frontend gates staleness at 26 h on
`generated_at` to allow for a NICC late-publish + weekend/holiday.

Requires: stdlib only (urllib, json). No API key.
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────
BASE = 'https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer'
DAY_COUNT = 7                    # NWCG publishes layers 0..6 (Day 1..7)
MAX_OFFSET_DEG = 0.05            # geometry-simplification budget (~5 km at CONUS)
                                 # Verified 2026-07-10: 0.05 → 158 KB/3.2s vs
                                 # native 30 MB / 30s+ timeout. Feature counts
                                 # (234 total / 213 valid / 18 CRIT / 1 IGN)
                                 # identical to native at all decimation levels.
GEOM_PRECISION = 3               # decimal places in coordinates (~110 m at equator)
REQUEST_TIMEOUT = 60             # per-day fetch timeout (seconds)
INTER_DAY_DELAY = 3              # seconds between consecutive day requests
                                 # (NWCG returns 400 under back-to-back load
                                 # even at decimated payload sizes — verified
                                 # 2026-07-10; a 3-5s gap is enough)
RETRY_ATTEMPTS = 4               # per-day retry count on 400/500/timeout
RETRY_BACKOFF_SEC = [4, 8, 16, 32]
OUT_FILE = os.path.join(os.path.dirname(__file__), 'data', 'potential.json')

# NWCG NPSG field NAMES (verified 2026-07-10 against MapServer/1?f=json):
# gacc_name (NOT 'gacc'), forecastdatapointid = OID.
# The pre-pipeline frontend used 'gacc' as an outField and NWCG served it
# without ERROR but with no data. Fix at the source here.
OUT_FIELDS = 'drynesscode,type,isvalid,gacc_name,nat_code,timestampdate'


class NwcgQueryError(RuntimeError):
    pass


def fetch_day(day_idx: int) -> dict:
    """Fetch one NWCG MapServer layer (day_idx 0..6 = Day 1..7).

    Returns the raw ArcGIS FeatureCollection dict. Raises NwcgQueryError on
    transport error or ArcGIS server-side error after all retries.
    """
    query = {
        'where': '1=1',
        'outFields': OUT_FIELDS,
        'returnGeometry': 'true',
        'outSR': '4326',
        'maxAllowableOffset': str(MAX_OFFSET_DEG),
        'geometryPrecision': str(GEOM_PRECISION),
        'f': 'json',
    }
    url = f'{BASE}/{day_idx}/query?' + urllib.parse.urlencode(query)
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode('utf-8')
            d = json.loads(body)
            if isinstance(d, dict) and d.get('error'):
                last_err = f'ArcGIS server error: {d["error"]}'
                # 400 "Failed to execute query" is retryable on NWCG under load
                if attempt < RETRY_ATTEMPTS - 1:
                    print(f'[POTENTIAL] Day {day_idx+1} attempt {attempt+1}: {last_err} — retry in {RETRY_BACKOFF_SEC[attempt]}s', flush=True)
                    time.sleep(RETRY_BACKOFF_SEC[attempt])
                    continue
                raise NwcgQueryError(last_err)
            return d
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_err = f'transport: {e}'
            if attempt < RETRY_ATTEMPTS - 1:
                print(f'[POTENTIAL] Day {day_idx+1} attempt {attempt+1}: {last_err} — retry in {RETRY_BACKOFF_SEC[attempt]}s', flush=True)
                time.sleep(RETRY_BACKOFF_SEC[attempt])
                continue
            raise NwcgQueryError(last_err)
    raise NwcgQueryError(last_err or 'unknown')


def slim(feature: dict) -> dict | None:
    """Reduce an ArcGIS feature to {attrs, rings}. Drops geometry-less."""
    geom = feature.get('geometry') or {}
    rings = geom.get('rings') or []
    if not rings:
        return None
    attrs = feature.get('attributes') or {}
    # Emit the field under BOTH names for a while — the pre-pipeline frontend
    # reads f.attrs.gacc; new frontend code will read gacc_name. Keep both so
    # the pipeline swap is drop-in and popup text doesn't regress mid-cutover.
    gacc_name = attrs.get('gacc_name')
    return {
        'attrs': {
            'drynesscode':    attrs.get('drynesscode'),
            'type':           attrs.get('type'),
            'isvalid':        attrs.get('isvalid'),
            'gacc':           gacc_name,
            'gacc_name':      gacc_name,
            'nat_code':       attrs.get('nat_code'),
            'timestampdate':  attrs.get('timestampdate'),
        },
        'rings': rings,
    }


def summarize(features: list) -> dict:
    valid = 0
    crit = 0
    ign = 0
    for f in features:
        a = f['attrs']
        if a.get('isvalid') == 1:
            valid += 1
        t = (a.get('type') or '').upper()
        if t == 'CRITICAL':
            crit += 1
        elif t == 'IGNITION':
            ign += 1
    return {'total': len(features), 'valid': valid, 'critical': crit, 'ignition': ign}


def load_prior() -> dict:
    """Load prior data/potential.json for last-known-good fallback per day."""
    try:
        return json.load(open(OUT_FILE))
    except Exception:
        return {}


def main() -> int:
    print(f'[POTENTIAL] fetching {DAY_COUNT} NWCG forecast days ...', flush=True)
    prior = load_prior()
    prior_days = (prior.get('days') or {}) if isinstance(prior, dict) else {}
    days: dict[str, list] = {}
    day_source: dict[str, str] = {}    # 'fresh' or 'stale-carryover-<generated_at>'
    by_day_counts: dict[str, dict] = {}
    total = valid = crit = ign = 0
    hard_fail = 0

    for day_idx in range(DAY_COUNT):
        day_num = day_idx + 1
        # Serialize with inter-day delay. NWCG's MapServer returns HTTP 400
        # ("Failed to execute query") under back-to-back geometry-simplified
        # requests even at modest payload sizes — verified 2026-07-10.
        if day_idx > 0:
            time.sleep(INTER_DAY_DELAY)
        try:
            payload = fetch_day(day_idx)
        except NwcgQueryError as e:
            # Per-day resilience: NWCG intermittently returns HTTP 400 on ONE
            # specific day layer even when the others succeed (verified
            # 2026-07-10). Rather than blank that day out, fall back to the
            # previous run's data for that day. This keeps yesterday's
            # forecast on the map instead of a hole in the outlook. The
            # frontend will still flag the whole file as stale via
            # `generated_at` after 26 h.
            prior_feats = prior_days.get(str(day_num)) or []
            print(f'[POTENTIAL] Day {day_num} fetch FAILED after retries: {e} — '
                  f'falling back to prior ({len(prior_feats)} features)', flush=True)
            days[str(day_num)] = prior_feats
            s = summarize(prior_feats)
            by_day_counts[str(day_num)] = s
            day_source[str(day_num)] = 'stale-carryover'
            total += s['total']
            valid += s['valid']
            crit += s['critical']
            ign += s['ignition']
            hard_fail += 1
            continue

        feats_raw = payload.get('features') or []
        feats = [x for x in (slim(f) for f in feats_raw) if x is not None]
        days[str(day_num)] = feats
        day_source[str(day_num)] = 'fresh'

        s = summarize(feats)
        by_day_counts[str(day_num)] = s
        total += s['total']
        valid += s['valid']
        crit += s['critical']
        ign += s['ignition']
        print(f'[POTENTIAL] Day {day_num}: total={s["total"]} valid={s["valid"]} '
              f'CRIT={s["critical"]} IGN={s["ignition"]}', flush=True)

    if hard_fail >= DAY_COUNT and not any(days.values()):
        # ALL seven days failed AND we have no carryover = NWCG box is fully down
        # AND this is a cold start. Bail without touching the committed file so
        # viewers keep last-known-good.
        print(f'[POTENTIAL] all {DAY_COUNT} day fetches failed and no prior — '
              f'leaving prior JSON in place', file=sys.stderr, flush=True)
        return 1

    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'source': 'fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer',
        'resolution_deg': MAX_OFFSET_DEG,
        'counts': {
            'total_features': total,
            'valid_features': valid,
            'critical': crit,
            'ignition': ign,
            'by_day': by_day_counts,
        },
        'day_source': day_source,     # per-day 'fresh' vs 'stale-carryover'
        'days': days,
        'attribution': 'NWCG Predictive Services · Significant Fire Potential Outlook · public domain',
    }

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    tmp = OUT_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(out, fh, separators=(',', ':'))
    os.replace(tmp, OUT_FILE)

    size_kb = os.path.getsize(OUT_FILE) / 1024.0
    print(f'[POTENTIAL] wrote {OUT_FILE} ({size_kb:.1f} KB) — '
          f'total={total} valid={valid} CRIT={crit} IGN={ign} '
          f'({hard_fail} day-fetches failed)', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
