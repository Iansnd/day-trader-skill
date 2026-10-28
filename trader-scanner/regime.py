#!/usr/bin/env python3
"""Macro Regime Filter (System 2) for trader-scanner. Standard library only.

Runs once pre-market (9:00 AM New York). It decides ONE thing, written to
trader/config.json as "market_regime": "RISK_ON" or "RISK_OFF":

  RISK_ON   SPY and QQQ each closed above their own 5-day AND 20-day simple
            moving averages, and the VIX closed below --vix-max (22).
  RISK_OFF  anything else, including missing data (fail closed).

trader-entry reads the flag: on RISK_OFF it skips the Jev scoring call and
logs "Execution halted: Macro conditions hostile to breakouts." The flag is
a mechanical rule from closes, not a judgement call, so this script is the
only writer; Claude does not hand-edit it.

    python3 regime.py                          # Yahoo daily bars (the repo's own data path)
    python3 regime.py --bars trader/macro_bars.json   # bars Claude fetched over the IBKR MCP
    python3 regime.py --dry-run --json         # evaluate, print, do not write config

How the IBKR MCP fits. The hosted IBKR bridge is OAuth-authenticated inside
Claude; a Python process cannot call it. So the 9:00 job is two steps:
  1. Claude asks the IBKR connector for ~30 daily bars of SPY, QQQ and VIX
     (three tool calls in one turn, so they run in parallel) and saves them
     as --bars JSON: {"SPY": [{"date": "YYYY-MM-DD", "close": 0.0}, ...],
     "QQQ": [...], "VIX": [...]}. Any extra keys per bar are ignored.
  2. Claude runs this script with --bars. If the file is missing or stale
     (not yesterday's session), the script falls back to Yahoo and says so.

Output trader/state/regime.json keeps the full evidence (closes, MAs, VIX,
which check failed) and trader/state/events.jsonl gets one line per run so
the dashboard can show every flip.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

SYMBOLS = ("SPY", "QQQ", "VIX")
YAHOO = {"SPY": "SPY", "QQQ": "QQQ", "VIX": "%5EVIX"}
CHART = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=3mo&interval=1d"
UA = "trader-scanner/regime (stdlib urllib)"
HALT = "Execution halted: Macro conditions hostile to breakouts."


class RegimeError(Exception):
    pass


# ---------------------------------------------------------------- bars


def yahoo_closes(sym, opener=None):
    req = urllib.request.Request(CHART % YAHOO[sym], headers={"User-Agent": UA})
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        doc = json.loads(r.read().decode("utf-8"))
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise RegimeError("Yahoo returned no chart for %s" % sym)
    ts = res[0].get("timestamp") or []
    closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get(
        "close"
    ) or []
    out = []
    for t, c in zip(ts, closes):
        if c is not None:
            out.append(
                {
                    "date": datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat(),
                    "close": float(c),
                }
            )
    return out


def load_bars(path):
    """--bars file -> {sym: [{date, close}]} with the three symbols, oldest first."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise RegimeError("cannot read %s: %s" % (path, e))
    out = {}
    for sym in SYMBOLS:
        rows = doc.get(sym) or doc.get(sym.lower()) or doc.get("^" + sym) or []
        bars = []
        for b in rows:
            try:
                bars.append({"date": str(b["date"])[:10], "close": float(b["close"])})
            except (KeyError, TypeError, ValueError):
                continue
        if not bars:
            raise RegimeError("%s has no usable %s bars" % (path, sym))
        bars.sort(key=lambda b: b["date"])
        out[sym] = bars
    return out


def gather(bars_path=None, opener=None, today=None):
    """({sym: bars}, source, notes). IBKR bars when fresh, else Yahoo."""
    notes = []
    if bars_path and os.path.exists(bars_path):
        try:
            bars = load_bars(bars_path)
            last = max(b[-1]["date"] for b in bars.values())
            today = today or datetime.date.today()
            if (
                today - datetime.date.fromisoformat(last)
            ).days <= 4:  # weekend + holiday slack
                return bars, "ibkr:" + bars_path, notes
            notes.append("%s is stale (last bar %s); using Yahoo" % (bars_path, last))
        except RegimeError as e:
            notes.append(str(e) + "; using Yahoo")
    elif bars_path:
        notes.append("%s not found; using Yahoo" % bars_path)
    bars = {}
    for sym in SYMBOLS:
        try:
            bars[sym] = yahoo_closes(sym, opener)
        except (urllib.error.URLError, OSError, ValueError, RegimeError) as e:
            raise RegimeError("Yahoo %s: %s" % (sym, e))
    return bars, "yahoo", notes


# ---------------------------------------------------------------- the rule


