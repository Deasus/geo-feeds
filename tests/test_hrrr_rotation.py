"""HRRR winds: grid-relative u/v → earth-relative (feeds/hrrr/fetch_hrrr.py, 2026-11-16).

Run:  python -m unittest tests/test_hrrr_rotation.py
      HRRR_TEST_GRIB=/path/to/hrrr.wrfsfcf00.grib2 python -m unittest tests/test_hrrr_rotation.py
The GRIB file needs the UGRD/VGRD 10 m messages (any HRRR CONUS or Alaska cycle). With it, the
rotation is checked against the grid's OWN geometry: the true bearing of the grid's x and y axes,
measured from neighbouring grid points' latitudes and longitudes, independent of the formula.
"""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'feeds', 'hrrr'))
import fetch_hrrr as H  # noqa: E402

LAMBERT = {'gridType': 'lambert', 'LoVInDegrees': 262.5, 'Latin1InDegrees': 38.5, 'Latin2InDegrees': 38.5,
           'resolutionAndComponentFlags': 56}   # 0b00111000: bit 5 set = grid-relative (as HRRR CONUS)


def from_deg(u, v):
    return (270.0 - math.degrees(math.atan2(v, u))) % 360.0


class Synthetic(unittest.TestCase):
    def test_flag_bit5(self):
        self.assertTrue(H.uv_relative_to_grid({'resolutionAndComponentFlags': 56}))
        self.assertFalse(H.uv_relative_to_grid({'resolutionAndComponentFlags': 48}))
        self.assertTrue(H.uv_relative_to_grid({'uvRelativeToGrid': 1}))
        self.assertIsNone(H.uv_relative_to_grid({}))

    def test_earth_relative_untouched(self):
        g = dict(LAMBERT, resolutionAndComponentFlags=48)
        u, v, info = H.rotate_to_earth(np.array([3.0]), np.array([4.0]), np.array([-122.0]), g)
        self.assertFalse(info['rotated'])
        self.assertEqual((float(u[0]), float(v[0])), (3.0, 4.0))

    def test_no_rotation_on_the_central_meridian(self):
        u, v, info = H.rotate_to_earth(np.array([3.0]), np.array([4.0]), np.array([-97.5]), LAMBERT)
        self.assertTrue(info['rotated'])
        self.assertAlmostEqual(float(u[0]), 3.0, places=9)
        self.assertAlmostEqual(float(v[0]), 4.0, places=9)

    def test_west_coast_angle_and_speed(self):
        # at 122°W the grid's y axis leans sin(38.5°)·(−24.5°) = −15.25° from true north: a wind blowing
        # toward grid +y blows toward 344.75° (true), i.e. FROM 164.75°; speed unchanged
        u, v, info = H.rotate_to_earth(np.array([0.0]), np.array([10.0]), np.array([-122.0]), LAMBERT)
        ang = math.sin(math.radians(38.5)) * (-24.5)
        self.assertAlmostEqual(info['n'], math.sin(math.radians(38.5)), places=6)
        self.assertAlmostEqual(math.hypot(float(u[0]), float(v[0])), 10.0, places=9)
        self.assertAlmostEqual(from_deg(float(u[0]), float(v[0])), (180.0 + ang) % 360, places=6)
        # east of LoV the lean is the other way
        u2, v2, _ = H.rotate_to_earth(np.array([0.0]), np.array([10.0]), np.array([-75.0]), LAMBERT)
        self.assertGreater(float(u2[0]), 0.0)
        self.assertLess(float(u[0]), 0.0)

    def test_longitudes_in_0_360_and_shapes(self):
        lng = np.array([[237.5, 262.5], [287.5, -122.5]])
        u, v, _ = H.rotate_to_earth(np.ones((2, 2)), np.zeros((2, 2)), lng, LAMBERT)
        self.assertEqual(u.shape, (2, 2))
        self.assertAlmostEqual(float(u[0, 0]), float(u[1, 1]), places=9)   # 237.5°E = 122.5°W
        self.assertAlmostEqual(float(v[0, 1]), 0.0, places=9)

    def test_two_standard_parallels(self):
        n = H.rotation_constant({'gridType': 'lambert', 'Latin1InDegrees': 33.0, 'Latin2InDegrees': 45.0})
        self.assertAlmostEqual(n, 0.630477, places=5)   # the CONUS Albers/LCC textbook pair

    def test_polar_stereographic(self):
        self.assertEqual(H.rotation_constant({'gridType': 'polar_stereographic', 'projectionCentreFlag': 0}), 1.0)
        self.assertEqual(H.rotation_constant({'gridType': 'polar_stereographic', 'projectionCentreFlag': 128}), -1.0)

    def test_unknown_projection_refuses(self):
        with self.assertRaises(RuntimeError):
            H.rotate_to_earth(np.ones(1), np.ones(1), np.zeros(1), {'gridType': 'mercator', 'resolutionAndComponentFlags': 8})
        with self.assertRaises(RuntimeError):
            H.rotate_to_earth(np.ones(1), np.ones(1), np.zeros(1), {'gridType': 'lambert'})


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    return np.arctan2(np.sin(dl) * np.cos(p2), np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl))


