"""VIIRS/MODIS false-positive deconfliction — spatial join against
geo-feeds's industrial-sites reference layer.

Given a stream of FIRMS detections (already normalized to the pipeline's
lat/lng/frp/confidence shape), this module adds:

    nearest_infra_m       int  — distance in meters to the closest known thermal source
    nearest_infra_id      str  — provenance-safe source id, e.g. epa_frs:110000... / gvp:333010
    nearest_infra_class   str  — one of: industrial | volcano | (future: solar)
    nearest_infra_name    str  — human-readable source name for operator tooltips
    deconfliction_flag    str  — one of:
                                    clear                 (real fire candidate — nothing nearby, on land, not low-confidence)
                                    low_confidence        (VIIRS confidence == 'low')
                                    proximate_industrial  (within buffer of EPA-FRS thermal facility)
                                    proximate_volcano     (within buffer of GVP volcano summit)
                                    offshore              (detection is over water per Natural Earth 10m land)

The buffer distance is per-source AND per-detection (2026-11-16). A known source
explains a detection when the source can lie inside the detection's pixel:

    distance <= buffer_m (the source's own extent, published per source)
              + half the detection's pixel diagonal (FIRMS scan x track, km;
                the GOES feed passes its pixel footprint the same way)
              + GEOLOC_M (location error)

Before 2026-11-16 the test was distance <= buffer_m (500 m), sized for a VIIRS
pixel at nadir. A MODIS pixel at the scan edge is ~4.8 x 2 km and a VIIRS pixel
~0.8 x 0.8 km, so heat from a landfill or a plant routinely landed outside 500 m.
Every source within reach is tested, not only the nearest one (sources carry
different buffers, so the nearest is not always the one that covers).

Data-source URL is public — served from industrial feed-sites via
raw.githubusercontent.com, same pattern as every other geo-feeds feed.

Failure modes:
  - If the industrial-sites layer is unreachable at ingest, we log a warning
    and every detection gets deconfliction_flag='clear' (no change to
    behavior). We NEVER hard-fail — the fire feed is life-safety, the
    filter is a nice-to-have on top.
  - If sklearn is unavailable in the runtime, we fall back to a pure-Python
    brute-force nearest-neighbor. Slower (~ms/detection at 5k sources) but
    correct.
"""
import io
import json
import math
import os
import sys
import time
import urllib.request
import zipfile
from typing import Optional

# Public raw URLs — same pattern as firms feed → lightning feed etc.
INDUSTRIAL_SITES_URL = os.environ.get('GEO_FEEDS_INDUSTRIAL_URL') or 'https://raw.githubusercontent.com/Deasus/geo-feeds/industrial/industrial_sites.min.geojson'  # env override = local testing only
LAND_POLY_URL        = 'https://raw.githubusercontent.com/Deasus/geo-feeds/industrial/land_10m.geojson'

# Meters conversion for haversine distances (Earth radius commonly cited)
EARTH_RADIUS_M = 6_371_000.0

HTTP_TIMEOUT = 30

# The location-error term of the known-source test. FIRMS FAQ: the NRT location
# error is "very small (< 100 meters)" for VIIRS and MODIS; GOES-16 ABI navigation
# errors are about 1.5 µrad or less (Tan et al. 2018, SPIE 10764), ~60 m at the
# western-US slant range. One 100 m term covers all three.
GEOLOC_M = 100
# Pixel size at nadir (km) for a row that carries no scan/track.
NADIR_PIXEL_KM = {'VIIRS': 0.375, 'MODIS': 1.0, 'ABI': 2.0}
# How many nearest sources are tested per detection (sources carry different buffers).
K_CANDIDATES = 8


def pixel_half_diag_m(det: dict) -> float:
    """Half the diagonal of the detection's pixel footprint, metres: the farthest a
    source inside that pixel can sit from the pixel centre FIRMS reports.
    Uses the row's own scan/track (km, as FIRMS publishes them); otherwise the
    sensor's nadir pixel."""
    try:
        scan = float(det.get('scan'))
        track = float(det.get('track'))
        if scan > 0 and track > 0:
            return 500.0 * math.hypot(scan, track)
    except (TypeError, ValueError):
        pass
    sensor = str(det.get('sensor') or det.get('instrument') or '').upper()
    if not sensor:
        sat = str(det.get('satellite') or '').strip()
        sensor = 'MODIS' if sat in ('T', 'A', 'Terra', 'Aqua') else 'VIIRS'
    px = NADIR_PIXEL_KM.get(sensor, NADIR_PIXEL_KM['VIIRS'])
    return 500.0 * math.hypot(px, px)


