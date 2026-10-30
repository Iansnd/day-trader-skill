import datetime
import json
import os
import tempfile
import types
import unittest

from _load import load

ap = load("trader-autopilot", "autopilot")
tws = load("trader-broker", "ibkr_tws")
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

    def test_regime_exit_1_is_a_verdict_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader", "state"))

            def fake(argv, **kw):
                if "regime.py" in argv[1]:
                    return types.SimpleNamespace(returncode=1, stdout="market_regime = RISK_OFF\n", stderr="")
                return ok("{}")
            self.assertTrue(ap.morning(d, runner=fake)["steps"][0]["ok"])

    def test_morning_clears_yesterdays_card(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader", "state"))
            card = os.path.join(d, "trader", "decision.json")
            open(card, "w").write("{}")
            ap.morning(d, runner=lambda argv, **kw: ok("{}"))
            self.assertFalse(os.path.exists(card))

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

    def test_already_ran_today(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader", "state"))
            os.chdir(d)
            today = ap.now_et().strftime("%Y-%m-%d")
            self.assertFalse(ap.already_ran_today())
            with open("trader/state/autopilot.json", "w") as fh:
                json.dump({"date": today, "steps": [], "picks": []}, fh)
            self.assertFalse(ap.already_ran_today())   # a manual `morning` must not eat the scheduled day
            with open("trader/state/autopilot.json", "w") as fh:
                json.dump({"date": today, "day_started": True}, fh)
            self.assertTrue(ap.already_ran_today())
            with open("trader/state/autopilot.json", "w") as fh:
                json.dump({"date": "2020-01-01"}, fh)
            self.assertFalse(ap.already_ran_today())

    def test_skips_non_trading_day(self):
        self.assertFalse(ap.is_trading_day(datetime.datetime(2026, 11, 1, 14, 0, tzinfo=UTC)))  # Sunday
        self.assertTrue(ap.is_trading_day(datetime.datetime(2026, 10, 30, 14, 0, tzinfo=UTC)))  # Friday
        self.assertFalse(ap.is_trading_day(datetime.datetime(2026, 11, 26, 14, 0, tzinfo=UTC)))  # Thanksgiving

    def test_entry_cutoff_pulls_in_on_early_close(self):
        cfg = {"no_entries_after": "15:30"}
        self.assertEqual(ap.entry_cutoff(cfg, datetime.datetime(2026, 10, 30, 14, 0, tzinfo=UTC)), "15:30")
        self.assertEqual(ap.entry_cutoff(cfg, datetime.datetime(2026, 11, 27, 14, 0, tzinfo=UTC)), "12:30")  # 1 p.m. close

    def test_failed_scan_leaves_no_picks(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader", "state"))
            with open(os.path.join(d, "trader", "watchlist.json"), "w") as fh:
                json.dump({"hits": [{"symbol": "OLD", "jev": {"quality": 2}}]}, fh)   # yesterday's list

            def fake(argv, **kw):
                if "scan.py" in argv[1]:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="yahoo down")
                return ok("{}")
            state = ap.morning(d, runner=fake)
            self.assertEqual(state["picks"], [])
            self.assertFalse(os.path.exists(os.path.join(d, "trader", "state", "autopilot.json.tmp")))


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

    def test_watch_ignores_a_trigger_older_than_three_bars(self):
        def fake(argv, **kw):
            return ok(json.dumps({"symbol": argv[3], "status": "triggered", "minutes_since": 240}))
        clock = iter([T1000, T1531])
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=fake, now_fn=lambda: next(clock)))

    def test_watch_keeps_polling_when_the_write_recheck_fails(self):
        clock = iter([T1000, T1000, T1531])

        def fake(argv, **kw):
            if "--write" in argv:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="chart stale")
            return ok(json.dumps({"symbol": argv[3], "status": "triggered", "minutes_since": 1}))
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=fake, now_fn=lambda: next(clock)))

    def test_watch_stops_at_until(self):
        def must_not_call(argv, **kw):
            raise AssertionError("polled after the cutoff")
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=must_not_call, now_fn=lambda: T1531))

    def test_watch_keeps_polling_while_waiting(self):
        clock = iter([T1000, T1000, T1531])

        def fake(argv, **kw):
            return ok(json.dumps({"symbol": argv[3], "status": "waiting"}))
        self.assertIsNone(ap.watch(".", PICKS, until="15:30", every=0, runner=fake, now_fn=lambda: next(clock)))


def chain_runner(check_json, orders=None):
    """Fake runner for the ticket chain; records argv and plays back check.py's JSON."""
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        name = os.path.basename(argv[1])
        if name == "check.py":
            return ok(json.dumps(check_json))
        if name == "ticket.py":
            rc = 1 if check_json.get("block") else 0   # ticket.py show exits 1 when no ticket is READY
            return types.SimpleNamespace(returncode=rc, stdout="#1 buy 8 TSLA @ market\nJev: act\n", stderr="")
        if name == "size.py" and orders is not None:
            with open("trader/orders.json", "w") as fh:
                json.dump({"orders": orders}, fh)
        return ok("")
    fake.calls = calls
    return fake


