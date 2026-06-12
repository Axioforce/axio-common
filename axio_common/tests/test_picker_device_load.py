"""Tests for the picker's parallel, cached device loading.

load_types_and_devices is a pure data helper (no Tk windows are created),
so these run headless; the storage calls are monkeypatched.
"""
import pytest

import axio_common.storage.picker as picker


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(
        picker, "_device_cache", {"ts": 0.0, "types": None, "devices": None}
    )


def _fake_listing(monkeypatch, types, devices_by_type, calls):
    def list_device_types():
        calls.append("types")
        return types

    def list_devices(t):
        calls.append(f"devices:{t}")
        return devices_by_type[t]

    monkeypatch.setattr(picker._sc, "list_device_types", list_device_types)
    monkeypatch.setattr(picker._sc, "list_devices", list_devices)


def test_loads_all_devices_sorted(monkeypatch):
    _fake_listing(
        monkeypatch, ["10", "11"],
        {"10": ["10-00000002", "10-00000001"], "11": ["11-00000009"]}, [],
    )
    types, devices = picker.load_types_and_devices(use_cache=False)
    assert types == ["10", "11"]
    assert devices == ["10-00000001", "10-00000002", "11-00000009"]


def test_callbacks_fire_per_type(monkeypatch):
    _fake_listing(
        monkeypatch, ["10", "11"],
        {"10": ["10-00000001"], "11": ["11-00000001"]}, [],
    )
    seen_types, chunks = [], []
    picker.load_types_and_devices(
        on_types=seen_types.append,
        on_devices_chunk=lambda t, devs, done, total: chunks.append(
            (t, list(devs), done, total)
        ),
        use_cache=False,
    )
    assert seen_types == [["10", "11"]]
    # One chunk per type, completion counter reaches the total
    assert sorted(c[0] for c in chunks) == ["10", "11"]
    assert sorted(c[2] for c in chunks) == [1, 2]
    assert all(c[3] == 2 for c in chunks)


def test_cache_hit_skips_network(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    n_first = len(calls)
    chunks = []
    types, devices = picker.load_types_and_devices(
        on_devices_chunk=lambda t, devs, done, total: chunks.append(t)
    )
    assert len(calls) == n_first  # no new network calls
    assert devices == ["10-00000001"]
    assert chunks == [None]  # cache hit delivers one full chunk


def test_use_cache_false_refetches(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    picker.load_types_and_devices(use_cache=False)
    assert calls.count("types") == 2


def test_expired_cache_refetches(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    monkeypatch.setattr(picker, "_DEVICE_CACHE_TTL", 0.0)
    picker.load_types_and_devices()
    assert calls.count("types") == 2


# ---------- drag-select row math (needs a Tk interpreter, no window shown) ----


@pytest.fixture
def tree():
    import tkinter as tk
    from tkinter import ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk unavailable in this environment")
    root.withdraw()
    t = ttk.Treeview(root)
    # a (open, with children) / b / c — visible order: a, a1, a2, b, c
    t.insert("", "end", iid="a", text="a", open=True)
    t.insert("a", "end", iid="a1", text="a1")
    t.insert("a", "end", iid="a2", text="a2")
    t.insert("", "end", iid="b", text="b", open=False)
    t.insert("b", "end", iid="b1", text="b1")  # hidden: parent collapsed
    t.insert("", "end", iid="c", text="c")
    yield t
    root.destroy()


def test_visible_rows_skips_collapsed_branches(tree):
    assert picker._visible_rows(tree) == ["a", "a1", "a2", "b", "c"]


def test_drag_span_inclusive_and_direction_agnostic(tree):
    assert picker._drag_span(tree, "a1", "b") == ["a1", "a2", "b"]
    assert picker._drag_span(tree, "b", "a1") == ["a1", "a2", "b"]
    assert picker._drag_span(tree, "c", "c") == ["c"]


def test_drag_span_vanished_row_returns_empty(tree):
    assert picker._drag_span(tree, "b1", "c") == []  # b1 not visible