@unittest.skipUnless(os.environ.get('HRRR_TEST_GRIB'), 'set HRRR_TEST_GRIB to a saved HRRR wrfsfcf00 file')
class RealGrib(unittest.TestCase):
    def test_against_the_grids_own_geometry(self):
        ds = H._open_filtered_dataset(os.environ['HRRR_TEST_GRIB'], {'typeOfLevel': 'heightAboveGround', 'level': 10}, read_keys=H.GRID_KEYS)
        g = H.grid_of(ds['u10'].attrs)
        self.assertTrue(H.uv_relative_to_grid(g), f'HRRR should be grid-relative: {g}')
        ue, ve, info = H._earth_relative_winds(ds)
        u, v = ds['u10'].values.astype(float), ds['v10'].values.astype(float)
        lat, lon = ds.latitude.values, ds.longitude.values
        # interior sample: the x axis's true bearing from the neighbours left/right, y from below/above
        J, I = np.meshgrid(np.arange(5, lat.shape[0] - 5, 37), np.arange(5, lat.shape[1] - 5, 41), indexing='ij')
        ax = _bearing(lat[J, I - 1], lon[J, I - 1], lat[J, I + 1], lon[J, I + 1])
        ay = _bearing(lat[J - 1, I], lon[J - 1, I], lat[J + 1, I], lon[J + 1, I])
        east = u[J, I] * np.sin(ax) + v[J, I] * np.sin(ay)
        north = u[J, I] * np.cos(ax) + v[J, I] * np.cos(ay)
        ref = np.degrees(np.arctan2(east, north))
        got = np.degrees(np.arctan2(ue[J, I], ve[J, I]))
        diff = np.abs((got - ref + 540) % 360 - 180)
        strong = np.hypot(u[J, I], v[J, I]) > 1.0
        p99 = float(np.percentile(diff[strong], 99))
        unrot = np.degrees(np.arctan2(u[J, I], v[J, I]))
        before = np.abs((unrot - ref + 540) % 360 - 180)
        print(f"\n  {info['gridType']}: {strong.sum()} points; rotated vs grid geometry: median {np.median(diff[strong]):.3f}°, "
              f"p99 {p99:.3f}°, max {diff[strong].max():.3f}° · unrotated was off by a median {np.median(before[strong]):.2f}° "
              f"(max {before[strong].max():.2f}°) · {info}")
        self.assertLess(p99, 0.5)
        self.assertTrue(np.allclose(np.hypot(ue, ve), np.hypot(u, v), atol=1e-4))


if __name__ == '__main__':
    unittest.main()
