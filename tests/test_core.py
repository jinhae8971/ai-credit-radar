import copy, json, os, sys, tempfile, unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect
import notify


def load_seed():
    with open(os.path.join(os.path.dirname(collect.__file__), "cds_seed.json"), encoding="utf-8") as f:
        return json.load(f)


class TestSeed(unittest.TestCase):
    def test_seed_schema(self):
        seed = load_seed()
        keys = {e.get("cds_key", e["key"]) for e in collect.ENTITIES} | {m["key"] for m in collect.MEASURED}
        for k in keys:
            self.assertIn(k, seed, f"{k} 시드 누락")
            for row in seed[k]:
                self.assertEqual(len(row), 3)
                self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2}$")
                self.assertGreater(float(row[1]), 0)
            dates = [r[0] for r in seed[k]]
            self.assertEqual(len(dates), len(set(dates)), f"{k} 같은 날짜 중복 — 한 날짜에 하나만")

    def test_baselines_schema(self):
        base = load_seed()["_baselines"]
        for m in collect.MEASURED:
            lines = base[m["key"]]
            self.assertTrue(any(b["kind"] == "alert" for b in lines), f"{m['key']} 경보선 없음")
            for b in lines:
                self.assertIn(b["kind"], ("ref", "alert"))
                self.assertGreater(float(b["bp"]), 0)
                self.assertTrue(b["label"])

    def test_basket_weights(self):
        for e in collect.ENTITIES:
            self.assertAlmostEqual(sum(e["basket"].values()), 1.0, places=6)

    def test_csi_weights_sum_to_one(self):
        total = collect.W_DRAWDOWN + collect.W_VOL + collect.W_MOMENTUM + collect.W_MACRO
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_bad_rows_are_dropped(self):
        rows = collect.seed_rows({"x": [["2026-01-02", 1, "a"], ["bad", 2, "b"], [1, 2], ["2026-01-01", "3", "c"]]}, "x")
        self.assertEqual([r[0] for r in rows], ["2026-01-01", "2026-01-02"])


class TestMeasured(unittest.TestCase):
    SEED = {
        "_baselines": {
            "oracle": [{"bp": 300, "kind": "alert", "label": "경보"}, {"bp": 198, "kind": "ref", "label": "GFC"}],
            "coreweave": [{"bp": 1289, "kind": "alert", "label": "경보"}],
        },
        "oracle": [["2026-10-19", 295.0, "a"], ["2026-11-06", 298.0, "b"]],
        "coreweave": [["2026-11-05", 1289.0, "c"], ["2026-11-06", 1155.0, "d"]],
    }

    def test_distance_and_change(self):
        ms = {m["key"]: m for m in collect.build_measured(self.SEED, date(2026, 11, 13))}
        o, c = ms["oracle"], ms["coreweave"]
        self.assertEqual(o["chg"], 3.0)
        self.assertEqual(o["to_alert"], 2.0)
        self.assertFalse(o["alert"])
        self.assertEqual(o["age_days"], 7)
        self.assertEqual([b["bp"] for b in o["baselines"]], [198.0, 300.0])     # 정렬
        self.assertTrue(o["baselines"][0]["above"])
        self.assertEqual(c["chg"], -134.0)
        self.assertEqual((c["high"], c["high_date"]), (1289.0, "2026-11-05"))

    def test_alert_when_breached(self):
        seed = copy.deepcopy(self.SEED)
        seed["coreweave"].append(["2026-11-12", 1300.0, "e"])
        c = {m["key"]: m for m in collect.build_measured(seed, date(2026, 11, 13))}["coreweave"]
        self.assertTrue(c["alert"])
        self.assertEqual(c["to_alert"], -11.0)

    def test_signature_ignores_age(self):
        a = collect.measured_signature(collect.build_measured(self.SEED, date(2026, 11, 13)))
        b = collect.measured_signature(collect.build_measured(self.SEED, date(2026, 12, 1)))
        self.assertEqual(a, b)
        seed = copy.deepcopy(self.SEED)
        seed["_baselines"]["oracle"][0]["bp"] = 310
        self.assertNotEqual(a, collect.measured_signature(collect.build_measured(seed, date(2026, 11, 13))))


