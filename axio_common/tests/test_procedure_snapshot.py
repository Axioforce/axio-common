from axio_common.storage.procedure_resolution import snapshot_for_session


def test_snapshot_for_lite_day1_forward_order():
    # Fixture: lite-style master list with activities A, B, STK1-4, on days [1,2],
    # day 1 forward, day 2 forward (lite has no reverse).
    members = [("TR-BER", 0), ("TR-45V", 1), ("TR-STK1", 2), ("TR-STK2", 3),
               ("TR-STK3", 4), ("TR-STK4", 5)]
    day_members_by_day = {1: set(aid for aid, _ in members),
                          2: set(aid for aid, _ in members)}
    days = {1: {"reverse_order": False}, 2: {"reverse_order": False}}

    out = snapshot_for_session(
        family_activities=members,
        day_members_by_day=day_members_by_day,
        days=days,
        day_number=1,
    )
    assert out == ["TR-BER", "TR-45V", "TR-STK1", "TR-STK2", "TR-STK3", "TR-STK4"]


def test_snapshot_for_lp_day2_reverse():
    members = [("TR-BER", 0), ("TR-STK1", 1), ("TR-STK2", 2),
               ("TR-STK3", 3), ("TR-STK4", 4)]
    day_members_by_day = {1: {"TR-BER", "TR-STK1", "TR-STK2"},
                          2: {"TR-BER", "TR-STK3", "TR-STK4"}}
    days = {1: {"reverse_order": False}, 2: {"reverse_order": True}}

    out = snapshot_for_session(
        family_activities=members,
        day_members_by_day=day_members_by_day,
        days=days,
        day_number=2,
    )
    # Day 2's members in master order are TR-BER, TR-STK3, TR-STK4 → reversed.
    assert out == ["TR-STK4", "TR-STK3", "TR-BER"]


def test_snapshot_returns_empty_for_undefined_day():
    members = [("TR-BER", 0)]
    out = snapshot_for_session(
        family_activities=members,
        day_members_by_day={1: {"TR-BER"}},
        days={1: {"reverse_order": False}},
        day_number=99,  # day not defined
    )
    assert out == []
