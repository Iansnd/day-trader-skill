#!/usr/bin/env python3
"""Autopilot (trader-autopilot): runs the morning steps, watches the top picks,
and builds a decision card when one triggers. Standard library only.

    python3 day-trader-skill/trader-autopilot/autopilot.py day       # morning, then watch
    python3 day-trader-skill/trader-autopilot/autopilot.py morning   # regime, daily scan, news -> picks
    python3 day-trader-skill/trader-autopilot/autopilot.py watch     # poll the picks until a trigger

It never places an order. The chain stops at check.py + ticket.py show, and the
card it writes (trader/decision.json) is what you look at before saying yes.
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


def _news():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-news"))
    import news
    return news


def now_et(now=None):
    return _news().to_eastern(now or datetime.datetime.now(datetime.timezone.utc))


def is_trading_day(now=None):
    # ponytail: weekdays only; NYSE holidays make an idle run, not a bad one.
    return now_et(now).weekday() < 5


def script(folder, name):
    return os.path.join(SKILLS, folder, name)


def run_step(name, argv, timeout=900, runner=subprocess.run):
    t0 = time.time()
    try:
        r = runner(argv, capture_output=True, text=True, timeout=timeout)
        rc, out, stdout = r.returncode, (r.stdout or "") + (r.stderr or ""), r.stdout or ""
    except subprocess.TimeoutExpired:
        rc, out, stdout = -1, "timed out after %ss" % timeout, ""
    except OSError as e:
        rc, out, stdout = -2, str(e), ""
    tail = "\n".join(out.strip().splitlines()[-20:])
    return {"name": name, "ok": rc == 0, "rc": rc, "seconds": round(time.time() - t0, 1), "tail": tail,
            "stdout": stdout}  # stdout: whole thing, for callers that parse JSON; tail is for the log


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


def morning(root, runner=subprocess.run, now=None):
    os.chdir(root)
    steps = [
        run_step("regime", [PY, script("trader-scanner", "regime.py")], runner=runner),
        run_step("scan", [PY, script("trader-scanner", "scan.py"), "daily", "--quiet"], timeout=3600, runner=runner),
        run_step("news", [PY, script("trader-news", "news.py"), "--json", "--hours", "24"], runner=runner),
    ]
    state = {"date": now_et(now).strftime("%Y-%m-%d"), "steps": steps,
             "picks": rank_picks(load_json("trader/watchlist.json"))}
    os.makedirs("trader/state", exist_ok=True)
    with open("trader/state/autopilot.json", "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
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


def watch(root, picks, until="15:30", every=60, runner=subprocess.run, now_fn=now_et):
    """Poll every pick's ORB setup; the first trigger by rank wins. None at the cutoff."""
    os.chdir(root)
    cutoff = _hhmm(until)
    while True:
        t = now_fn()
        if (t.hour, t.minute) >= cutoff:
            return None
        fired = [p["symbol"] for p in picks
                 if (check_entry(p["symbol"], runner=runner) or {}).get("status") == "triggered"]
        if fired:
            res = check_entry(fired[0], write=True, runner=runner) or {"symbol": fired[0], "status": "triggered"}
            res["also_triggered"] = fired[1:]
            return res
        if every:
            time.sleep(every)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autopilot: morning steps, watch picks, decision card. Never orders.")
    ap.add_argument("cmd", choices=["day", "morning", "watch"])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--every", type=float, default=60)
    a = ap.parse_args(argv)
    os.chdir(a.root)
    if not is_trading_day():
        print("autopilot: not a trading day, nothing to do.")
        return 0
    state = morning(a.root) if a.cmd in ("day", "morning") else load_json("trader/state/autopilot.json", {})
    for s in state.get("steps", []):
        print("%-7s %s %ss" % (s["name"], "ok" if s["ok"] else "FAIL rc=%s" % s["rc"], s["seconds"]))
    print("picks: " + ", ".join(p["symbol"] for p in state.get("picks", [])))
    if a.cmd == "morning":
        return 0
    cfg = load_json("trader/config.json", {})
    trig = watch(a.root, state.get("picks", []), until=cfg.get("no_entries_after") or "15:30", every=a.every)
    print("trigger: %s" % (trig["symbol"] if trig else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
