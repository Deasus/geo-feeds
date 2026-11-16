"""NASA static land sources from the FIRMS archive (feeds/industrial/fetch_nasa_static.py, 2026-11-16):
type-2 detections on a 400 m grid, cells with >= 5 in the year. Offline. Run: python -m unittest tests/test_nasa_static.py"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'feeds', 'industrial'))
import fetch_nasa_static as N  # noqa: E402


def row(lat, lng, t='2', d='2024-05-01'):
    return {'latitude': str(lat), 'longitude': str(lng), 'type': t, 'acq_date': d}


class Mine(unittest.TestCase):
    def test_cells_are_400m(self):
        i0, j0 = N.cell_of(42.0, -111.0)
        i1, _ = N.cell_of(42.0 + 0.41 / 111.195, -111.0)
        _, j1 = N.cell_of(42.0, -111.0 + 0.41 / (111.195 * math.cos(math.radians(42.0))))
        self.assertEqual(i1 - i0, 1)
        self.assertEqual(j1 - j0, 1)

    def test_only_type_2_and_at_least_5(self):
        lat, lng = N.cell_of(42.6869, -111.5913), None
        base = (42.68695, -111.59133)
        rows = [row(base[0], base[1], d=f'2024-0{k + 1}-01') for k in range(5)]            # 5 type-2 → kept
        rows += [row(40.0, -120.0) for _ in range(4)]                                      # 4 type-2 → dropped
        rows += [row(39.0, -121.0, t='0') for _ in range(50)]                              # vegetation fire, never a source
        rows += [row(38.0, -122.0, t='1') for _ in range(50)]                              # volcano typed by NASA: GVP covers it
        out = N.mine(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['n'], 5)
        self.assertAlmostEqual(out[0]['lat'], base[0], places=5)
        self.assertEqual((out[0]['first'], out[0]['last']), ('2024-01-01', '2024-05-01'))


if __name__ == '__main__':
    unittest.main()
