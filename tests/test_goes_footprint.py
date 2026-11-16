"""GOES FDC cells: the pixel footprint and the known-source flag (feeds/goes_fire/fetch_fdc.py, 2026-11-16).
Offline (the reference is local). Run: python -m unittest tests/test_goes_footprint.py"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'feeds', 'firms'))
sys.path.insert(0, os.path.join(HERE, '..', 'feeds', 'goes_fire'))
import deconflict as DC  # noqa: E402
import fetch_fdc as F  # noqa: E402


class Footprint(unittest.TestCase):
    def test_sub_satellite_point_is_2km(self):
        ew, ns = F.pixel_km(F._Proj(-137.0), 0.0, 0.0)
        self.assertAlmostEqual(float(ew), 2.00, delta=0.01)
        self.assertAlmostEqual(float(ns), 2.00, delta=0.02)

    def test_inverse_round_trip(self):
        p = F._Proj(-137.0)
        for lat, lon in [(40.0, -120.0), (33.0, -112.5), (48.5, -104.0), (36.6, -121.9)]:
            x, y = F._scan_angles(p, lat, lon)
            la, lo, ok = F._geo_latlon(p, np.array([x]), np.array([y]))
            self.assertTrue(bool(ok[0]))
            self.assertAlmostEqual(float(la[0]), lat, places=6)
            self.assertAlmostEqual(float(lo[0]), lon, places=6)

    def test_western_us_pixels_are_larger(self):
        sizes = {k: F.pixel_km_at('G18', *k) for k in [(32.5, -117.0), (40.0, -120.0), (42.7, -111.6), (48.0, -104.0)]}
        for (lat, lon), (ew, ns) in sizes.items():
            self.assertGreater(ew, 2.0)
            self.assertGreater(ns, 2.5)
            self.assertLess(max(ew, ns), 6.0)
        print('\n  GOES-18 FDC pixel (EW × NS km): ' + '; '.join(f'{lat},{lon}: {ew:.2f} × {ns:.2f}' for (lat, lon), (ew, ns) in sizes.items()))


class Cells(unittest.TestCase):
    def setUp(self):
        feats = [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-111.593, 42.688]},
                  'properties': {'class': 'persistent_other', 'buffer_m': 550, 'name': 'PERSISTENT THERMAL 42.688,-111.593'}}]

        class Local(DC.Deconflicter):
            def load(self, land=True, cache_path=None, cache_max_age_s=0):
                self.load_features(feats)
        self._orig = F.Deconflicter
        F.Deconflicter = Local

    def tearDown(self):
        F.Deconflicter = self._orig

    def test_flag_never_drop(self):
        pk = [round(v, 2) for v in F.pixel_km_at('G18', 42.688, -111.593)]
        dets = [{'lat': 42.706, 'lng': -111.593, 'frp': 40.0, 'sat': 'G18', 'pix_km': pk},   # 2.0 km north
                {'lat': 42.760, 'lng': -111.593, 'frp': 300.0, 'sat': 'G18', 'pix_km': pk},  # 8.0 km north
                {'lat': 42.700, 'lng': -111.593, 'frp': 10.0, 'sat': 'G18', 'pix_km': None}]  # no footprint: 2 km nominal
        meta = F.deconflict_cells(dets)
        self.assertEqual(len(dets), 3)
        self.assertEqual(dets[0]['deconfliction_flag'], 'proximate_persistent_other')
        self.assertEqual(dets[0]['nearest_infra_m'], 2001)
        self.assertEqual(dets[1]['deconfliction_flag'], 'clear')
        self.assertNotIn('nearest_infra_name', dets[1])
        self.assertEqual(dets[2]['deconfliction_flag'], 'proximate_persistent_other')
        self.assertEqual(meta['by_flag'], {'proximate_persistent_other': 2, 'clear': 1})
        self.assertTrue(meta['reference_loaded'])

    def test_reference_down_publishes_clear(self):
        class Down(DC.Deconflicter):
            def load(self, land=True, cache_path=None, cache_max_age_s=0):
                return
        F.Deconflicter = Down
        dets = [{'lat': 42.70, 'lng': -111.59, 'frp': 40.0, 'pix_km': [3.0, 3.5]}]
        meta = F.deconflict_cells(dets)
        self.assertEqual(dets[0]['deconfliction_flag'], 'clear')
        self.assertFalse(meta['reference_loaded'])


if __name__ == '__main__':
    unittest.main()
