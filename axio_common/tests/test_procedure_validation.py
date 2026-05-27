import unittest
from axio_common.storage import procedure_resolution as pr

KNOWN = {"TR-BER", "TR-45V", "TE-45V"}


def _payload(activities, days=(1, 2)):
    return {
        "days": [{"day_number": d, "reverse_order": False} for d in days],
        "activities": activities,
    }


class TestValidateFamilyPut(unittest.TestCase):
    def test_valid_payload_has_no_errors(self):
        p = _payload([
            {"activity_id": "TR-BER", "order_index": 0,
             "day_overrides": [{"day_number": 1}, {"day_number": 2}]},
            {"activity_id": "TE-45V", "order_index": 1,
             "day_overrides": [{"day_number": 1}]},
        ])
        self.assertEqual(pr.validate_family_put(p, KNOWN), [])

    def test_unknown_activity_is_reported(self):
        p = _payload([
            {"activity_id": "TR-NOPE", "order_index": 0, "day_overrides": []},
        ])
        errs = pr.validate_family_put(p, KNOWN)
        self.assertTrue(any("TR-NOPE" in e for e in errs))

    def test_day_override_for_undeclared_day_is_reported(self):
        p = _payload([
            {"activity_id": "TR-BER", "order_index": 0,
             "day_overrides": [{"day_number": 3}]},
        ], days=(1, 2))
        errs = pr.validate_family_put(p, KNOWN)
        self.assertTrue(any("day 3" in e for e in errs))

    def test_empty_activities_is_valid(self):
        self.assertEqual(pr.validate_family_put(_payload([]), KNOWN), [])


if __name__ == "__main__":
    unittest.main()
