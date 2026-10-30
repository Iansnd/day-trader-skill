"""A stand-in for ib_async.IB with the attribute names the real one uses
(Trade.order.orderId, Trade.orderStatus.status/filled/avgFillPrice, Position.contract.secType).
Tests push events onto `script`; each `sleep` applies the next one."""

import types


def _ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeIB:
    def __init__(self, positions=None, fail_on_sell=False):
        self.connected_to = None
        self._positions = positions or {}
        self.script = []          # ("fill", qty, price) | ("fill_sell", order_id | "next_market", qty, price)
        self.trades = []
        self.cancelled = []
        self.fail_on_sell = fail_on_sell
        self.summary = {"AccountType": "PAPER", "NetLiquidation": "1000000.00"}
        self._next_id = 1

    # --- ib_async surface
    def connect(self, host, port, clientId=1, timeout=4, **kw):
        self.connected_to = (host, port, clientId)

    def disconnect(self):
        self.connected_to = None

    def managedAccounts(self):
        return ["DU000000"]

    def accountSummary(self, account=""):
        return [_ns(tag=k, value=v, account="DU000000") for k, v in self.summary.items()]

    def positions(self, account=""):
        return [_ns(contract=_ns(symbol=s, secType="FUT" if s == "ES" else "STK"), position=q, avgCost=0)
                for s, q in self._positions.items()]

    def placeOrder(self, contract, order):
        if self.fail_on_sell and order.action == "SELL":
            raise ConnectionError("gateway dropped")
        order.orderId = self._next_id
        self._next_id += 1
        t = _ns(contract=contract, order=order,
                orderStatus=_ns(status="Submitted", filled=0.0, remaining=order.totalQuantity, avgFillPrice=0.0))
        self.trades.append(t)
        return t

    def cancelOrder(self, order, **kw):
        self.cancelled.append(order.orderId)
        for t in self.trades:
            if t.order.orderId == order.orderId and t.orderStatus.status not in ("Filled",):
                t.orderStatus.status = "Cancelled"

    def openTrades(self):
        return [t for t in self.trades if t.orderStatus.status in ("Submitted", "PreSubmitted", "PendingSubmit")]

    def sleep(self, secs=0.02):
        if self.script:
            self._apply(self.script.pop(0))
        return True

    # --- scripted events
    def _apply(self, ev):
        kind = ev[0]
        if kind == "fill":
            _, qty, price = ev
            t = next(t for t in self.openTrades() if t.order.action == "BUY")
        else:
            _, oid, qty, price = ev
            opens = [t for t in self.openTrades() if t.order.action == "SELL"]
            t = next(t for t in opens if t.order.orderType == "MKT") if oid == "next_market" \
                else next(t for t in opens if t.order.orderId == oid)
        t.orderStatus.status, t.orderStatus.filled, t.orderStatus.remaining, t.orderStatus.avgFillPrice = \
            "Filled", float(qty), 0.0, float(price)
        if t.order.ocaGroup:   # the broker cancels the OCA siblings
            for o in self.openTrades():
                if o is not t and o.order.ocaGroup == t.order.ocaGroup:
                    o.orderStatus.status = "Cancelled"
