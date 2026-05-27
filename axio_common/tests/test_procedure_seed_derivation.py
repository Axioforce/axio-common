import unittest
from axio_common.storage import procedure_resolution as pr


class TestDeriveSeedForFamily(unittest.TestCase):
    def test_lp_day2_is_reversed_day1(self):
        day1 = ["TR-BER", "TR-STK1", "TR-STK2", "TR-TIL", "TR-PLY"]
        day2 = ["TR-PLY", "TR-TIL", "TR-STK3", "TR-STK4", "TR-BER"]
        seed = pr.derive_seed_for_family({1: day1, 2: day2})
        self.assertTrue(seed["days"][2]["reverse_order"])
        self.assertFalse(seed["days"][1]["reverse_order"])
        self.assertIn("TR-STK1", seed["membership"][1])
        self.assertNotIn("TR-STK3", seed["membership"][1])
        self.assertIn("TR-STK3", seed["membership"][2])
        self.assertNotIn("TR-STK1", seed["membership"][2])
        order = seed["order"]
        for day in (1, 2):
            self.assertTrue(set(seed["membership"][day]).issubset(set(order)))
        # Day-2-exclusive STKs land in the STK slot (grouped right after the
        # day-1 STKs), NOT appended at the end — so a reversed day 2 keeps them
        # in the reversed-slot position instead of jumping to the top.
        self.assertEqual(
            order,
            ["TR-BER", "TR-STK1", "TR-STK2", "TR-STK3", "TR-STK4", "TR-TIL", "TR-PLY"],
        )

    def test_lite_day1_equals_day2_not_reversed(self):
        same = ["TR-BER", "TR-STK1", "TR-STK2", "TR-STK3", "TR-STK4"]
        seed = pr.derive_seed_for_family({1: list(same), 2: list(same)})
        self.assertFalse(seed["days"][1]["reverse_order"])
        self.assertFalse(seed["days"][2]["reverse_order"])
        self.assertEqual(set(seed["membership"][1]), set(same))
        self.assertEqual(set(seed["membership"][2]), set(same))


if __name__ == "__main__":
    unittest.main()
