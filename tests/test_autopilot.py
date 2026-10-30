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
    def setUp(self):
        self.cwd = os.getcwd()   # morning() and watch() chdir; put the suite back where it was

    def tearDown(self):
        os.chdir(self.cwd)

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


PICKS = [{"symbol": s, "quality": q, "setups": ["orb"]} for s, q in (("A", 3), ("B", 2), ("C", 1))]
T1000 = datetime.datetime(2026, 10, 30, 10, 0)
T1531 = datetime.datetime(2026, 10, 30, 15, 31)


class Watch(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)

    def test_watch_returns_first_trigger_by_rank_and_lists_others(self):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            sym = argv[3]
            st = "triggered" if sym in ("B", "C") else "waiting"
            return ok(json.dumps({"symbol": sym, "status": st, "price": 10.0}))
        r = ap.watch(".", PICKS, until="15:30", every=0, runner=fake, now_fn=lambda: T1000)
        self.assertEqual(r["symbol"], "B")
        self.assertEqual(r["also_triggered"], ["C"])
        self.assertTrue(any("--write" in c for c in calls if c[3] == "B"))

    def test_watch_stops_at_until(self):
        def must_not_call(argv, **kw):
            raise AssertionError("polled after the cutoff")
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=must_not_call, now_fn=lambda: T1531))

    def test_watch_keeps_polling_while_waiting(self):
        clock = iter([T1000, T1000, T1531])

        def fake(argv, **kw):
            return ok(json.dumps({"symbol": argv[3], "status": "waiting"}))
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=fake, now_fn=lambda: next(clock)))


if __name__ == "__main__":
    unittest.main()
