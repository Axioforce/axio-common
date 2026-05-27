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


if __name__ == "__main__":
    unittest.main()
