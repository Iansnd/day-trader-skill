"""trader-scanner/regime.py and the market_regime gate in trader-entry/entry.py."""

import argparse
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr

from _load import load

regime = load("trader-scanner", "regime")
entry = load("trader-entry", "entry")


def bars(closes, start="2026-09-01"):
    d0 = datetime.date.fromisoformat(start)
    return [{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "close": c}
            for i, c in enumerate(closes)]


UP = bars([100 + i for i in range(25)])        # rising: last > sma5 > sma20
DOWN = bars([125 - i for i in range(25)])      # falling


class Evaluate(unittest.TestCase):
    def test_risk_on(self):
        res = regime.evaluate({"SPY": UP, "QQQ": UP, "VIX": bars([15.0])})
        self.assertEqual(res["market_regime"], "RISK_ON")
        self.assertEqual(res["failed"], [])

    def test_one_index_below_ma_is_risk_off(self):
        res = regime.evaluate({"SPY": UP, "QQQ": DOWN, "VIX": bars([15.0])})
        self.assertEqual(res["market_regime"], "RISK_OFF")
        self.assertEqual(res["failed"], ["QQQ above 5d and 20d SMA"])

    def test_vix_at_cap_is_risk_off(self):
        res = regime.evaluate({"SPY": UP, "QQQ": UP, "VIX": bars([22.0])})
        self.assertEqual(res["failed"], ["VIX below 22"])

    def test_short_history_fails_closed(self):
        res = regime.evaluate({"SPY": UP[-10:], "QQQ": UP, "VIX": bars([15.0])})
        self.assertEqual(res["market_regime"], "RISK_OFF")


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.a = argparse.Namespace(
            bars=os.path.join(self.d, "macro_bars.json"), vix_max=22.0,
            config=os.path.join(self.d, "config.json"),
            out=os.path.join(self.d, "regime.json"),
            events=os.path.join(self.d, "events.jsonl"), dry_run=False)
        self.now = datetime.datetime(2026, 9, 26, 13, 0, tzinfo=datetime.timezone.utc)

    def test_ibkr_bars_write_config_and_keep_other_keys(self):
        with open(self.a.config, "w") as fh:
            json.dump({"jev": {"enabled": True}}, fh)
        with open(self.a.bars, "w") as fh:
            json.dump({"SPY": UP, "QQQ": UP, "VIX": bars([15.0] * 25)}, fh)
        res = regime.run(self.a, opener=lambda *x, **k: self.fail("Yahoo called"), now=self.now)
        self.assertEqual(res["source"], "ibkr:" + self.a.bars)
        cfg = json.load(open(self.a.config))
        self.assertEqual(cfg["market_regime"], "RISK_ON")
        self.assertEqual(cfg["jev"], {"enabled": True})
        self.assertEqual(json.loads(open(self.a.events).read())["regime"], "RISK_ON")

    def test_missing_or_empty_bars_fall_back_to_yahoo(self):
        open(self.a.bars, "w").close()  # empty file
        calls = []

        def opener(req, timeout=0):
            calls.append(req.full_url)
            ts = [int(datetime.datetime(2026, 9, 1 + i, 20, tzinfo=datetime.timezone.utc).timestamp())
                  for i in range(25)]
            closes = [30.0] * 25 if "VIX" in req.full_url else [100 + i for i in range(25)]
            body = json.dumps({"chart": {"result": [{"timestamp": ts,
                               "indicators": {"quote": [{"close": closes}]}}]}})
            return io.BytesIO(body.encode())

        res = regime.run(self.a, opener=opener, now=self.now)
        self.assertEqual(len(calls), 3)
        self.assertEqual(res["source"], "yahoo")
        self.assertEqual(res["detail"]["SPY"]["date"], "2026-09-25")
        self.assertEqual(json.load(open(self.a.config))["market_regime"], "RISK_OFF")


class EntryGate(unittest.TestCase):
    def test_risk_off_skips_jev_call(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"jev": {"enabled": True}, "market_regime": "RISK_OFF"}, fh)
        os.environ["JEV_API_KEY"] = "x"
        self.addCleanup(os.environ.pop, "JEV_API_KEY", None)
        res = [{"symbol": "AAPL"}]
        err = io.StringIO()
        with redirect_stderr(err):
            entry.jev_attach(res, cfg, opener=lambda *x, **k: self.fail("Jev called"))
        self.assertIn(entry.HALT, err.getvalue())
        self.assertEqual(res[0]["jev"]["halted"], entry.HALT)


if __name__ == "__main__":
    unittest.main()
