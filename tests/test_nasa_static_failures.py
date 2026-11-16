"""fetch_nasa_static.py failure handling (2026-11-16 review), with mocked responses — no live endpoint is touched.
Run: python -m unittest tests/test_nasa_static_failures.py"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'feeds', 'industrial'))
import fetch_nasa_static as N  # noqa: E402

Y1 = datetime.now(timezone.utc).year - 1
Y2 = Y1 - 1
CSV = ('latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight,type\n'
       + ''.join(f'42.68695,-111.59133,330,0.4,0.4,{Y1}-0{k + 1}-01,0900,N,VIIRS,n,2,290,20,D,2\n' for k in range(6))).encode()


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(url, code):
    return urllib.error.HTTPError(url, code, 'x', {}, None)


class Failures(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stage = os.path.join(self.dir, '_stage_nasa_static.json')
        self.saved = (N.OUT_JSON, N.LOCAL_DIR, N.COUNTRIES, N.urllib.request.urlopen)
        N.OUT_JSON, N.LOCAL_DIR, N.COUNTRIES = self.stage, None, ['United_States']
        os.environ.pop('NASA_STATIC_YEAR', None)
        self.calls = []

    def tearDown(self):
        N.OUT_JSON, N.LOCAL_DIR, N.COUNTRIES, N.urllib.request.urlopen = self.saved

    def serve(self, rule):
        """rule(url, ranged) → bytes, or an exception to raise."""
        def urlopen(req, timeout=None):
            url, ranged = req.full_url, req.has_header('Range')
            self.calls.append((url.rsplit('/', 1)[-1], ranged))
            out = rule(url, ranged)
            if isinstance(out, Exception):
                raise out
            return Resp(out[:1024] if ranged else out)
        N.urllib.request.urlopen = urlopen

    def put_stage(self, year, count=7):
        with open(self.stage, 'w') as f:
            json.dump({'year': year, 'countries': ['United_States'], 'grid_m': 400.0, 'count': count, 'complete': True, 'sources': [{'lat': 1, 'lng': 2, 'n': 9}] * count}, f)
        return open(self.stage).read()

    def test_happy_path_writes_and_attributes(self):
        self.serve(lambda url, ranged: CSV if f'_{Y1}_' in url else http_error(url, 404))
        self.assertEqual(N.main(), 0)
        st = json.load(open(self.stage))
        self.assertEqual((st['year'], st['count'], st['complete']), (Y1, 1, True))
        self.assertIn('FIRMS', st['attribution'])
        self.assertTrue(st['licence'])
        self.assertTrue(all(r for _, r in self.calls[:3]), 'probes are ranged GETs, not HEAD')

    def test_refused_probe_fails_and_keeps_the_stage(self):
        before = self.put_stage(Y2)
        self.serve(lambda url, ranged: http_error(url, 403) if ranged else CSV)
        self.assertEqual(N.main(), 1)
        self.assertEqual(open(self.stage).read(), before)

    def test_transient_failure_on_the_newer_year_never_falls_back(self):
        before = self.put_stage(Y1)
        self.serve(lambda url, ranged: urllib.error.URLError('timed out') if f'_{Y1}_' in url else CSV)
        self.assertEqual(N.main(), 1)
        self.assertEqual(open(self.stage).read(), before, 'the staged newer year is not replaced by the older one')
        self.assertFalse(any(f'_{Y2}_' in u for u, _ in self.calls), 'the older year is not even asked for')

    def test_one_product_404_fails_and_keeps_the_stage(self):
        before = self.put_stage(Y2)
        self.serve(lambda url, ranged: CSV if (ranged or 'modis' not in url) else http_error(url, 404))
        self.assertEqual(N.main(), 1)
        self.assertEqual(open(self.stage).read(), before, 'a partial year does not replace a complete one')

    def test_newer_not_published_keeps_a_newer_stage(self):
        before = self.put_stage(Y1)
        self.serve(lambda url, ranged: http_error(url, 404) if f'_{Y1}_' in url else CSV)
        self.assertEqual(N.main(), 0)
        self.assertEqual(open(self.stage).read(), before)

    def test_no_year_published_is_a_failure(self):
        self.serve(lambda url, ranged: http_error(url, 404))
        self.assertEqual(N.main(), 1)
        self.assertFalse(os.path.exists(self.stage))

    def test_an_empty_year_does_not_replace_sources(self):
        before = self.put_stage(Y2)
        empty = CSV.decode().replace(',2\n', ',0\n').encode()   # every row type 0: no static source
        self.serve(lambda url, ranged: empty if f'_{Y1}_' in url else http_error(url, 404))
        self.assertEqual(N.main(), 1)
        self.assertEqual(open(self.stage).read(), before)

    def test_not_a_csv_is_a_failure(self):
        self.serve(lambda url, ranged: b'<html>maintenance</html>')
        self.assertEqual(N.main(), 1)


if __name__ == '__main__':
    unittest.main()
