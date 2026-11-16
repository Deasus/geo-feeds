#!/usr/bin/env python3
"""Merge the three staged source JSONs into a single industrial_sites.geojson
that firms feed consumes at ingest time.

Inputs (produced by the fetch_*.py scripts):
  data/_stage_epa_frs.json     — EPA FRS thermal-emitter facilities
  data/_stage_gvp.json         — Smithsonian GVP Holocene volcanoes
  data/_stage_nasa_static.json — NASA type-2 static land sources, prior year (2026-11-16)

Output:
  data/industrial_sites.geojson       — merged FeatureCollection of Points
  data/industrial_sites_meta.json     — per-source manifest
  data/industrial_sites.min.geojson   — same but rounded to 4dp, no whitespace
                                        (~1/3 the size — for browser fetch)

Note: land_10m.geojson is a separate file (polygons, distance-to-land test
lives in the FIRMS pipeline). This script does NOT touch it.
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

STAGE_FRS   = os.path.join('data', '_stage_epa_frs.json')
STAGE_GVP   = os.path.join('data', '_stage_gvp.json')
STAGE_GIBS  = os.path.join('data', '_stage_gibs_nuclear.json')       # Path B
STAGE_FIRMS = os.path.join('data', '_stage_firms_persistent.json')   # Path A
STAGE_GLOBAL = os.path.join('data', '_stage_global_industrial.json')  # 2026-10-16: WRI GPPD + OSM, outside the US
STAGE_NASA  = os.path.join('data', '_stage_nasa_static.json')        # 2026-11-16: NASA type-2 static land sources (prior year)
LAND_POLY   = os.path.join('data', 'land_10m.geojson')   # existence check only

OUT_GEOJSON = os.path.join('data', 'industrial_sites.geojson')
OUT_MIN     = os.path.join('data', 'industrial_sites.min.geojson')
OUT_META    = os.path.join('data', 'industrial_sites_meta.json')

# Per-class buffer_m: the SOURCE's own extent around its point (2026-11-16). The detection's
# pixel and location error are added per detection when it is tested
# (feeds/firms/deconflict.effective_buffer_m: buffer_m + half the pixel diagonal + 100 m),
# so a MODIS pixel at the scan edge or a GOES pixel reaches farther than a VIIRS pixel at
# nadir. Before 2026-11-16 buffer_m was the whole test (500 m, sized for VIIRS at nadir).
#   industrial / nuclear  500 m  a registered facility point (EPA FRS, WRI GPPD, OSM, SEDAC):
#                                the plant, flare stack or landfill within ~500 m of it
#   volcano              5000 m  flank vents
#   persistent_other      550 m  a 0.004° self-mined cell (half-diagonal ~280 m) whose detection
#   industrial_firms      550 m  centres sit up to half a VIIRS pixel (265 m) from the source; the
#                                NASA static cells are 400 m (half-diagonal 283 m) → 283 + 265 ≈ 550
DEFAULT_BUFFER_M = {
    'industrial': 500,
    'volcano':    5000,
    'nuclear':    500,   # Path B: NASA GIBS SEDAC nuclear plants — reactor complex bounded ~500m
    # Path A: self-mined persistent thermal sources (replaces the earlier VNP14IMGML approach)
    'persistent_other': 550,
    # 2026-11-16: NASA's own type-2 "other static land source" detections of the previous year
    # (fetch_nasa_static.py); the flag is proximate_industrial_firms, which the client already names
    'industrial_firms': 550,
}


def _load(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        sys.stderr.write(f'[merge] MISSING {path}\n')
        return None
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            sys.stderr.write(f'[merge] {path} corrupt: {e}\n')
            return None


def _feat(lat: float, lng: float, props: dict) -> dict:
    return {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [round(lng, 5), round(lat, 5)]},
        'properties': props,
    }


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    frs    = _load(STAGE_FRS)
    gvp    = _load(STAGE_GVP)
    gibs   = _load(STAGE_GIBS)     # Path B: nuclear plants
    firms  = _load(STAGE_FIRMS)    # Path A: NASA's own persistent-source clustering
    glob   = _load(STAGE_GLOBAL)   # 2026-10-16: global industrial heat (power plants, flares, refineries, steel, cement)
    nasa   = _load(STAGE_NASA)     # 2026-11-16: NASA type-2 static land sources, prior calendar year
    if not frs and not gvp and not gibs and not firms and not glob and not nasa and not os.path.exists(OUT_GEOJSON):
        sys.exit('[merge] FATAL: no source stages and no previous output; nothing to merge')

    features: list[dict] = []
    source_summary: list[dict] = []

    # ---- EPA FRS ----
    if frs:
        n_before = len(features)
        for r in frs.get('facilities', []):
            features.append(_feat(r['lat'], r['lng'], {
                'source_id':  f'epa_frs:{r["reg_id"]}',
                'source_type': r['src_type'],
                'class':      r['class'],
                'naics':      r['naics'],
                'name':       r['name'],
                'state':      r['state'],
                'buffer_m':   DEFAULT_BUFFER_M['industrial'],
                'provenance': 'EPA FRS national_single.zip',
            }))
        source_summary.append({
            'source':      'epa_frs',
            'generated':   frs.get('generated_utc'),
            'count':       len(features) - n_before,
            'naics_used':  frs.get('naics_filter', []),
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} EPA FRS facilities\n')

    # ---- GVP volcanoes ----
    if gvp:
        n_before = len(features)
        for v in gvp.get('volcanoes', []):
            features.append(_feat(v['lat'], v['lng'], {
                'source_id':   v['source_id'],
                'source_type': 'volcano',
                'class':       'volcano',
                'name':        v['name'],
                'country':     v.get('country'),
                'volcano_type': v.get('volcano_type'),
                'last_eruption_year': v.get('last_eruption_year'),
                'buffer_m':    v.get('buffer_m', DEFAULT_BUFFER_M['volcano']),
                'provenance':  'Smithsonian GVP-VOTW Holocene Volcanoes',
            }))
        source_summary.append({
            'source':    'gvp_holocene',
            'generated': gvp.get('generated_utc'),
            'count':     len(features) - n_before,
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} GVP volcanoes\n')

    # ---- GIBS nuclear plants (Path B) ----
    if gibs:
        n_before = len(features)
        for p in gibs.get('plants', []):
            features.append(_feat(p['lat'], p['lng'], {
                'source_id':   p['source_id'],
                'source_type': 'nuclear_plant',
                'class':       'nuclear',
                'name':        p['name'],
                'country':     p.get('country'),
                'num_reactors': p.get('num_reactors'),
                'buffer_m':    p.get('buffer_m', DEFAULT_BUFFER_M['nuclear']),
                'provenance':  'NASA GIBS Nuclear_Power_Plant_Locations (SEDAC)',
            }))
        source_summary.append({
            'source':    'gibs_nuclear',
            'generated': gibs.get('generated_utc'),
            'count':     len(features) - n_before,
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} GIBS nuclear plants\n')

    # ---- FIRMS self-mined persistent sources (Path A, redesigned) ----
    # Replaces the earlier VNP14IMGML fetch: NASA doesn't publish a product
    # by that name (verified 2026-07-02 via CMR zero-results search). UMD's
    # Type field is a 90-day clustering result computed by them; we replicate
    # the algorithm on our own FIRMS archive for a real-time equivalent.
    #
    # Emit as class='persistent_other' — a fourth deconfliction category
    # (alongside industrial / volcano / nuclear) that captures thermal
    # sources EPA/GVP/GIBS don't cover: illegal burn pits, off-grid flares,
    # unregistered agricultural burn areas, small-industrial not in NAICS
    # thermal codes, etc. Provenance says "self-mined" so operators know
    # this is our derived layer, not an authoritative registry.
    if firms:
        n_before = len(features)
        for i, c in enumerate(firms.get('clusters', [])):
            features.append(_feat(c['lat'], c['lng'], {
                'source_id':   f'firms_self_mined:{i}',
                'source_type': 'persistent_other',
                'class':       'persistent_other',
                'name':        f'PERSISTENT THERMAL ({c.get("n_distinct_days",0)}d / {c.get("n_detections",0)} dets)',
                'n_distinct_days': c.get('n_distinct_days'),
                'n_detections':    c.get('n_detections'),
                'first_seen':      c.get('first_seen'),
                'last_seen':       c.get('last_seen'),
                'mean_frp':        c.get('mean_frp'),
                'buffer_m':    DEFAULT_BUFFER_M.get('persistent_other', 500),
                'provenance':  f'geo-feeds self-mine 90-day FIRMS archive '
                               f'({firms.get("min_distinct_days","6")}-day distinct threshold, '
                               f'{firms.get("grid_deg","0.004")}° grid)',
            }))
        source_summary.append({
            'source':          'firms_self_mined_persistent',
            'generated':       firms.get('generated_utc'),
            'count':           len(features) - n_before,
            'lookback_days':   firms.get('lookback_days'),
            'sampled_commits': firms.get('sampled_commits'),
            'algorithm':       firms.get('algorithm'),
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} self-mined persistent-source clusters\n')

    # ---- 2026-10-16: global industrial heat sources (FIRMS pipeline went global) ----
    # class 'industrial' so detections near them get proximate_industrial exactly as
    # EPA FRS sites do in the US; source_type keeps the finer kind (gas_flare,
    # refinery, steelworks, cement, power_plant_<fuel>) for operator tooltips.
    if glob:
        n_before = len(features)
        for site in glob.get('sites', []):
            features.append(_feat(site['lat'], site['lng'], {
                'source_id':   site['source_id'],
                'source_type': site['source_type'],
                'class':       'industrial',
                'name':        site.get('name'),
                'buffer_m':    site.get('buffer_m', DEFAULT_BUFFER_M['industrial']),
                'provenance':  site.get('provenance'),
            }))
        source_summary.append({
            'source':    'global_industrial',
            'generated': glob.get('generated_utc'),
            'count':     len(features) - n_before,
            'detail':    glob.get('sources'),
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} global industrial heat sources\n')

    # ---- 2026-11-16: NASA's own static land sources (FIRMS standard archive, type 2) ----
    if nasa:
        n_before = len(features)
        yr = nasa.get('year')
        for i, c in enumerate(nasa.get('sources', [])):
            features.append(_feat(c['lat'], c['lng'], {
                'source_id':   f'nasa_static:{yr}:{i}',
                'source_type': 'static_land_source',
                'class':       'industrial_firms',
                'name':        f'NASA STATIC LAND SOURCE ({c.get("n", 0)} type-2 dets, {yr})',
                'n_detections': c.get('n'),
                'first_seen':  c.get('first'),
                'last_seen':   c.get('last'),
                'buffer_m':    DEFAULT_BUFFER_M['industrial_firms'],
                'provenance':  f'NASA FIRMS {yr} standard archive, type 2 (other static land source); '
                               f'{int(nasa.get("grid_m", 400))} m grid, >= {nasa.get("min_detections", 5)} detections in the year',
            }))
        source_summary.append({'source': 'nasa_static', 'generated': nasa.get('generated_utc'), 'year': yr,
                               'countries': nasa.get('countries'), 'count': len(features) - n_before, 'method': nasa.get('method'),
                               'attribution': nasa.get('attribution'), 'licence': nasa.get('licence')})
        sys.stderr.write(f'[merge] +{len(features) - n_before} NASA static land sources ({yr})\n')

    # ---- Carry forward any source whose stage is missing this run ----
    # A failed fetch (upstream outage, a WAF challenge) leaves no stage file. Rather
    # than silently dropping that whole layer from the reference, keep its features
    # from the previous merged output so deconfliction never regresses.
    missing = {'epa_frs': frs, 'gvp': gvp, 'gibs_npp': gibs, 'firms_self_mined': firms,
               'wri_gppd': glob, 'osm': glob, 'nasa_static': nasa}
    missing = {prefix for prefix, stage in missing.items() if not stage}
    if missing:
        prev = _load(OUT_GEOJSON) or {}
        carried = [f for f in prev.get('features', [])
                   if str((f.get('properties') or {}).get('source_id', '')).split(':')[0] in missing]
        features.extend(carried)
        source_summary.append({'source': 'carried_forward', 'prefixes': sorted(missing), 'count': len(carried)})
        sys.stderr.write(f'[merge] carried forward {len(carried)} features for missing stages {sorted(missing)}\n')

    # ---- Land polygon file existence check (not merged, just reported) ----
    land_ok = os.path.exists(LAND_POLY) and os.path.getsize(LAND_POLY) > 100_000
    source_summary.append({
        'source':    'natural_earth_land_10m',
        'file':      LAND_POLY if land_ok else None,
        'available': land_ok,
    })

    fc = {
        'type':     'FeatureCollection',
        'generated_utc': now_iso,
        'version':  '1.0.0',
        'features': features,
    }

    # Write the pretty-formatted GeoJSON (for git-diffing)
    os.makedirs(os.path.dirname(OUT_GEOJSON), exist_ok=True)
    for path, kwargs in [
        (OUT_GEOJSON, {'indent': None, 'separators': (',', ':')}),
        (OUT_MIN,     {'separators': (',', ':')}),
    ]:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(fc, f, **kwargs)
        os.replace(tmp, path)

    # Manifest — every claim in the output must be back-linkable to a source
    meta = {
        'generated_utc':   now_iso,
        'total_features':  len(features),
        'sources':         source_summary,
        'consumer':        'firms feed/fetch_firms.py',
        'buffer_defaults': DEFAULT_BUFFER_M,
    }
    tmp = OUT_META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, OUT_META)

    size_kb = os.path.getsize(OUT_MIN) / 1024
    sys.stderr.write(f'[merge] wrote {OUT_GEOJSON} '
                     f'({len(features)} features, {size_kb:.0f} KB minified)\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
