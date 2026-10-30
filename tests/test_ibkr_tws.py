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


if __name__ == "__main__":
    unittest.main()
