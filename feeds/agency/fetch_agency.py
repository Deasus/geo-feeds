#!/usr/bin/env python3
"""Mirror public agency and hazard feeds whose origins send no CORS headers, so a
browser client can read them from raw.githubusercontent.com instead.

Each source is written to data/<name>.json in its upstream wire shape, unchanged
(parsed and re-serialized, nothing added or renamed), so the same client decoder
reads the origin and the mirror alike. data/health.json records each source's
last attempt, last success, item count and last error.

A failed or invalid fetch never overwrites the last good file. A data file is
rewritten only when its content changed, so the branch tree only moves when the
upstream did (health.json moves every run).

Sources:
  qld   Queensland Fire Department bushfire warnings (GeoJSON on S3; no CORS)
  nhc   NOAA National Hurricane Center current storms (CurrentStorms.json; no CORS)

Stdlib only. Cadence: every 10 min (feeds.py). Env:
  AGENCY_DATA_DIR   output directory (default: data/ next to this script)
  AGENCY_ONLY       comma-separated subset of source names (debugging)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('AGENCY_DATA_DIR') or os.path.join(HERE, 'data')
UA = 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)'
TIMEOUT = 30
ATTEMPTS = 3


def features_list(doc):
    return isinstance(doc, dict) and isinstance(doc.get('features'), list)


def storms_list(doc):
    return isinstance(doc, dict) and isinstance(doc.get('activeStorms'), list)


SOURCES = {
    'qld': {
        'url': 'https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com/content/Feeds/BushfireCurrentIncidents/bushfireAlert.json',
        'upstream': 'Queensland Fire Department bushfire warnings',
        'valid': features_list,
        'count': lambda d: len(d['features']),
    },
    'nhc': {
        'url': 'https://www.nhc.noaa.gov/CurrentStorms.json',
        'upstream': 'NOAA/NWS National Hurricane Center current storms',
        'valid': storms_list,
        'count': lambda d: len(d['activeStorms']),
    },
}


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fetch(url, timeout=TIMEOUT, attempts=ATTEMPTS):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json, */*'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError('fetch failed after %d attempts: %s' % (attempts, last))


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(',', ':'))
        fh.write('\n')
    os.replace(tmp, path)


def publish(name, doc):
    """Rewrite data/<name>.json only when the document changed. Returns True if written."""
    path = os.path.join(DATA, name + '.json')
    if read_json(path, None) == doc:
        return False
    write_json(path, doc)
    return True


def main():
    os.makedirs(DATA, exist_ok=True)
    only = [s for s in os.environ.get('AGENCY_ONLY', '').replace(' ', '').split(',') if s]
    stamp = now_iso()
    health_path = os.path.join(DATA, 'health.json')
    health = read_json(health_path, {'sources': {}})
    sources = health.setdefault('sources', {})
    failed = 0

    for name, spec in SOURCES.items():
        if only and name not in only:
            continue
        st = sources.setdefault(name, {})
        st.update(upstream=spec['upstream'], url=spec['url'], last_attempt=stamp)
        try:
            raw = fetch(spec['url'])
            doc = json.loads(raw.decode('utf-8-sig'))
            if not spec['valid'](doc):
                raise ValueError('unexpected shape (top-level keys: %s)' % ', '.join(sorted(doc)[:8]) if isinstance(doc, dict) else 'not an object')
            count = spec['count'](doc)
            changed = publish(name, doc)
            st.update(ok=True, count=count, bytes=len(raw), last_success=stamp, error=None)
            if changed:
                st['last_change'] = stamp
            print('%s: ok, %d items, %d bytes%s' % (name, count, len(raw), ' (changed)' if changed else ''))
        except Exception as e:  # keep the last good file; record why
            failed += 1
            st.update(ok=False, last_failure=stamp, error=('%s: %s' % (type(e).__name__, e))[:300])
            print('%s: FAILED - %s' % (name, st['error']), file=sys.stderr)

    health['generated_at'] = stamp
    health['cadence_s'] = 600
    write_json(health_path, health)
    # Exit non-zero only when nothing could be fetched: one dead upstream must not mark the whole feed failed.
    return 1 if failed and failed == len(only or SOURCES) else 0


if __name__ == '__main__':
    raise SystemExit(main())
