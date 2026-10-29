#!/usr/bin/env python3
"""IBKR account -> trader/portfolio.json, so trader-risk and trader-budget check
orders against the broker's real cash and positions instead of a hand-kept file.

The hosted IBKR connector is OAuth inside Claude; Python cannot call it. So:
  1. Claude calls get_account_summary and get_account_positions and saves them as
     trader/ibkr_account.json: {"fetched_at", "summary": {...}, "positions": [...]}
  2. python3 ibkr_sync.py            # -> trader/portfolio.json

Cash = summary.total_cash_value. Each STK position becomes one lot at its
average price. Non-stock positions are skipped and listed in "skipped".
"""
import argparse
import datetime
import json
import sys


def convert(acct):
    s = acct["summary"]
    cash = float(s.get("total_cash_value", s.get("available_funds")))
    positions, skipped = [], []
    for p in acct.get("positions", []):
        sym = str(p.get("contract_description") or p.get("symbol") or "").upper()
        qty = float(p.get("position") or 0)
        if p.get("asset_class", "STK") != "STK" or not sym or qty <= 0:
            skipped.append(sym or str(p.get("contract_id")))
            continue
        avg = float(p.get("average_price") or 0)
        positions.append({"symbol": sym, "shares": qty,
                          "lots": [{"shares": qty, "cost": round(qty * avg, 2), "price": avg}]})
    return {"cash": round(cash, 2), "positions": positions, "skipped": skipped,
            "net_liquidation": s.get("net_liquidation"), "currency": s.get("currency", "USD"),
            "source": "ibkr", "fetched_at": acct.get("fetched_at"),
            "synced_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="trader/ibkr_account.json")
    ap.add_argument("--out", default="trader/portfolio.json")
    ap.add_argument("--max-age-hours", type=float, default=24, help="refuse a stale snapshot")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    with open(a.account, encoding="utf-8") as fh:
        acct = json.load(fh)
    fetched = acct.get("fetched_at")
    if fetched:
        age = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ")
        if age.total_seconds() > a.max_age_hours * 3600:
            print("ibkr_sync: %s is %.1f h old; fetch a fresh one from the IBKR connector" % (a.account, age.total_seconds() / 3600))
            return 2
    pf = convert(acct)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(pf, fh, indent=2)
    if a.json:
        print(json.dumps(pf, indent=2))
    else:
        print("Wrote %s: cash $%.2f, %d position(s)%s" % (a.out, pf["cash"], len(pf["positions"]),
              (", skipped " + ", ".join(pf["skipped"])) if pf["skipped"] else ""))
    return 0


def demo():
    acct = {"summary": {"total_cash_value": 100.5, "net_liquidation": 300},
            "positions": [{"contract_description": "ABC", "position": 10, "average_price": 20, "asset_class": "STK"},
                          {"contract_description": "ES", "position": 1, "asset_class": "FUT"}]}
    pf = convert(acct)
    assert pf["cash"] == 100.5 and pf["positions"][0]["symbol"] == "ABC"
    assert pf["positions"][0]["lots"][0]["cost"] == 200.0 and pf["skipped"] == ["ES"]
    print("ok")


if __name__ == "__main__":
    sys.exit(demo() if "--demo" in sys.argv else main())
