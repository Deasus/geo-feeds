"""Himawari / Meteosat / IODC (feeds/_shared/geo_common.py): their deconfliction keeps the reach it had before 2026-11-16
(nearest source, buffer_m, plus the feed's min_buffer_m), because their rows carry no pixel size. Offline.
Run: python -m unittest tests/test_geo_common_reach.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'feeds', 'firms'))
sys.path.insert(0, os.path.join(HERE, '..', 'feeds', '_shared'))
import deconflict as DC  # noqa: E402
import geo_common as GC  # noqa: E402

KM_LAT = 111.19493


def feat(lat, lng, cls, buf, name):
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lng, lat]}, 'properties': {'class': cls, 'buffer_m': buf, 'name': name}}


REF = [feat(-8.0, 115.0, 'volcano', 5000, 'A VOLCANO'), feat(-30.0, 120.0, 'industrial', 500, 'A MINE'), feat(-30.0 + 4.5 / KM_LAT, 120.0, 'volcano', 5000, 'ANOTHER VOLCANO')]


class Reach(unittest.TestCase):
    def setUp(self):
        self._orig = DC.Deconflicter.load

        def load(dc, land=True, cache_path=None, cache_max_age_s=0):
            dc.load_features(REF)
        DC.Deconflicter.load = load

    def tearDown(self):
        DC.Deconflicter.load = self._orig

    def det(self, lat, lng):
        return {'lat': lat, 'lng': lng, 'frp': 50.0, 'conf': 80, 'tier': 'good', 'sat': 'H9', 'age_sec': 60}

    def test_heat_5_2_km_from_a_volcano_stays_shown(self):
        d = self.det(-8.0 + 5.2 / KM_LAT, 115.0)
        shown, suppressed, meta = GC.deconflict([d], min_buffer_m=2500)
        self.assertEqual(len(shown), 1, 'shown before 2026-11-16 (5.2 km > 5 km), shown now')
        # the pixel-sized test would have moved it (5.0 + 0.265 + 0.1 km reach)
        full = DC.Deconflicter(); full.load()
        self.assertEqual(full.enrich({'lat': d['lat'], 'lng': d['lng'], 'confidence': None})['deconfliction_flag'], 'proximate_volcano')

    def test_only_the_nearest_source_is_tested(self):
        # 0.6 km south of the mine (the nearest source, buffer 500 m), 5.1 km from the volcano north of it (buffer 5 km)
        lat, lng = -30.0 - 0.6 / KM_LAT, 120.0
        legacy = DC.Deconflicter(legacy_reach=True); legacy.load()
        r = legacy.enrich({'lat': lat, 'lng': lng, 'confidence': None})
        self.assertEqual((r['nearest_infra_name'], r['deconfliction_flag']), ('A MINE', 'clear'), 'legacy: the nearest only, 600 m > 500 m')
        full = DC.Deconflicter(); full.load()
        self.assertEqual(full.enrich({'lat': lat, 'lng': lng, 'confidence': None})['deconfliction_flag'], 'proximate_industrial',
                         'the pixel-sized test: 500 + 265 + 100 m covers 600 m')
        shown, suppressed, _ = GC.deconflict([self.det(lat, lng)], min_buffer_m=0)
        self.assertEqual(len(shown), 1, 'geo_common keeps the legacy reach')

    def test_min_buffer_floor_unchanged(self):
        d = self.det(-30.0 - 2.0 / KM_LAT, 120.0)
        shown, suppressed, meta = GC.deconflict([d], min_buffer_m=2500)
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0][2], 'proximate_industrial')
        self.assertEqual(meta['min_buffer_m'], 2500)


if __name__ == '__main__':
    unittest.main()
