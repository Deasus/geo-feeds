"""The known-source test (feeds/firms/deconflict.py, 2026-11-16): a source explains a detection when
it can lie inside the detection's pixel — distance <= buffer_m + half the pixel diagonal + 100 m.
Offline: the reference is a handful of local features. Run: python -m unittest tests/test_deconflict_buffers.py"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'feeds', 'firms'))
import deconflict as DC  # noqa: E402

M_PER_DEG_LAT = 111194.9


def feat(lat, lng, cls, buf=None, name='src'):
    p = {'class': cls, 'name': name, 'source_id': f'test:{name}'}
    if buf is not None:
        p['buffer_m'] = buf
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lng, lat]}, 'properties': p}


def north(lat, lng, metres):
    return lat + metres / M_PER_DEG_LAT, lng


class Buffers(unittest.TestCase):
    def setUp(self):
        self.d = DC.Deconflicter()
        self.d.load_features([feat(40.0, -112.0, 'industrial', 500, 'plant'),
                              feat(40.0, -111.0, 'volcano', 5000, 'cone'),
                              feat(41.0, -112.0, 'industrial', 500, 'near-small'),
                              feat(41.0, -111.9875, 'industrial_firms', 550, 'nasa-static')])

    def test_pixel_term_from_scan_track(self):
        self.assertAlmostEqual(DC.pixel_half_diag_m({'scan': 0.375, 'track': 0.375}), 265.2, places=1)
        self.assertAlmostEqual(DC.pixel_half_diag_m({'scan': 4.8, 'track': 2.0}), 2600.0, places=1)
        self.assertAlmostEqual(DC.pixel_half_diag_m({'sensor': 'MODIS'}), 707.1, places=1)   # no scan/track: nadir
        self.assertAlmostEqual(DC.pixel_half_diag_m({'satellite': 'T'}), 707.1, places=1)
        self.assertAlmostEqual(DC.pixel_half_diag_m({}), 265.2, places=1)
        self.assertAlmostEqual(DC.effective_buffer_m(500, {'scan': 0.375, 'track': 0.375}), 865.2, places=1)

    def test_viirs_nadir_vs_edge(self):
        lat, lng = north(40.0, -112.0, 900)            # 900 m from the plant
        nadir = self.d.enrich({'lat': lat, 'lng': lng, 'confidence': 'n', 'scan': 0.38, 'track': 0.37})
        edge = self.d.enrich({'lat': lat, 'lng': lng, 'confidence': 'n', 'scan': 0.80, 'track': 0.78})
        self.assertEqual(nadir['deconfliction_flag'], 'clear')                 # reach 865 m
        self.assertEqual(edge['deconfliction_flag'], 'proximate_industrial')    # reach 1,159 m
        self.assertEqual(edge['nearest_infra_name'], 'plant')
        self.assertEqual(edge['nearest_infra_m'], 900)

    def test_old_500m_rule_is_gone(self):
        lat, lng = north(40.0, -112.0, 700)
        r = self.d.enrich({'lat': lat, 'lng': lng, 'confidence': 'h', 'scan': 0.4, 'track': 0.4})
        self.assertEqual(r['deconfliction_flag'], 'proximate_industrial')     # was clear at 500 m

    def test_modis_scan_edge(self):
        lat, lng = north(40.0, -112.0, 2500)
        r = self.d.enrich({'lat': lat, 'lng': lng, 'confidence': '80', 'scan': 4.8, 'track': 2.0, 'sensor': 'MODIS'})
        self.assertEqual(r['deconfliction_flag'], 'proximate_industrial')
        far = self.d.enrich({'lat': north(40.0, -112.0, 3300)[0], 'lng': lng, 'confidence': '80', 'scan': 4.8, 'track': 2.0})
        self.assertEqual(far['deconfliction_flag'], 'clear')

    def test_low_confidence_outranks(self):
        lat, lng = north(40.0, -112.0, 100)
        r = self.d.enrich({'lat': lat, 'lng': lng, 'confidence': 'l'})
        self.assertEqual(r['deconfliction_flag'], 'low_confidence')
        self.assertEqual(r['nearest_infra_name'], 'plant')

    def test_a_farther_source_with_a_wider_reach_covers(self):
        # 1.20 km from the small plant (reach 865 m) but 1.30 km... from the NASA static source east of it?
        lat, lng = north(41.0, -112.0, 0)
        lng2 = lng - 0.0130   # 1.09 km west of near-small (41°N: 1° lng ≈ 83.9 km); nasa-static 2.14 km east
        r = self.d.enrich({'lat': lat, 'lng': lng2, 'confidence': 'n', 'scan': 0.375, 'track': 0.375})
        self.assertEqual(r['deconfliction_flag'], 'clear')
        # the GOES-sized pixel reaches both; the nearest covering source is named
        g = self.d.enrich({'lat': lat, 'lng': lng2, 'confidence': None, 'scan': 3.0, 'track': 3.6, 'sensor': 'ABI'})
        self.assertEqual(g['deconfliction_flag'], 'proximate_industrial')
        self.assertEqual(g['nearest_infra_name'], 'near-small')
        # 0.95 km east of near-small, 0.10 km west of the NASA static source: the static source covers; small plant does not
        h = self.d.enrich({'lat': lat, 'lng': -111.9887, 'confidence': 'n', 'scan': 0.375, 'track': 0.375})
        self.assertEqual(h['deconfliction_flag'], 'proximate_industrial_firms')
        self.assertEqual(h['nearest_infra_name'], 'nasa-static')

    def test_volcano_reach(self):
        lat, lng = north(40.0, -111.0, 5300)
        self.assertEqual(self.d.enrich({'lat': lat, 'lng': lng, 'confidence': 'n'})['deconfliction_flag'], 'proximate_volcano')

    def test_batched_equals_single(self):
        rows = []
        for k, m in enumerate(range(0, 6000, 150)):
            lat, lng = north(40.0, -112.0, m)
            rows.append({'lat': lat, 'lng': lng + (0.01 if k % 3 else 0), 'confidence': 'n', 'scan': 0.3 + (k % 5) * 0.1, 'track': 0.4})
        single = [self.d.enrich(dict(r)) for r in rows]
        if self.d._tree is not None:
            many = [dict(r) for r in rows]
            self.d.enrich_many(many)
            for a, b in zip(single, many):
                self.assertEqual(a['deconfliction_flag'], b['deconfliction_flag'])
                self.assertEqual(a['nearest_infra_name'], b['nearest_infra_name'])

    def test_no_reference_means_clear(self):
        e = DC.Deconflicter()
        self.assertEqual(e.enrich({'lat': 40.0, 'lng': -112.0, 'confidence': 'n'})['deconfliction_flag'], 'clear')


if __name__ == '__main__':
    unittest.main()