def effective_buffer_m(source_buffer_m: float, det: dict) -> float:
    """The known-source test's reach for this source and this detection."""
    return float(source_buffer_m) + pixel_half_diag_m(det) + GEOLOC_M


def _flag_for_class(cls) -> str:
    if cls == 'volcano':
        return 'proximate_volcano'
    if cls == 'industrial':
        return 'proximate_industrial'
    if cls == 'solar':
        return 'proximate_solar'
    return 'proximate_' + str(cls)


def _http_json(url: str) -> Optional[dict]:
    """Best-effort fetch of a JSON URL. Returns None on any failure — the
    deconfliction layer is a nice-to-have, not a fail-hard dep."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)',
            'Accept-Encoding': 'gzip',
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
            # Handle gzip manually — urllib doesn't auto-decompress
            if resp.headers.get('Content-Encoding') == 'gzip':
                import gzip
                body = gzip.decompress(body)
            return json.loads(body.decode('utf-8', errors='replace'))
    except Exception as e:
        sys.stderr.write(f'[deconflict] fetch failed for {url}: {e}\n')
        return None


class Deconflicter:
    """Load-once, enrich-many. Instantiate at pipeline start; call enrich()
    for each detection.

    Attributes:
        loaded          bool — True if reference data was fetched and indexed
        n_sources       int  — number of point sources in the index
        has_land        bool — True if the land polygon was loaded (offshore test enabled)
    """

    def __init__(self, legacy_reach: bool = False) -> None:
        # legacy_reach (2026-11-16 review): the pre-2026-11-16 test — the NEAREST source only, distance <= buffer_m — for callers
        # that pass no pixel size (the geostationary feeds via _shared/geo_common.py, which add their own pixel-scale floor)
        self.legacy_reach = legacy_reach
        self.loaded = False
        self.has_land = False
        self.n_sources = 0
        self._tree = None                  # sklearn BallTree if available, else None
        self._lats_rad: list[float] = []   # parallel arrays, brute-force path
        self._lngs_rad: list[float] = []
        self._props: list[dict] = []
        self._land_bboxes: list[tuple[float, float, float, float]] = []  # (minlat,maxlat,minlng,maxlng)
        self._land_polys: list[list[list[list[float]]]] = []             # per-poly list of rings

    def load(self, land: bool = True, cache_path: Optional[str] = None, cache_max_age_s: int = 6 * 3600) -> None:
        """Fetch both reference layers and build the spatial index. Idempotent.
        land=False skips the land polygons (the offshore test is disabled anyway).
        cache_path keeps the reference on disk for cache_max_age_s, for a feed that
        runs every few minutes (the reference itself is rebuilt weekly)."""
        if self.loaded:
            return
        fc = None
        if cache_path and os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < cache_max_age_s:
            try:
                with open(cache_path) as f:
                    fc = json.load(f)
                sys.stderr.write(f'[deconflict] reference from cache {cache_path}\n')
            except (OSError, ValueError):
                fc = None
        if fc is None:
            sys.stderr.write('[deconflict] fetching industrial_sites.min.geojson...\n')
            fc = _http_json(INDUSTRIAL_SITES_URL)
            if fc and cache_path and isinstance(fc.get('features'), list):
                try:
                    tmp = cache_path + '.tmp'
                    with open(tmp, 'w') as f:
                        json.dump(fc, f, separators=(',', ':'))
                    os.replace(tmp, cache_path)
                except OSError:
                    pass
        if not fc or not isinstance(fc.get('features'), list):
            sys.stderr.write('[deconflict] industrial sites unavailable; enrichment disabled\n')
            return
        self.load_features(fc['features'])
        if land:
            self._load_land()
        self.loaded = True

    def load_features(self, feats: list) -> None:
        """Index a list of GeoJSON Point features (the reference, or a test's)."""
        for f in feats:
            geom = f.get('geometry') or {}
            if geom.get('type') != 'Point':
                continue
            coords = geom.get('coordinates') or []
            if len(coords) < 2:
                continue
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue
            self._lats_rad.append(math.radians(lat))
            self._lngs_rad.append(math.radians(lng))
            self._props.append(f.get('properties') or {})
        self.n_sources = len(self._props)
        sys.stderr.write(f'[deconflict] indexed {self.n_sources} point sources\n')

        # Prefer sklearn BallTree when available — O(log N) per query, negligible at 5k sources
        try:
            from sklearn.neighbors import BallTree
            import numpy as np
            if self.n_sources > 0:
                pts = np.column_stack([self._lats_rad, self._lngs_rad])
                self._tree = BallTree(pts, metric='haversine')
                sys.stderr.write('[deconflict] BallTree indexed\n')
        except ImportError:
            sys.stderr.write('[deconflict] sklearn not available — brute-force nearest-neighbor\n')

        self.loaded = True

    def _load_land(self) -> None:
        # Land polygon (offshore test)
        sys.stderr.write('[deconflict] fetching land_10m.geojson...\n')
        land_fc = _http_json(LAND_POLY_URL)
        if land_fc and isinstance(land_fc.get('features'), list):
            for f in land_fc['features']:
                self._walk_geometry(f.get('geometry') or {})
            self.has_land = bool(self._land_polys)
            sys.stderr.write(f'[deconflict] indexed {len(self._land_polys)} land polygons\n')

    def _walk_geometry(self, geom: dict) -> None:
        """Recursively unwrap a GeoJSON geometry (Polygon / MultiPolygon /
        GeometryCollection) into flat Polygon rings. Natural Earth ships some
        land features as GeometryCollection (e.g., Antarctica), so we must
        recurse — the v0.1 flat-type-switch silently skipped all 11 features."""
        if not geom: return
        t = geom.get('type')
        if t == 'Polygon':
            self._add_polygon(geom.get('coordinates') or [])
        elif t == 'MultiPolygon':
            for poly in geom.get('coordinates') or []:
                self._add_polygon(poly)
        elif t == 'GeometryCollection':
            for g in geom.get('geometries') or []:
                self._walk_geometry(g)

    def _add_polygon(self, coords: list) -> None:
        """Store polygon + bbox for fast pre-filtering."""
        if not coords or not coords[0]:
            return
        outer = coords[0]
        lats = [pt[1] for pt in outer if len(pt) >= 2]
        lngs = [pt[0] for pt in outer if len(pt) >= 2]
        if not lats or not lngs:
            return
        self._land_bboxes.append((min(lats), max(lats), min(lngs), max(lngs)))
        self._land_polys.append(coords)

    def _candidates(self, lat: float, lng: float) -> list:
        """The K nearest sources as (index, distance_m), nearest first."""
        lat_r = math.radians(lat)
        lng_r = math.radians(lng)
        k = min(K_CANDIDATES, self.n_sources)
        if self._tree is not None:
            import numpy as np
            dist_rad, idx = self._tree.query(np.array([[lat_r, lng_r]]), k=k)
            return [(int(i), float(d) * EARTH_RADIUS_M) for d, i in zip(dist_rad[0], idx[0])]
        ds = sorted((_haversine_rad(lat_r, lng_r, self._lats_rad[i], self._lngs_rad[i]), i) for i in range(self.n_sources))
        return [(i, d * EARTH_RADIUS_M) for d, i in ds[:k]]

    def _judge(self, det: dict, cands: list) -> dict:
        """The enrichment fields from the candidate sources (nearest first): the
        nearest source that covers the detection (see effective_buffer_m) names the
        flag; with none covering, the nearest source is named and the flag stays."""
        out = {
            'nearest_infra_m':     None,
            'nearest_infra_id':    None,
            'nearest_infra_class': None,
            'nearest_infra_name':  None,
            'deconfliction_flag':  'clear',
        }
        # Confidence-low is authoritative regardless of proximity — NASA says
        # dropping confidence=low kills ~80% of false positives. We flag but
        # do not drop (life-safety default: never suppress silently).
        #
        # VIIRS emits single-letter codes ('l'/'n'/'h') NOT the full words
        # ('low'/'nominal'/'high'). MODIS emits 0-100 integers. Handle all
        # three shapes. Bug caught 2026-07-02 operator UAT: my initial
        # `conf == 'low'` string check missed 1,529 confidence='l' rows
        # (~6% of the feed) — those rows were silently marked 'clear'
        # despite being NASA's own drop-these signal.
        conf_raw = det.get('confidence')
        conf_str = str(conf_raw).strip().lower() if conf_raw is not None else ''
        if conf_str in ('l', 'low') or (conf_str.isdigit() and int(conf_str) < 30):   # MODIS numeric: <30 = low
            out['deconfliction_flag'] = 'low_confidence'
        if not cands:
            return out
        pick = None
        for i, dist_m in (cands[:1] if self.legacy_reach else cands):
            props = self._props[i]
            buffer_m = props.get('buffer_m') or _default_buffer(props.get('class'))
            reach = buffer_m if self.legacy_reach else effective_buffer_m(buffer_m, det)
            if dist_m <= reach:
                pick = (i, dist_m)
                break
        i, dist_m = pick or cands[0]
        props = self._props[i]
        out['nearest_infra_m'] = int(dist_m)
        out['nearest_infra_id'] = props.get('source_id')
        out['nearest_infra_class'] = props.get('class')
        out['nearest_infra_name'] = props.get('name')
        # confidence-low outranks proximate flags — if it's low-conf AND near an
        # industrial site, both facts are true but low_confidence is the more
        # actionable filter (operator picks the button that hides low-conf globally).
        if pick and out['deconfliction_flag'] == 'clear':
            out['deconfliction_flag'] = _flag_for_class(props.get('class'))
        return out

    # v2 — OFFSHORE flagging DISABLED (was: point-in-Natural-Earth-10m-land).
    # Rationale: Natural Earth 10m is a 1:10M-scale generalized coastline.
    # VIIRS 375m native pixel + ~125m FIRMS geolocation error means a
    # LEGITIMATE coastal wildfire commonly plots outside the simplified
    # polygon — a false-positive-false-positive. Operator screenshot
    # 2026-07-02 showed CONUS-wide offshore mis-flagging (St. Louis,
    # Chicago, Nashville, Birmingham all tagged offshore).
    #
    # The right fix is a distance-INTO-water threshold (require ≥2km
    # from any land polygon before flagging), but that needs a proper
    # spatial index against the polygon boundary distance which we
    # haven't built. Until then: keep the industrial / volcano /
    # nuclear / low_confidence flags (accurate + operator-actionable)
    # and skip offshore entirely.
    #
    # NOTE: has_land / _walk_geometry / _is_on_land are still loaded
    # so we can re-enable in v2 with the distance-threshold fix. No
    # code deletion — just don't emit the flag.

    def enrich(self, det: dict) -> dict:
        """Given a detection dict with lat/lng/confidence keys (and, when known,
        scan/track in km and sensor), return a new dict of enrichment fields to
        merge in. Never mutates the input."""
        lat = det.get('lat')
        lng = det.get('lng')
        if lat is None or lng is None or not (self.loaded and self.n_sources > 0):
            return self._judge(det, [])
        return self._judge(det, self._candidates(lat, lng))

    def enrich_many(self, dets: list) -> None:
        """Batched enrich() for the global feed (~300k detections per cycle).
        Same rules as enrich(), but one vectorised BallTree query (K nearest)
        instead of one numpy round-trip per detection. Updates dets in place.
        Falls back to per-row enrich() if numpy/sklearn are missing."""
        if not dets:
            return
        if self._tree is None or not (self.loaded and self.n_sources > 0):
            for d in dets:
                d.update(self.enrich(d))
            return
        import numpy as np
        pts = np.radians(np.array([[d['lat'], d['lng']] for d in dets], dtype=float))
        k = min(K_CANDIDATES, self.n_sources)
        dist_rad, idx = self._tree.query(pts, k=k)
        for n, d in enumerate(dets):
            cands = [(int(i), float(dr) * EARTH_RADIUS_M) for dr, i in zip(dist_rad[n], idx[n])]
            d.update(self._judge(d, cands))

    def _nearest(self, lat: float, lng: float) -> tuple[Optional[int], float]:
        """Return (index, distance_m) of the closest source."""
        c = self._candidates(lat, lng) if self.n_sources else []
        return c[0] if c else (None, float('inf'))

    def _is_on_land(self, lat: float, lng: float) -> bool:
        """Return True if (lat, lng) is inside any land polygon."""
        for bbox, poly in zip(self._land_bboxes, self._land_polys):
            min_lat, max_lat, min_lng, max_lng = bbox
            if lat < min_lat or lat > max_lat or lng < min_lng or lng > max_lng:
                continue
            if _point_in_polygon(lng, lat, poly):
                return True
        return False


def _default_buffer(cls: Optional[str]) -> int:
    if cls == 'volcano':
        return 5000
    if cls == 'solar':
        return 375
    return 500


def _haversine_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in radians (multiply by EARTH_RADIUS_M for meters)."""
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(min(1.0, math.sqrt(a)))


def _point_in_polygon(x: float, y: float, poly_rings: list) -> bool:
    """Ray-casting point-in-polygon. poly_rings is [[outer], [hole1], ...]
    Point is 'inside' if inside outer ring AND not inside any hole.
    """
    if not poly_rings:
        return False
    if not _in_ring(x, y, poly_rings[0]):
        return False
    for hole in poly_rings[1:]:
        if _in_ring(x, y, hole):
            return False
    return True


def _in_ring(x: float, y: float, ring: list) -> bool:
    """Even-odd ray casting on a single ring."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside
