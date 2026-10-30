#!/usr/bin/env python3
"""IBKR through the TWS API (ib_async) for the paper account: place the entry,
attach an OCA stop + target priced from the real fill, hold to the exit.

    python3 ibkr_tws.py ping        # proves the gateway is up and it is a paper account

Every function takes the `ib` object so tests pass tests/fake_ib.py. ib_async is
imported lazily, inside connect(), so the rest of the repo stays stdlib-only.
Port 7496 (live) is refused whatever the config says.
"""
import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAPER_PORT, LIVE_PORT = 7497, 7496
DEFAULT_TWS = {"host": "127.0.0.1", "port": PAPER_PORT, "client_id": 7}


class TwsError(Exception):
    pass


def _ib_async():
    try:
        import ib_async
    except ImportError:
        # ponytail: Homebrew python is PEP 668 managed; the package lives in the project venv
        sys.path.append(os.path.join(ROOT, ".venv", "lib", "python%d.%d" % sys.version_info[:2], "site-packages"))
        import ib_async
    return ib_async


def connect(cfg, factory=None):
    """cfg is trader/config.json. Refuses anything but paper on the paper port."""
    tws = dict(DEFAULT_TWS, **(cfg.get("tws") or {}))
    if cfg.get("paper") is not True:
        raise TwsError("config paper is not true; the TWS path is paper only")
    if int(tws["port"]) == LIVE_PORT:
        raise TwsError("live port %d refused" % LIVE_PORT)
    ib = (factory or _ib_async().IB)()
    try:
        ib.connect(tws["host"], int(tws["port"]), clientId=int(tws["client_id"]), timeout=10)
    except Exception as e:   # socket refused, gateway logged out, wrong port
        raise TwsError("cannot reach IB Gateway at %s:%s (%s)" % (tws["host"], tws["port"], e))
    return ib


def positions(ib):
    """symbol -> shares, stocks only."""
    return {p.contract.symbol: float(p.position) for p in ib.positions() if p.contract.secType == "STK"}


def stock(symbol):
    return _ib_async().Stock(symbol, "SMART", "USD")


def cmd_ping(a):
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    ib = connect(cfg)
    vals = {v.tag: v.value for v in ib.accountSummary()}
    acct = (ib.managedAccounts() or ["?"])[0]
    ib.disconnect()
    if not acct.startswith("DU"):
        raise TwsError("account %s is not a paper account (DU...)" % acct)
    print("connected paper account %s net_liquidation %s" % (acct, vals.get("NetLiquidation", "?")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="trader/config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    a = ap.parse_args(argv)
    try:
        return {"ping": cmd_ping}[a.cmd](a)
    except TwsError as e:
        print("ibkr_tws: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
