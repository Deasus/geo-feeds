"""Review P4: the reference cache (deconflict.load) and the FIRMS rows' scan/track (used by the known-source test, then
dropped so the published rows are unchanged). Offline: the fetch is replaced. Run: python -m unittest tests/test_cache_and_slim.py"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'feeds', 'firms'))
import deconflict as DC  # noqa: E402
os.environ.setdefault('FIRMS_PUBLIC_FALLBACK', '1')   # importing the module needs a key or the keyless mode; nothing is fetched
import fetch_firms as FF  # noqa: E402

REF = {'features': [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-112.0, 40.0]}, 'properties': {'class': 'industrial', 'buffer_m': 500, 'name': 'CACHED PLANT'}}]}
NEW = {'features': REF['features'] + [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-111.0, 41.0]}, 'properties': {'class': 'industrial', 'buffer_m': 500, 'name': 'NEW PLANT'}}]}


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, '_industrial_cache.json')
        self.orig = DC._http_json
        self.fetches = 0

    def tearDown(self):
        DC._http_json = self.orig

    def fetch_returns(self, value):
        def f(url):
            self.fetches += 1
            return value
        DC._http_json = f

    def write_cache(self, obj, age_s):
        with open(self.cache, 'w') as f:
            json.dump(obj, f)
        t = time.time() - age_s
        os.utime(self.cache, (t, t))

    def test_fresh_cache_is_read_without_a_fetch(self):
        self.write_cache(REF, 60)
        self.fetch_returns(NEW)
        d = DC.Deconflicter(); d.load(land=False, cache_path=self.cache)
        self.assertEqual((d.loaded, d.n_sources, self.fetches, d.stale_cache), (True, 1, 0, False))

    def test_expired_cache_is_refreshed_when_the_fetch_works(self):
        self.write_cache(REF, 7 * 3600)
        self.fetch_returns(NEW)
        d = DC.Deconflicter(); d.load(land=False, cache_path=self.cache)
        self.assertEqual((d.n_sources, self.fetches, d.stale_cache), (2, 1, False))
        self.assertEqual(len(json.load(open(self.cache))['features']), 2, 'the cache file is rewritten')

    def test_expired_cache_is_used_when_the_fetch_fails(self):
        self.write_cache(REF, 30 * 3600)
        self.fetch_returns(None)
        d = DC.Deconflicter(); d.load(land=False, cache_path=self.cache)
        self.assertEqual((d.loaded, d.n_sources, self.fetches, d.stale_cache), (True, 1, 1, True))

    def test_no_cache_and_no_fetch_disables_enrichment(self):
        self.fetch_returns(None)
        d = DC.Deconflicter(); d.load(land=False, cache_path=self.cache)
        self.assertFalse(d.loaded)
        self.assertEqual(d.enrich({'lat': 40.0, 'lng': -112.0, 'confidence': 'n'})['deconfliction_flag'], 'clear')


class Slim(unittest.TestCase):
    def test_scan_track_reach_the_test_and_never_the_output(self):
        seen = []

        class Stub:
            def enrich_many(self, rows):
                for r in rows:
                    seen.append((r.get('scan'), r.get('track')))
                    r['deconfliction_flag'] = 'clear'
        orig = FF._get_deconflicter
        FF._get_deconflicter = lambda: Stub()
        try:
            rows = FF._enrich_and_slim([{'lat': 40.0, 'lng': -112.0, 'confidence': 'n', 'scan': '0.54', 'track': '0.42'}, {'lat': 41.0, 'lng': -112.0, 'confidence': '80', 'scan': None, 'track': None}])
        finally:
            FF._get_deconflicter = orig
        self.assertEqual(seen, [('0.54', '0.42'), (None, None)])
        self.assertTrue(all('scan' not in r and 'track' not in r for r in rows))
        self.assertTrue(all(r['deconfliction_flag'] == 'clear' for r in rows))

    def test_a_failing_enrichment_still_slims(self):
        class Boom:
            def enrich_many(self, rows):
                raise RuntimeError('reference down')
        orig = FF._get_deconflicter
        FF._get_deconflicter = lambda: Boom()
        try:
            rows = FF._enrich_and_slim([{'lat': 40.0, 'lng': -112.0, 'scan': '0.4', 'track': '0.4'}])
        finally:
            FF._get_deconflicter = orig
        self.assertEqual(rows, [{'lat': 40.0, 'lng': -112.0, 'deconfliction_flag': 'clear'}])


if __name__ == '__main__':
    unittest.main()
