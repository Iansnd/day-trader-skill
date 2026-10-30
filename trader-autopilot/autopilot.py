#!/usr/bin/env python3
"""Autopilot (trader-autopilot): runs the morning steps, watches the top picks,
and builds a decision card when one triggers. Standard library only.

    python3 day-trader-skill/trader-autopilot/autopilot.py day       # morning, then watch
    python3 day-trader-skill/trader-autopilot/autopilot.py morning   # regime, daily scan, news -> picks
    python3 day-trader-skill/trader-autopilot/autopilot.py watch     # poll the picks until a trigger

By default it never places an order: the chain stops at check.py + ticket.py show,
and the card it writes (trader/decision.json) is what you look at before saying yes.
With "auto_place": true and "paper": true in trader/config.json it goes on to place
the entry at IBKR through the TWS API (ibkr_tws.py), attaches the OCA stop + target
from the real fill, and holds until one fills or flat_by, then sells at market.
Launchd cannot enter the external volume, so the script does the chdir itself.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

ROOT = "/Volumes/Extreme SSD/Day trader Setup"
SKILLS = "day-trader-skill"
PY = sys.executable or "python3"


def _tool(folder, name):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", folder))
    return __import__(name)


def _news():
    return _tool("trader-news", "news")


def _alerts():
    return _tool("trader-alerts", "alerts")   # owns the NYSE holiday and early-close calendar


def now_et(now=None):
    return _news().to_eastern(now or datetime.datetime.now(datetime.timezone.utc))


def is_trading_day(now=None):
    return _alerts().trading_day(now_et(now).date())[0]


def entry_cutoff(cfg, now=None):
    """config no_entries_after, pulled in to 30 minutes before a 1:00 p.m. early close."""
    want = cfg.get("no_entries_after") or "15:30"
    s = _alerts().session(now_et(now).date())
    if not s:
        return want
    close = datetime.datetime.combine(datetime.date.today(), s[1]) - datetime.timedelta(minutes=30)
    return min(want, close.strftime("%H:%M"))


def already_ran_today(now=None):
    # ponytail: the plist fires at 15:00 and 16:00 local (9:00 ET across the DST gap) and launchd
    # refires missed jobs on wake; today's state file means the morning already happened.
    st = load_json("trader/state/autopilot.json", {}) or {}
    return bool(st.get("day_started")) and st.get("date") == now_et(now).strftime("%Y-%m-%d")


def script(folder, name):
    return os.path.join(SKILLS, folder, name)


VERDICT_RC = (0, 1)   # regime.py, check.py and ticket.py exit 1 to say RISK_OFF / BLOCK / not READY


def run_step(name, argv, timeout=900, runner=subprocess.run, ok_rcs=(0,)):
    t0 = time.time()
    try:
        r = runner(argv, capture_output=True, text=True, timeout=timeout)
        rc, out, stdout = r.returncode, (r.stdout or "") + (r.stderr or ""), r.stdout or ""
    except subprocess.TimeoutExpired:
        rc, out, stdout = -1, "timed out after %ss" % timeout, ""
    except OSError as e:
        rc, out, stdout = -2, str(e), ""
    tail = "\n".join(out.strip().splitlines()[-20:])
    return {"name": name, "ok": rc in ok_rcs and bool(stdout.strip() or rc == 0), "rc": rc, "seconds": round(time.time() - t0, 1), "tail": tail,
            "stdout": stdout}  # stdout: whole thing, for callers that parse JSON; tail is for the log


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)   # the dashboard polls this file; never let it see a half-written one


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def rank_picks(watchlist, n=5):
    hits = (watchlist or {}).get("hits") or []
    rows = [{"symbol": h["symbol"], "quality": (h.get("jev") or {}).get("quality"), "setups": h.get("setups", [])}
            for h in hits if h.get("symbol")]
    rows.sort(key=lambda r: -(r["quality"] if r["quality"] is not None else -1e9))
    return rows[:n]


def morning(root, runner=subprocess.run, now=None, day_started=False):
    os.chdir(root)
    if os.path.exists("trader/decision.json"):
        os.remove("trader/decision.json")   # yesterday's card is not today's instruction
    steps = [
        run_step("regime", [PY, script("trader-scanner", "regime.py")], runner=runner, ok_rcs=VERDICT_RC),
        run_step("scan", [PY, script("trader-scanner", "scan.py"), "daily", "--quiet"], timeout=3600, runner=runner),
        run_step("news", [PY, script("trader-news", "news.py"), "--json", "--hours", "24"], runner=runner),
    ]
    picks = rank_picks(load_json("trader/watchlist.json")) if steps[1]["ok"] else []   # a failed scan leaves yesterday's list
    state = {"date": now_et(now).strftime("%Y-%m-%d"), "day_started": day_started, "steps": steps, "picks": picks}
    os.makedirs("trader/state", exist_ok=True)
    write_json("trader/state/autopilot.json", state)
    return state


def _hhmm(text):
    h, m = (text or "15:30").split(":")
    return int(h), int(m)


def check_entry(symbol, write=False, runner=subprocess.run):
    """entry.py check SYMBOL --setup orb [--write] -> its JSON, or None when it failed."""
    argv = [PY, script("trader-entry", "entry.py"), "check", symbol, "--setup", "orb", "--json"]
    if write:
        argv.append("--write")
    r = run_step("entry:" + symbol, argv, timeout=120, runner=runner)
    try:
        return json.loads(r["stdout"]) if r["stdout"].lstrip().startswith("{") else None
    except ValueError:
        return None


MAX_TRIGGER_AGE_MIN = 15   # three 5-minute bars; entry.py reports a breakout all day, we only want a fresh one


def fresh_trigger(res):
    return bool(res) and res.get("status") == "triggered" and res.get("minutes_since", 0) <= MAX_TRIGGER_AGE_MIN


def watch(root, picks, until="15:30", every=60, runner=subprocess.run, now_fn=now_et):
    """Poll every pick's ORB setup; the first fresh trigger by rank wins. None at the cutoff."""
    os.chdir(root)
    cutoff = _hhmm(until)
    while True:
        t = now_fn()
        if (t.hour, t.minute) >= cutoff:
            return None
        fired = [p["symbol"] for p in picks if fresh_trigger(check_entry(p["symbol"], runner=runner))]
        if fired:
            res = check_entry(fired[0], write=True, runner=runner)
            if res and res.get("symbol") == fired[0]:
                res["also_triggered"] = fired[1:]
                return res
            # the --write re-check failed, so trader/entry.json is not this symbol; exit.py would plan the wrong one
        if every:
            time.sleep(every)