class TestMessage(unittest.TestCase):
    NOW = datetime(2026, 11, 13, 8, 0, tzinfo=notify.KST)          # 금요일
    SAMPLE = {
        "asof": "2026-11-12",
        "generated_at": "2026-11-13T06:10:00+09:00",
        "macro": {"bbb_oas_bp": 112.0, "percentile_2y": 0.84, "series": []},
        "measured": collect.build_measured(TestMeasured.SEED, date(2026, 11, 13)),
        "measured_updated": "2026-11-10",
        "entities": [
            {"key": "oracle", "name": "Oracle", "type": "traded", "note": "n",
             "basket": {"ORCL": 1.0}, "csi": 86.9, "csi_prev": 82.4, "csi_hist": [],
             "cds": {"last": 298.0, "last_date": "2026-11-06", "source": "x",
                     "ytd_low": 145.0, "ytd_high": 298.0, "series": []}},
            {"key": "nvidia", "name": "Nvidia", "type": "traded", "note": "n",
             "basket": {"NVDA": 1.0}, "csi": 50.0, "csi_prev": 50.2, "csi_hist": [],
             "cds": {"last": 139.0, "last_date": "2026-11-06", "source": "x",
                     "ytd_low": 42.0, "ytd_high": 139.0, "series": []}},
            {"key": "anthropic", "name": "Anthropic", "type": "proxy", "note": "n",
             "basket": {"GOOGL": 0.7, "AMZN": 0.3}, "csi": 41.5, "csi_prev": 46.2,
             "csi_hist": [], "cds": {"last": 80.0, "last_date": "2026-10-28",
                                     "source": "x", "ytd_low": 67.0, "ytd_high": 80.0,
                                     "series": []}},
        ],
        "candidates": [],
    }

    def msg(self, d=None):
        return notify.build_message(d or self.SAMPLE, self.NOW)

    def test_renders_measured_with_alert_distance(self):
        m = self.msg()
        self.assertIn("오라클 <b>298bp</b> 11/06 (+3) · 경보선 300까지 2bp", m)
        self.assertIn("코어위브 <b>1,155bp</b> 11/06 (-134) · 경보선 1,289까지 134bp", m)
        self.assertIn("엔비디아 139bp 11/06", m)
        self.assertIn("알파벳(앤트로픽 대리) 80bp 10/28", m)
        self.assertIn("▲", m)
        self.assertIn("▼", m)

    def test_calm_format_no_decoration(self):
        m = self.msg()
        for deco in ("🛡", "📊", "📰", "🟢", "🟡", "🟠", "⚠"):
            self.assertNotIn(deco, m)
        self.assertNotIn("🔴", m)                        # 경보선 돌파가 없으면 색 표시 없음
        self.assertIn("<b>과열</b>", m)                    # 과열은 굵게만

    def test_red_only_on_breach(self):
        d = copy.deepcopy(self.SAMPLE)
        seed = copy.deepcopy(TestMeasured.SEED)
        seed["coreweave"].append(["2026-11-12", 1300.0, "e"])
        d["measured"] = collect.build_measured(seed, date(2026, 11, 13))
        m = self.msg(d)
        self.assertEqual(m.count("🔴"), 1)
        self.assertIn("경보선 1,289bp 돌파", m)
        self.assertIn("실측 경보: 코어위브", m)

    def test_band_boundaries(self):
        self.assertEqual(notify.band(75.0), (True, "과열"))
        self.assertEqual(notify.band(74.9)[1], "확대")
        self.assertEqual(notify.band(39.9)[1], "안정")

    def test_small_moves(self):
        self.assertEqual(notify.arrow(90.7, 90.2), "▲0.5")
        self.assertEqual(notify.arrow(90.0, 90.2), " ―")
        self.assertEqual(notify.arrow(80.0, 86.0), "▼6")

    def test_stale_warning(self):
        stale = dict(self.SAMPLE, asof="2020-01-01")
        self.assertIn("경과", self.msg(stale))

    def test_missing_cds_is_tolerated(self):
        d = copy.deepcopy(self.SAMPLE)
        d["entities"][1]["cds"]["last"] = None
        d["measured"] = []
        self.assertIn("Oracle", self.msg(d))


class TestHubPayload(unittest.TestCase):
    def test_chart_only_on_change_alert_or_tuesday(self):
        d = copy.deepcopy(TestMessage.SAMPLE)
        fri = datetime(2026, 11, 13, 8, 0, tzinfo=notify.KST)
        tue = datetime(2026, 11, 17, 8, 0, tzinfo=notify.KST)
        self.assertEqual(notify.build_hub_payload(d, fri)["photos"], [])
        self.assertEqual(notify.build_hub_payload(d, tue)["photos"], [notify.HUB_PHOTO_PATH])
        d["measured_updated"] = "2026-11-13"
        p = notify.build_hub_payload(d, fri)
        self.assertEqual(p["photos"], [notify.HUB_PHOTO_PATH])
        self.assertEqual(p["as_of_kst"], "2026-11-13")
        self.assertTrue(p["message"].startswith("<b>AI 크레딧 레이더</b>"))

    def test_write_json_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "hub", "latest.json")
            self.assertTrue(notify.write_json(p, {"generated_at": "1", "a": 1}))
            self.assertFalse(notify.write_json(p, {"generated_at": "2", "a": 1}))
            self.assertTrue(notify.write_json(p, {"generated_at": "2", "a": 2}))


