import unittest

from _load import load
from fake_ib import FakeIB

tws = load("trader-broker", "ibkr_tws")


class Connect(unittest.TestCase):
    def test_connect_refuses_live_port(self):
        with self.assertRaises(tws.TwsError):
            tws.connect({"paper": True, "tws": {"port": 7496}}, factory=FakeIB)

    def test_connect_refuses_non_paper(self):
        with self.assertRaises(tws.TwsError):
            tws.connect({"paper": False}, factory=FakeIB)

    def test_connect_defaults(self):
        ib = tws.connect({"paper": True}, factory=FakeIB)
        self.assertEqual(ib.connected_to, ("127.0.0.1", 7497, 7))

    def test_positions_stk_only(self):
        ib = FakeIB(positions={"ICE": 16, "ES": 1})
        self.assertEqual(tws.positions(ib), {"ICE": 16.0})


ICE = {"symbol": "ICE", "side": "buy", "qty": "16", "type": "market", "time_in_force": "day",
       "order_class": "bracket", "stop_loss_stop_price": "166.51",
       "plan": {"stop": 166.51, "target_r": 2.0}}


class Bracket(unittest.TestCase):
    def test_bracket_prices_from_fill(self):
        self.assertEqual(tws.bracket_prices(168.79, 166.51, 2.0), (166.51, 173.35))

    def test_place_entry_returns_fill(self):
        ib = FakeIB(); ib.script.append(("fill", 16, 168.79))
        r = tws.place_entry(ib, ICE)
        self.assertEqual((r["filled_qty"], r["fill_price"], r["status"]), (16, 168.79, "Filled"))

    def test_place_entry_times_out_and_cancels(self):
        ib = FakeIB()
        with self.assertRaises(tws.TwsError):
            tws.place_entry(ib, ICE, wait_s=2)
        self.assertEqual(ib.cancelled, [1])

    def test_bracket_uses_filled_qty(self):
        ib = FakeIB()
        b = tws.attach_bracket(ib, ICE, filled_qty=10, fill_price=168.79)
        sells = [t for t in ib.openTrades() if t.order.action == "SELL"]
        self.assertEqual([t.order.totalQuantity for t in sells], [10, 10])
        self.assertEqual({t.order.ocaGroup for t in sells}, {b["oca_group"]})
        self.assertEqual((b["stop"], b["target"], b["qty"]), (166.51, 173.35, 10))

    def test_cancel_group(self):
        ib = FakeIB(); b = tws.attach_bracket(ib, ICE, 16, 168.79)
        self.assertEqual(tws.cancel_group(ib, b["oca_group"]), 2)
        self.assertEqual(ib.openTrades(), [])


if __name__ == "__main__":
    unittest.main()