def notify(title, body, runner=subprocess.run):
    q = lambda t: str(t).replace("\\", "").replace('"', "'")
    try:
        runner(["osascript", "-e", 'display notification "%s" with title "%s"' % (q(body), q(title))],
               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass  # a missing osascript (not macOS) is not a reason to lose the card


def portfolio_age_hours(path="trader/portfolio.json", now=None):
    stamp = (load_json(path, {}) or {}).get("synced_at") or ""
    try:
        t = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return round(((now or datetime.datetime.now(datetime.timezone.utc)) - t).total_seconds() / 3600, 1)


def decide(root, trigger, runner=subprocess.run, now=None, notifier=notify):
    """Trigger -> the whole pre-order chain -> trader/decision.json. Never orders."""
    os.chdir(root)
    sym = trigger["symbol"]
    chain = [
        ("exit", [PY, script("trader-exit", "exit.py"), "plan", "--stop", "or-mid", "--target", "2R", "--write"]),
        ("timemachine", [PY, script("trader-timemachine", "timemachine.py"), "run", "--write"]),
        ("size", [PY, script("trader-budget", "size.py"), "--write"]),
        ("check", [PY, script("trader-risk", "check.py"), "--json"]),
        ("ticket", [PY, script("trader-broker", "ticket.py"), "show"]),
    ]
    steps, risk, ticket_text = [], None, ""
    for name, argv in chain:
        r = run_step(name, argv, timeout=600, runner=runner, ok_rcs=VERDICT_RC if name in ("check", "ticket") else (0,))
        steps.append(r)
        if name == "check":
            try:
                risk = json.loads(r["stdout"]) if r["stdout"].lstrip().startswith("{") else None
            except ValueError:
                risk = None
            r["ok"] = risk is not None
        elif name == "ticket":
            ticket_text = r["stdout"]
        if not r["ok"]:
            break
    age = portfolio_age_hours(now=now)
    orders = (load_json("trader/orders.json", {}) or {}).get("orders") or []
    first = orders[0] if orders else {}
    if not all(s["ok"] for s in steps) or len(steps) < len(chain):
        status = "error"
    elif risk.get("block"):
        status = "blocked"
    elif age is None or age > 24:
        status = "stale_account"
    else:
        status = "ready"
    card = {"created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbol": sym, "status": status, "trigger": trigger, "steps": steps, "risk": risk,
            "ticket_text": ticket_text, "also_triggered": trigger.get("also_triggered", []),
            "portfolio_age_hours": age,
            "reply_hint": "yes %s %s %s" % (first.get("side", "buy"), first.get("qty", "?"), sym)}
    write_json("trader/decision.json", card)
    notifier("Autopilot: %s %s" % (sym, status), {"ready": "Ticket ready. Open the dashboard, then say: " + card["reply_hint"],
                                               "blocked": "Risk check blocked it.",
                                               "stale_account": "Sync the IBKR account in Claude first.",
                                               "error": "A step failed; see the card."}[status])
    return card


def _ticket():
    return _tool("trader-broker", "ticket")


def _tws():
    return _tool("trader-broker", "ibkr_tws")


def trade(root, card, cfg, connect=None, now_fn=now_et, notifier=notify):
    """A ready card -> entry + OCA bracket at IBKR -> hold to the exit -> fills booked. Paper only, opt in."""
    if card.get("status") != "ready" or not (cfg.get("auto_place") and cfg.get("paper") is True):
        return card
    os.chdir(root)
    tws, ticket = _tws(), _ticket()
    sym = card["symbol"]
    order = ((load_json("trader/orders.json", {}) or {}).get("orders") or [{}])[0]
    t = now_fn()
    today = t.date().isoformat() if hasattr(t, "date") else datetime.date.today().isoformat()
    state_path, pf_path = "trader/state/broker.json", "trader/portfolio.json"

    def fail(msg, unprotected=False):
        card.update({"status": "broker_error", "error": str(msg), "unprotected": unprotected})
        write_json("trader/decision.json", card)
        notifier("Autopilot: %s broker error" % sym,
                 ("%s is open with NO stop: %s" % (sym, msg)) if unprotected else str(msg))
        return card

    def book(side, qty, price):
        now = datetime.datetime.now(datetime.timezone.utc)
        ticket.apply_fill(pf_path, cfg, sym, side, qty, price, today)
        ticket.log_fill(state_path, now, today, sym, side, qty, price)

    try:
        ib = (connect or tws.connect)(cfg)
    except Exception as e:
        return fail(e)
    try:
        entry = tws.place_entry(ib, order)
    except Exception as e:
        return fail(e)
    st = ticket.load_state(state_path)
    st.setdefault("orders", []).append({"at": ticket.ny_iso(datetime.datetime.now(datetime.timezone.utc)), "date": today,
                                        "status": "placed", "symbol": sym, "side": order.get("side", "buy"),
                                        "qty": str(order.get("qty")), "broker_order_id": entry["order_id"],
                                        "plain": "BUY %s %s, market, day (autopilot via TWS)" % (order.get("qty"), sym)})
    write_json(state_path, st)
    book("buy", entry["filled_qty"], entry["fill_price"])
    try:
        bracket = tws.attach_bracket(ib, order, entry["filled_qty"], entry["fill_price"], date=today,
                                     entry_id=entry["order_id"])
    except Exception as e:
        return fail(e, unprotected=True)
    card.update({"status": "placed", "broker": {"entry": entry, "bracket": bracket}})
    write_json("trader/decision.json", card)
    notifier("Autopilot: %s placed" % sym, "%s %s at %.2f, stop %.2f, target %.2f" % (
        sym, _str(entry["filled_qty"]), entry["fill_price"], bracket["stop"], bracket["target"]))
    try:
        r = tws.hold(ib, bracket, cfg.get("flat_by") or "15:55", now_fn, on_fill=book)
    except Exception as e:
        return fail(e, unprotected=True)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    card.update({"status": "closed", "outcome": r["outcome"], "exit": r})
    write_json("trader/decision.json", card)
    notifier("Autopilot: %s %s" % (sym, r["outcome"]), "%s sold %s at %.2f" % (sym, _str(r["qty"]), r["price"]))
    return card


def _str(q):
    return ("%g" % float(q))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autopilot: morning steps, watch picks, decision card; places on paper only with auto_place.")
    ap.add_argument("cmd", choices=["day", "morning", "watch"])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--every", type=float, default=60)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)   # launchd log, readable while it runs
    os.chdir(a.root)
    if not is_trading_day():
        print("autopilot: not a trading day, nothing to do.")
        return 0
    if a.cmd == "day":
        if already_ran_today():
            print("autopilot: already ran today, nothing to do.")
            return 0
        if (now_et().hour, now_et().minute) < (9, 0):
            print("autopilot: before 9:00 New York (the 15:00 SAST fire in winter), the next fire does it.")
            return 0
    state = morning(a.root, day_started=a.cmd == "day") if a.cmd in ("day", "morning") else load_json("trader/state/autopilot.json", {})
    for s in state.get("steps", []):
        print("%-7s %s %ss" % (s["name"], "ok" if s["ok"] else "FAIL rc=%s" % s["rc"], s["seconds"]))
    print("picks: " + ", ".join(p["symbol"] for p in state.get("picks", [])))
    if a.cmd == "morning":
        return 0
    cfg = load_json("trader/config.json", {})
    trig = watch(a.root, state.get("picks", []), until=entry_cutoff(cfg), every=a.every)
    print("trigger: %s" % (trig["symbol"] if trig else "none"))
    if trig:
        card = decide(a.root, trig)
        print("decision: %s %s -> trader/decision.json" % (card["symbol"], card["status"]))
        card = trade(a.root, card, cfg)
        print("trade: %s %s%s" % (card["symbol"], card["status"], " " + card["outcome"] if card.get("outcome") else ""))
        if card["status"] == "broker_error":
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