class Decide(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "trader", "state"))
        self.portfolio(hours_ago=1)

    def tearDown(self):
        os.chdir(self.cwd)

    def portfolio(self, hours_ago):
        t = datetime.datetime.now(UTC) - datetime.timedelta(hours=hours_ago)
        with open(os.path.join(self.d, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 1e6, "synced_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)

    def card(self):
        with open(os.path.join(self.d, "trader", "decision.json")) as fh:
            return json.load(fh)

    TRIG = {"symbol": "TSLA", "status": "triggered", "price": 364.2, "also_triggered": ["NVDA"]}

    def test_decide_ready_card_has_reply_hint(self):
        fake = chain_runner({"pass": 1, "block": 0, "results": [{"verdict": "PASS"}]},
                            orders=[{"symbol": "TSLA", "side": "buy", "qty": 8}])
        card = ap.decide(self.d, self.TRIG, runner=fake, notifier=lambda *a: None)
        self.assertEqual(card["status"], "ready")
        self.assertEqual(card["reply_hint"], "yes buy 8 TSLA")
        self.assertEqual(card["also_triggered"], ["NVDA"])
        self.assertIn("Jev: act", card["ticket_text"])
        self.assertEqual([os.path.basename(c[1]) for c in fake.calls],
                         ["exit.py", "timemachine.py", "size.py", "check.py", "ticket.py"])
        self.assertEqual(self.card()["symbol"], "TSLA")

    def test_decide_marks_blocked_when_check_blocks(self):
        fake = chain_runner({"pass": 0, "block": 1, "results": [{"verdict": "BLOCK", "reasons": ["over max loss"]}]})
        card = ap.decide(self.d, self.TRIG, runner=fake, notifier=lambda *a: None)
        self.assertEqual(card["status"], "blocked")

    def test_decide_marks_stale_account(self):
        self.portfolio(hours_ago=30)
        fake = chain_runner({"pass": 1, "block": 0, "results": [{"verdict": "PASS"}]})
        card = ap.decide(self.d, self.TRIG, runner=fake, notifier=lambda *a: None)
        self.assertEqual(card["status"], "stale_account")
        self.assertGreater(card["portfolio_age_hours"], 24)

    def test_decide_marks_error_when_a_step_fails(self):
        def fake(argv, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no plan")
        card = ap.decide(self.d, self.TRIG, runner=fake, notifier=lambda *a: None)
        self.assertEqual(card["status"], "error")

    def test_notify_uses_osascript(self):
        seen = []
        ap.notify("Autopilot", "TSLA ready", runner=lambda argv, **kw: seen.append(argv) or ok())
        self.assertEqual(seen[0][0], "osascript")
        self.assertIn("TSLA ready", seen[0][2])


from fake_ib import FakeIB   # noqa: E402

ICE_ORDER = {"symbol": "ICE", "side": "buy", "qty": "16", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "stop_loss_stop_price": "166.51", "plan": {"stop": 166.51, "target_r": 2.0}}


class Trade(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "trader", "state"))
        with open(os.path.join(self.d, "trader", "orders.json"), "w") as fh:
            json.dump({"orders": [ICE_ORDER]}, fh)
        with open(os.path.join(self.d, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 10000.0, "positions": []}, fh)
        self.notes = []
        self.cfg = {"paper": True, "auto_place": True, "flat_by": "15:55"}
        self.card = {"status": "ready", "symbol": "ICE", "reply_hint": "yes buy 16 ICE"}

    def tearDown(self):
        os.chdir(self.cwd)

    def trade(self, ib, cfg=None, now="10:40"):
        return ap.trade(self.d, dict(self.card), cfg or self.cfg, connect=lambda c: ib,
                        now_fn=lambda: now, notifier=lambda t, b: self.notes.append((t, b)))

    def read(self, rel):
        with open(os.path.join(self.d, rel)) as fh:
            return json.load(fh)

    def test_trade_skipped_without_auto_place(self):
        self.assertEqual(self.trade(FakeIB(), {"paper": True})["status"], "ready")
        self.assertEqual(self.trade(FakeIB(), {"paper": False, "auto_place": True})["status"], "ready")
        self.assertEqual(ap.trade(self.d, {"status": "blocked", "symbol": "ICE"}, self.cfg, connect=lambda c: FakeIB())["status"], "blocked")

    def test_trade_places_books_and_holds(self):
        ib = FakeIB(); ib.script += [("fill", 16, 168.79), ("fill_sell", "stop", 16, 166.40)]
        card = self.trade(ib)
        self.assertEqual((card["status"], card["outcome"]), ("closed", "stopped"))
        self.assertEqual(card["broker"]["bracket"]["target"], 173.35)
        self.assertEqual(self.read("trader/portfolio.json")["positions"], [])
        log = self.read("trader/state/broker.json")["orders"]
        self.assertEqual([(o["status"], o["side"]) for o in log], [("placed", "buy"), ("filled", "buy"), ("filled", "sell")])
        self.assertIn("stop 166.51, target 173.35", self.notes[0][1])
        self.assertEqual(self.read("trader/decision.json")["status"], "closed")

    def test_bracket_failure_flags_unprotected(self):
        ib = FakeIB(fail_on_sell=True); ib.script.append(("fill", 16, 168.79))
        card = self.trade(ib)
        self.assertEqual(card["status"], "broker_error")
        self.assertTrue(card["unprotected"])
        self.assertIn("NO stop", self.notes[-1][1])
        self.assertEqual(self.read("trader/portfolio.json")["positions"][0]["shares"], 16)

    def test_connect_failure_is_broker_error_not_unprotected(self):
        def bad(c):
            raise tws.TwsError("gateway down")
        card = ap.trade(self.d, dict(self.card), self.cfg, connect=bad, now_fn=lambda: "10:40", notifier=lambda *a: None)
        self.assertEqual(card["status"], "broker_error")
        self.assertFalse(card.get("unprotected"))


class Dashboard(unittest.TestCase):
    def test_dashboard_snapshot_includes_decision(self):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location("dashboard", os.path.join(root, "dashboard.py"))
        dash = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dash)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "decision.json"), "w") as fh:
                json.dump({"symbol": "TSLA", "status": "ready"}, fh)
            self.assertEqual(dash.snapshot(d)["decision"]["symbol"], "TSLA")
        self.assertIn("decision", dash.PAGE)


if __name__ == "__main__":
    unittest.main()
