import json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect
import notify


class TestSeed(unittest.TestCase):
    def test_seed_schema(self):
        with open(os.path.join(os.path.dirname(collect.__file__), "cds_seed.json"),
                  encoding="utf-8") as f:
            seed = json.load(f)
        for e in collect.ENTITIES:
            self.assertIn(e["key"], seed, f"{e['key']} 시드 누락")
            for row in seed[e["key"]]:
                self.assertEqual(len(row), 3)
                self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2}$")
                self.assertGreater(float(row[1]), 0)

    def test_basket_weights(self):
        for e in collect.ENTITIES:
            self.assertAlmostEqual(sum(e["basket"].values()), 1.0, places=6)

    def test_csi_weights_sum_to_one(self):
        total = collect.W_DRAWDOWN + collect.W_VOL + collect.W_MOMENTUM + collect.W_MACRO
        self.assertAlmostEqual(total, 1.0, places=6)


class TestMessage(unittest.TestCase):
    SAMPLE = {
        "asof": "2026-08-03",
        "generated_at": "2026-08-04T07:30:00+09:00",
        "macro": {"bbb_oas_bp": 99.0, "percentile_2y": 0.27, "series": []},
        "entities": [
            {"key": "oracle", "name": "Oracle", "type": "traded", "note": "n",
             "basket": {"ORCL": 1.0}, "csi": 86.9, "csi_prev": 82.4, "csi_hist": [],
             "cds": {"last": 215.0, "last_date": "2026-07-28", "source": "x",
                     "ytd_low": 145.0, "ytd_high": 215.0, "series": []}},
            {"key": "anthropic", "name": "Anthropic", "type": "proxy", "note": "n",
             "basket": {"GOOGL": 0.7, "AMZN": 0.3}, "csi": 41.5, "csi_prev": 46.2,
             "csi_hist": [], "cds": {"last": 67.0, "last_date": "2026-07-27",
                                     "source": "x", "ytd_low": 67.0, "ytd_high": 67.0,
                                     "series": []}},
        ],
        "candidates": [],
    }

    def test_renders_without_credentials(self):
        msg = notify.build_message(self.SAMPLE)
        self.assertIn("Oracle", msg)
        self.assertIn("215bp", msg)
        self.assertIn("▲", msg)
        self.assertIn("▼", msg)

    def test_band_boundaries(self):
        self.assertEqual(notify.band(75.0)[1], "과열")
        self.assertEqual(notify.band(74.9)[1], "확대")
        self.assertEqual(notify.band(39.9)[1], "안정")

    def test_stale_warning(self):
        stale = dict(self.SAMPLE, asof="2020-01-01")
        self.assertIn("경과", notify.build_message(stale))

    def test_missing_cds_is_tolerated(self):
        d = json.loads(json.dumps(self.SAMPLE))
        d["entities"][0]["cds"]["last"] = None
        self.assertIn("Oracle", notify.build_message(d))


class TestSaveJson(unittest.TestCase):
    def test_idempotent_write(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "sub", "d.json")
            obj = {"generated_at": "1", "a": 1}
            self.assertTrue(collect.save_json(p, obj))
            self.assertFalse(collect.save_json(p, {"generated_at": "2", "a": 1}))
            self.assertTrue(collect.save_json(p, {"generated_at": "2", "a": 2}))


if __name__ == "__main__":
    unittest.main()