class TestChart(unittest.TestCase):
    def test_renders_png(self):
        import chart
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "c.png")
            self.assertTrue(chart.render(TestMessage.SAMPLE, out, datetime(2026, 11, 13)))
            with open(out, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            self.assertFalse(chart.render(TestMessage.SAMPLE, out, datetime(2026, 11, 13)))   # 같은 그림이면 안 씀


class TestSaveJson(unittest.TestCase):
    def test_idempotent_write(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "sub", "d.json")
            obj = {"generated_at": "1", "a": 1}
            self.assertTrue(collect.save_json(p, obj))
            self.assertFalse(collect.save_json(p, {"generated_at": "2", "a": 1}))
            self.assertTrue(collect.save_json(p, {"generated_at": "2", "a": 2}))


class TestNewsCache(unittest.TestCase):
    def test_reuses_same_day_candidates(self):
        today = datetime.now(collect.KST).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as t:
            orig = collect.OUT_PATH
            try:
                collect.OUT_PATH = os.path.join(t, "data.json")
                collect.save_json(collect.OUT_PATH,
                                  {"candidates_date": today, "candidates": [{"title": "x"}]})
                self.assertEqual(collect.load_cached_candidates(today), [{"title": "x"}])
                self.assertIsNone(collect.load_cached_candidates("1999-01-01"))
            finally:
                collect.OUT_PATH = orig


class TestObservationAlignment(unittest.TestCase):
    def test_future_macro_does_not_rewrite_past_csi(self):
        import pandas as pd
        idx = pd.bdate_range('2024-01-01', periods=220)
        px = pd.Series([100 + i * .1 + (i % 7) for i in range(220)], index=idx)
        macro = pd.Series(.2, index=idx)
        before = collect.ticker_stress(px, macro)
        macro.loc[idx[-1] + pd.Timedelta(days=7)] = .9
        after = collect.ticker_stress(px, macro)
        pd.testing.assert_series_equal(before, after)

    def test_missing_macro_fails_without_overwriting_output(self):
        from unittest.mock import patch
        with patch.object(collect, 'fetch_bbb_oas', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, '기존 산출물 보존'):
                collect.build()

    def test_flat_scores_are_not_all_falling(self):
        d = copy.deepcopy(TestMessage.SAMPLE)
        d['asof'] = notify.datetime.now(notify.KST).date().isoformat()
        for e in d['entities']:
            e['csi_prev'] = e['csi']
        self.assertNotIn('모두 하락', notify.build_message(d))


class TestPriceFallback(unittest.TestCase):
    def test_nasdaq_fills_missing_ticker(self):
        import pandas as pd
        from unittest.mock import patch
        idx = pd.bdate_range("2026-01-01", periods=5)
        yf_df = pd.DataFrame({"ORCL": [1, 2, 3, 4, 5]}, index=idx, dtype=float)
        nas = pd.Series([9, 8, 7, 6, 5], index=idx, dtype=float)
        with patch.object(collect, "fetch_prices_yf", return_value=yf_df), \
             patch.object(collect, "fetch_close_nasdaq", return_value=nas):
            out = collect.fetch_prices(["ORCL", "CRWV"])
        self.assertEqual(list(out.columns), ["ORCL", "CRWV"])
        self.assertEqual(out["CRWV"].iloc[-1], 5.0)

    def test_yfinance_failure_uses_nasdaq_for_all(self):
        import pandas as pd
        from unittest.mock import patch
        idx = pd.bdate_range("2026-01-01", periods=3)
        with patch.object(collect, "fetch_prices_yf", side_effect=RuntimeError("429")), \
             patch.object(collect, "fetch_close_nasdaq", return_value=pd.Series([1.0, 2.0, 3.0], index=idx)):
            out = collect.fetch_prices(["ORCL", "NVDA"])
        self.assertEqual(sorted(out.columns), ["NVDA", "ORCL"])


if __name__ == "__main__":
    unittest.main()
