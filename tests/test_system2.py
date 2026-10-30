import datetime
import json
import os
import tempfile
import unittest

from _load import load

s2 = load("trader-news", "system2")
NOW = datetime.datetime(2026, 10, 29, 14, 0, tzinfo=datetime.timezone.utc)


def fake_post(payload):
    sym = payload["input"][0]["content"].split("$")[1].split(":")[0]
    assert payload["tools"] == [{"type": "x_search", "from_date": "2026-10-28", "to_date": "2026-10-29"}]
    body = {"items": [{"text": "%s pops on earnings" % sym, "url": "https://x.com/a/status/%s" % sym,
                       "author": "@trader", "date": "2026-10-29", "likes": 12}]}
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Here: " + json.dumps(body)}]}],
            "usage": {"total_tokens": 100}}


class XChannel(unittest.TestCase):
    def test_key_from_env_file_without_export(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            open(p, "w").write("export JEV_API_KEY=a\n\nXAI_API_KEY=xai-123\n")
            os.environ.pop("XAI_API_KEY", None)
            self.assertEqual(s2.xai_key(p), "xai-123")

    def test_cap_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            wl = os.path.join(d, "wl.json")
            json.dump({"hits": [{"symbol": "MU"}, {"symbol": "SPY"}, {"symbol": "ORCL"}, {"symbol": "BA"}]}, open(wl, "w"))
            cfg = {"max_searches": 3, "watchlist": wl, "symbols": ["SPY"]}
            self.assertEqual(s2.x_symbols(cfg, 3), ["SPY", "MU", "ORCL"])
            items, usage, errors = s2.collect_x(cfg, NOW, post=fake_post, key="k")
            self.assertEqual(usage, {"searches": 3, "tokens": 300})
            self.assertEqual([i["symbol"] for i in items], ["SPY", "MU", "ORCL"])
            self.assertEqual(items[0]["author"], "trader")
            self.assertEqual(errors, [])

    def test_run_drops_seen_posts_and_no_key_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            wl = os.path.join(d, "wl.json")
            json.dump({"hits": [{"symbol": "MU"}]}, open(wl, "w"))
            src = {"x": {"max_searches": 1, "watchlist": wl}}
            cwd = os.getcwd()
            os.chdir(d)  # no trader/.env here, so "no key" really means no key
            self.addCleanup(os.chdir, cwd)
            os.environ["XAI_API_KEY"] = "k"
            doc, state = s2.run(src, {}, now=NOW, post=fake_post)
            self.assertEqual(len(doc["x"]), 1)
            self.assertIn("x", doc["channels"])
            doc2, _ = s2.run(src, state, now=NOW, post=fake_post)
            self.assertEqual(doc2["x"], [])
            os.environ.pop("XAI_API_KEY")
            doc3, _ = s2.run(src, {}, now=NOW, post=fake_post)
            self.assertEqual(doc3["x"], [])
            self.assertEqual(doc3["errors"][0]["error"], "XAI_API_KEY not set")

    def test_market_hours_is_new_york_time(self):
        utc = datetime.timezone.utc
        self.assertTrue(s2.market_hours(datetime.datetime(2026, 10, 29, 14, 0, tzinfo=utc)))   # 10:00 EDT Thu
        self.assertFalse(s2.market_hours(datetime.datetime(2026, 10, 29, 21, 0, tzinfo=utc)))  # 17:00 EDT
        self.assertFalse(s2.market_hours(datetime.datetime(2026, 10, 31, 14, 0, tzinfo=utc)))  # Saturday
        self.assertTrue(s2.market_hours(datetime.datetime(2026, 11, 3, 15, 0, tzinfo=utc)))   # 10:00 EST after DST


if __name__ == "__main__":
    unittest.main()
