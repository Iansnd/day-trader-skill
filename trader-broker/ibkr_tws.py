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
    if _has_ib():
        return _ib_async().Stock(symbol, "SMART", "USD")
    import types
    return types.SimpleNamespace(symbol=symbol, secType="STK")   # tests without the package


def _has_ib():
    try:
        _ib_async()
        return True
    except ImportError:
        return False


def _order(kind, action, qty, price=None, **kw):
    """kind: MKT | LMT | STP. Same fields whether ib_async builds it or a stand-in does."""
    base = dict(action=action, totalQuantity=float(qty), orderType=kind, tif="DAY", ocaGroup="", ocaType=0,
                lmtPrice=0.0, auxPrice=0.0, orderId=0)
    if _has_ib():
        ib = _ib_async()
        o = {"MKT": lambda: ib.MarketOrder(action, float(qty)),
             "LMT": lambda: ib.LimitOrder(action, float(qty), float(price)),
             "STP": lambda: ib.StopOrder(action, float(qty), float(price))}[kind]()
    else:
        import types
        o = types.SimpleNamespace(**base)
        if kind == "LMT":
            o.lmtPrice = float(price)
        elif kind == "STP":
            o.auxPrice = float(price)
    o.tif = "DAY"
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _wait_fill(ib, trade, wait_s):
    for _ in range(int(wait_s)):
        if trade.orderStatus.status == "Filled":
            return True
        ib.sleep(1)
    return trade.orderStatus.status == "Filled"


def place_entry(ib, order, wait_s=30):
    """The entry from trader/orders.json as a DAY market (or limit) order; waits for the fill.
    Cancels and raises when nothing filled inside wait_s: a market order that sits is a broker problem."""
    sym = order["symbol"].upper()
    action = order["side"].upper()
    if order.get("type") == "limit":
        o = _order("LMT", action, order["qty"], order["limit_price"])
    else:
        o = _order("MKT", action, order["qty"])
    trade = ib.placeOrder(stock(sym), o)
    if not _wait_fill(ib, trade, wait_s) and float(trade.orderStatus.filled or 0) <= 0:
        ib.cancelOrder(trade.order)
        raise TwsError("entry %s %s not filled in %ss (status %s); cancelled" % (action, sym, wait_s, trade.orderStatus.status))
    st = trade.orderStatus
    return {"order_id": trade.order.orderId, "status": st.status, "filled_qty": float(st.filled),
            "fill_price": float(st.avgFillPrice)}


def bracket_prices(fill_price, stop, target_r):
    """Stop stays the plan's; the target is target_r times the real risk above the real fill."""
    return float(stop), round(fill_price + float(target_r) * (fill_price - float(stop)), 2)


def attach_bracket(ib, order, filled_qty, fill_price, date=None, entry_id=0):
    """Two DAY sells for what actually filled, one OCA group so the broker cancels the other on a fill."""
    import datetime
    sym = order["symbol"].upper()
    plan = order.get("plan") or {}
    stop, target = bracket_prices(fill_price, order.get("stop_loss_stop_price") or plan["stop"], plan.get("target_r", 2.0))
    group = "%s-%s-%s" % (sym, date or datetime.date.today().isoformat(), entry_id)
    stop_t = ib.placeOrder(stock(sym), _order("STP", "SELL", filled_qty, stop, ocaGroup=group, ocaType=1))
    target_t = ib.placeOrder(stock(sym), _order("LMT", "SELL", filled_qty, target, ocaGroup=group, ocaType=1))
    return {"oca_group": group, "stop_id": stop_t.order.orderId, "target_id": target_t.order.orderId,
            "stop": stop, "target": target, "qty": float(filled_qty), "symbol": sym}


def cancel_group(ib, oca_group):
    n = 0
    for t in list(ib.openTrades()):
        if t.order.ocaGroup == oca_group:
            ib.cancelOrder(t.order)
            n += 1
    return n


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
