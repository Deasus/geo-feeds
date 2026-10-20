"""Feed registry for conductor.py.

Each feed's output lives on a branch of the same name; the branch root is the
feed's data/ directory, so a file is served at
  https://raw.githubusercontent.com/Deasus/geo-feeds/<feed>/<path>

every    target seconds between run starts
timeout  seconds allowed per command
run      commands, run in order inside work/<feed>/
code     feeds/<dir>/ folders copied into the work dir (their files land side by side)
requires env vars that must be set, else the feed is skipped
optional env vars passed through when set (keys that improve a feed but aren't needed)
env      extra env vars
keep_going  run every command even if one fails (independent fetchers + a final merge)
ignore   patterns under data/ that are never published (caches, temp files)
"""

FEEDS = {
    # ── near-real-time (fast lane) ──────────────────────────────────────
    'lightning': {'code': ['lightning'], 'run': [['python', 'fetch_glm.py']],
                  'every': 60, 'timeout': 240},
    'goes-fire': {'code': ['goes_fire'], 'run': [['python', 'fetch_fdc.py']],
                  'every': 120, 'timeout': 300},
    'ngfs':      {'code': ['ngfs'], 'run': [['python', 'fetch_ngfs.py']],
                  'every': 120, 'timeout': 300},
    'aircraft':  {'code': ['aircraft'], 'run': [['python', 'fetch_aircraft.py']],
                  'every': 300, 'timeout': 240, 'optional': ['ADSBX_API_KEY']},

    # ── geostationary fire outside the Americas ─────────────────────────
    'himawari':  {'code': ['himawari', '_shared', 'firms'], 'run': [['python', 'fetch_himawari.py']],
                  'every': 600, 'timeout': 300},
    'meteosat':  {'code': ['meteosat', '_shared', 'firms'],
                  'run': [['python', 'fetch_meteosat.py'], ['python', 'fetch_iodc.py']],
                  'every': 600, 'timeout': 600, 'keep_going': True,
                  'requires': ['LSASAF_USER', 'LSASAF_PASSWORD']},

    # ── polar-orbiter fire + its 90-day persistence archive ─────────────
    'firms':     {'code': ['firms'], 'run': [['python', 'fetch_firms.py'], ['python', 'archive_day.py']],
                  'every': 900, 'timeout': 1500, 'env': {'FIRMS_PUBLIC_FALLBACK': '1'},
                  'optional': ['FIRMS_MAP_KEY']},

    # ── situation, weather, air ─────────────────────────────────────────
    'imsr':      {'code': ['imsr'], 'run': [['python', 'fetch_imsr.py']], 'every': 900, 'timeout': 600},
    'aqi':       {'code': ['aqi'], 'run': [['python', 'fetch_aqi.py']], 'every': 1800, 'timeout': 1200},
    'news':      {'code': ['news'], 'run': [['python', 'fetch_news.py']], 'every': 1800, 'timeout': 1500},
    'hrrr':      {'code': ['hrrr'], 'run': [['python', 'fetch_hrrr.py']], 'every': 3600, 'timeout': 2400},
    'cameras':   {'code': ['cameras'], 'run': [['python', 'fetch_cameras.py']], 'every': 3600, 'timeout': 300},
    'potential': {'code': ['potential'], 'run': [['python', 'fetch_potential.py']], 'every': 10800, 'timeout': 900},
    'ecmwf':     {'code': ['ecmwf'], 'run': [['python', 'fetch_ecmwf.py']], 'every': 10800, 'timeout': 2400},
    'wind':      {'code': ['wind'], 'run': [['python', 'fetch_gfs.py']], 'every': 10800, 'timeout': 2400},
    'cams':      {'code': ['cams'], 'run': [['python', 'fetch_cams.py']], 'every': 21600, 'timeout': 2400,
                  'requires': ['CDS_API_TOKEN']},
    'satellite': {'code': ['satellite'], 'run': [['python', 'fetch_tles.py']], 'every': 86400, 'timeout': 300},

    # ── weekly deconfliction reference (industrial sites, volcanoes, flares, persistent hotspots) ──
    'industrial': {'code': ['industrial'], 'every': 604800, 'timeout': 2400, 'keep_going': True,
                   'run': [['python', 'fetch_epa_frs.py'], ['python', 'fetch_gvp.py'],
                           ['python', 'fetch_gibs_nuclear.py'], ['python', 'self_mine_persistent.py'],
                           ['python', 'fetch_global_industrial.py'], ['python', 'fetch_ne_land.py'],
                           ['python', 'build_industrial_sites.py']],
                   'ignore': ['_cache_*', '_ne_ogr_tmp.geojson', '*.tmp']},
}
