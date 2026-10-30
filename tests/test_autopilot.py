import datetime
import json
import os
import tempfile
import types
import unittest

from _load import load

ap = load("trader-autopilot", "autopilot")
UTC = datetime.timezone.utc


def ok(out=""):
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class Morning(unittest.TestCase):
    def test_rank_picks_sorts_by_quality(self):
        wl = {"hits": [{"symbol": "A", "jev": {"quality": 1.0}, "setups": ["orb"]},
                       {"symbol": "B", "jev": {"quality": 3.0}, "setups": ["orb"]},
                       {"symbol": "C", "setups": ["orb"]}]}
        self.assertEqual([p["symbol"] for p in ap.rank_picks(wl, 5)], ["B", "A", "C"])
        self.assertEqual(len(ap.rank_picks(wl, 1)), 1)

    def test_run_step_records_failure(self):
        def fake(argv, **kw):
            return types.SimpleNamespace(returncode=2, stdout="boom\n", stderr="")
        r = ap.run_step("x", ["python3", "nope.py"], runner=fake)
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 2)
        self.assertIn("boom", r["tail"])

    def test_morning_writes_state_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader", "state"))
            calls = []

            def fake(argv, **kw):
                calls.append(argv)
                if "regime.py" in argv[1]:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="yahoo down")
                if "scan.py" in argv[1]:
                    with open(os.path.join(d, "trader", "watchlist.json"), "w") as fh:
                        json.dump({"hits": [{"symbol": "TSLA", "jev": {"quality": 2}, "setups": ["orb"]}]}, fh)
                return ok("{}")
            state = ap.morning(d, runner=fake)
            self.assertEqual(len(state["steps"]), 3)
            self.assertFalse(state["steps"][0]["ok"])
            self.assertEqual(state["picks"][0]["symbol"], "TSLA")
            with open(os.path.join(d, "trader", "state", "autopilot.json")) as fh:
                self.assertEqual(json.load(fh)["picks"], state["picks"])

    def test_skips_non_trading_day(self):
        self.assertFalse(ap.is_trading_day(datetime.datetime(2026, 11, 1, 14, 0, tzinfo=UTC)))  # Sunday
        self.assertTrue(ap.is_trading_day(datetime.datetime(2026, 10, 30, 14, 0, tzinfo=UTC)))  # Friday


if __name__ == "__main__":
    unittest.main()
