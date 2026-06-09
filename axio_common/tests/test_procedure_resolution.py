import unittest
from axio_common.storage import procedure_resolution as pr


class TestResolveDescription(unittest.TestCase):
    def test_day_wins_over_family_and_base(self):
        self.assertEqual(pr.resolve_description("base", "fam", "day"), "day")

    def test_family_wins_when_no_day(self):
        self.assertEqual(pr.resolve_description("base", "fam", None), "fam")

    def test_base_when_no_overrides(self):
        self.assertEqual(pr.resolve_description("base", None, None), "base")

    def test_empty_string_counts_as_unset(self):
        self.assertEqual(pr.resolve_description("base", "", ""), "base")

    def test_all_none(self):
        self.assertIsNone(pr.resolve_description(None, None, None))


class TestAccumulateTags(unittest.TestCase):
    def test_concatenates_in_order_catalog_family_day(self):
        self.assertEqual(pr.accumulate_tags(["a"], ["b"], ["c"]), ["a", "b", "c"])

    def test_dedupes_preserving_first_occurrence(self):
        self.assertEqual(
            pr.accumulate_tags(["a", "b"], ["b", "c"], ["a", "d"]),
            ["a", "b", "c", "d"],
        )

    def test_handles_none_levels(self):
        self.assertEqual(pr.accumulate_tags(None, ["x"], None), ["x"])
        self.assertEqual(pr.accumulate_tags(None, None, None), [])


class TestDeriveDaySequence(unittest.TestCase):
    def test_forward_when_not_reversed(self):
        self.assertEqual(pr.derive_day_sequence(["A", "B", "C"], reverse=False), ["A", "B", "C"])

    def test_reversed_when_flag_set(self):
        self.assertEqual(pr.derive_day_sequence(["A", "B", "C"], reverse=True), ["C", "B", "A"])


FAMILY_ACTS = [(a, i) for i, a in enumerate([
    "TR-MDS","TR-MIP","TR-LAT","TR-LNG","TR-WLK","TR-TOE",
    "TE-TOE","TE-WLK","TE-LNG","TE-LAT","TE-MIP","TE-MDS",
])]


class TestSnapshotForSession(unittest.TestCase):
    def test_per_day_order_overrides_master_order(self):
        members = {2: {"TR-MDS","TR-LNG","TR-LAT","TR-MIP","TR-WLK","TR-TOE"}}
        # Day-2 paper order: MDS, LNG, LAT, MIP, WLK, TOE
        order = {2: {"TR-MDS":0,"TR-LNG":1,"TR-LAT":2,"TR-MIP":3,"TR-WLK":4,"TR-TOE":5}}
        out = pr.snapshot_for_session(
            family_activities=FAMILY_ACTS,
            day_members_by_day=members,
            days={2: {"reverse_order": False}},
            day_number=2,
            day_order_by_day=order,
        )
        self.assertEqual(out, ["TR-MDS","TR-LNG","TR-LAT","TR-MIP","TR-WLK","TR-TOE"])

    def test_no_per_day_order_falls_back_to_master_then_reverse(self):
        members = {1: {"TR-MDS","TR-MIP","TR-LAT"}}
        out = pr.snapshot_for_session(
            family_activities=FAMILY_ACTS,
            day_members_by_day=members,
            days={1: {"reverse_order": True}},
            day_number=1,
        )  # no day_order_by_day → master order then reversed
        self.assertEqual(out, ["TR-LAT","TR-MIP","TR-MDS"])

    def test_per_day_order_mixes_explicit_none_and_absent_without_crash(self):
        members = {3: {"TR-MDS", "TR-MIP", "TR-LAT", "TR-LNG"}}
        # TR-LAT=0, TR-MDS=1 (explicit); TR-MIP=None (explicit None); TR-LNG absent
        per_day = {3: {"TR-MDS": 1, "TR-LAT": 0, "TR-MIP": None}}
        out = pr.snapshot_for_session(
            family_activities=FAMILY_ACTS,
            day_members_by_day=members,
            days={3: {"reverse_order": False}},
            day_number=3,
            day_order_by_day=per_day,
        )
        # explicit-index members first (by index): TR-LAT(0), TR-MDS(1);
        # then None/absent by master order: TR-MIP(master 1), TR-LNG(master 3)
        self.assertEqual(out, ["TR-LAT", "TR-MDS", "TR-MIP", "TR-LNG"])

    def test_legacy_fallback_forward_order_when_no_per_day(self):
        members = {1: {"TR-MDS", "TR-MIP", "TR-LAT"}}
        out = pr.snapshot_for_session(
            family_activities=FAMILY_ACTS, day_members_by_day=members,
            days={1: {"reverse_order": False}}, day_number=1,
        )
        self.assertEqual(out, ["TR-MDS", "TR-MIP", "TR-LAT"])  # master order, not reversed

    def test_duplicate_per_day_index_is_deterministic_by_master_order(self):
        members = {1: {"TR-MDS", "TR-MIP", "TR-LAT"}}
        # TR-LAT and TR-MDS share index 0 → tiebreak by master order (TR-MDS=0 before TR-LAT=2)
        per_day = {1: {"TR-LAT": 0, "TR-MDS": 0, "TR-MIP": 1}}
        out = pr.snapshot_for_session(
            family_activities=FAMILY_ACTS, day_members_by_day=members,
            days={1: {"reverse_order": False}}, day_number=1,
            day_order_by_day=per_day,
        )
        self.assertEqual(out, ["TR-MDS", "TR-LAT", "TR-MIP"])


if __name__ == "__main__":
    unittest.main()