def sma(bars, n):
    closes = [b["close"] for b in bars[-n:]]
    return sum(closes) / len(closes) if len(closes) == n else None


def evaluate(bars, vix_max=22.0):
    """The regime from closes. Missing history fails closed (RISK_OFF)."""
    checks, detail = [], {}
    for sym in ("SPY", "QQQ"):
        b = bars.get(sym) or []
        last, m5, m20 = (b[-1]["close"] if b else None), sma(b, 5), sma(b, 20)
        ok = bool(
            last is not None
            and m5 is not None
            and m20 is not None
            and last > m5
            and last > m20
        )
        detail[sym] = {
            "close": last,
            "sma5": round(m5, 2) if m5 else None,
            "sma20": round(m20, 2) if m20 else None,
            "date": b[-1]["date"] if b else None,
            "above_both": ok,
        }
        checks.append((sym + " above 5d and 20d SMA", ok))
    v = bars.get("VIX") or []
    vix = v[-1]["close"] if v else None
    vok = bool(vix is not None and vix < vix_max)
    detail["VIX"] = {
        "close": vix,
        "max": vix_max,
        "date": v[-1]["date"] if v else None,
        "below_max": vok,
    }
    checks.append(("VIX below %.0f" % vix_max, vok))
    regime = "RISK_ON" if all(ok for _, ok in checks) else "RISK_OFF"
    return {
        "market_regime": regime,
        "checks": [{"check": c, "ok": ok} for c, ok in checks],
        "failed": [c for c, ok in checks if not ok],
        "detail": detail,
    }


# ---------------------------------------------------------------- files


def write_regime(config_path, regime, now_iso):
    """Read-modify-write: only market_regime and market_regime_at change."""
    cfg = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    before = cfg.get("market_regime")
    cfg["market_regime"] = regime
    cfg["market_regime_at"] = now_iso
    d = os.path.dirname(config_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    return before


def append_event(path, row):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def run(a, opener=None, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    bars, source, notes = gather(a.bars, opener, now.date())
    res = evaluate(bars, a.vix_max)
    res.update(
        {
            "created_by": "trader-scanner/regime",
            "source": source,
            "notes": notes,
            "checked_at": now_iso,
        }
    )
    if not a.dry_run:
        before = write_regime(a.config, res["market_regime"], now_iso)
        res["previous"] = before
        if os.path.dirname(a.out):
            os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
            fh.write("\n")
        append_event(
            a.events,
            {
                "at": now_iso,
                "step": "regime",
                "regime": res["market_regime"],
                "previous": before,
                "failed": res["failed"],
                "source": source,
            },
        )
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Macro Regime Filter: SPY/QQQ above 5d+20d SMA and VIX below a cap."
    )
    ap.add_argument(
        "--bars",
        default="trader/macro_bars.json",
        help="daily bars Claude saved from the IBKR MCP",
    )
    ap.add_argument("--vix-max", type=float, default=22.0)
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--out", default="trader/state/regime.json")
    ap.add_argument("--events", default="trader/state/events.jsonl")
    ap.add_argument(
        "--dry-run", action="store_true", help="evaluate and print; write nothing"
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run(a)
    except RegimeError as e:
        print("regime.py: %s" % e, file=sys.stderr)
        if not a.dry_run:  # fail closed: no data means no breakout clearance today
            write_regime(
                a.config,
                "RISK_OFF",
                datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
            print(
                "regime.py: wrote RISK_OFF to %s (no data)" % a.config, file=sys.stderr
            )
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        d = res["detail"]
        for sym in ("SPY", "QQQ"):
            x = d[sym]
            print(
                "%-4s %s  close %.2f  sma5 %s  sma20 %s  %s"
                % (
                    sym,
                    x["date"],
                    x["close"] or 0,
                    x["sma5"],
                    x["sma20"],
                    "above both" if x["above_both"] else "BELOW",
                )
            )
        print(
            "VIX  %s  close %.2f  cap %.0f  %s"
            % (
                d["VIX"]["date"],
                d["VIX"]["close"] or 0,
                d["VIX"]["max"],
                "ok" if d["VIX"]["below_max"] else "TOO HIGH",
            )
        )
        print(
            "source %s%s"
            % (
                res["source"],
                ("  (" + "; ".join(res["notes"]) + ")") if res["notes"] else "",
            )
        )
        print(
            "market_regime = %s%s"
            % (
                res["market_regime"],
                "" if a.dry_run else "  (written to %s)" % a.config,
            )
        )
        if res["market_regime"] == "RISK_OFF":
            print(HALT)
    return 0 if res["market_regime"] == "RISK_ON" else 1


if __name__ == "__main__":
    sys.exit(main())
