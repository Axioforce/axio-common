"""
Tk pickers backed by the Tigris bucket.

Drop-in replacements for tkinter.filedialog calls in apps that browsed local
calibration directories:

    pick_session(parent=None) -> str | None
        Replaces filedialog.askdirectory(). Returns a local cache path to the
        chosen session's date directory (with OneDrive-shaped calibration_data/
        layout inside), after downloading session files. None if cancelled.

    pick_files(parent=None, extensions=...) -> tuple[str, ...]
        Replaces filedialog.askopenfilenames(). Returns local cache paths to
        the chosen files after downloading. Empty tuple if cancelled.

Both pickers show a hierarchical tree (type -> device -> date -> [train|test|models]
-> file). Children are loaded lazily on expansion so the dialog opens fast even on
large buckets.

Downloads are sequential with a small progress dialog. Files already in cache are
returned without network I/O.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional, Sequence

from . import storage_core as _sc

# Tree node kinds (encoded in iid suffixes for routing on expand)
_K_TYPE = "type"
_K_DEVICE = "device"
_K_DATE = "date"
_K_KIND = "kind"   # train | test | models
_K_MODEL = "model"  # one compound-name dir under models/
_K_FILE = "file"

_PLACEHOLDER = "__loading__"


def _split_iid(iid: str) -> tuple[str, str]:
    """iid format: '<kind>:<payload>'. Payload encoding depends on kind.

    Tree placeholder iids end with '/<_PLACEHOLDER>' (the "loading..." rows
    inserted under collapsed nodes). They share the parent's '<kind>:' prefix,
    so a naive partition would mis-classify a placeholder as its parent's
    kind and let it slip through kind-filters in on_ok handlers — producing
    bogus picks like (device_id, '<iso>/__loading__') that flow downstream
    into mangled cache paths. Normalize them to ('placeholder', '') so every
    caller can filter with one uniform check.
    """
    if iid == _PLACEHOLDER or iid.endswith(f"/{_PLACEHOLDER}"):
        return "placeholder", ""
    kind, _, payload = iid.partition(":")
    return kind, payload


# ---------- progress dialog ----------

class _ProgressDialog:
    def __init__(self, parent, title="Downloading"):
        self.top = tk.Toplevel(parent) if parent else tk.Toplevel()
        self.top.title(title)
        self.top.geometry("420x110+300+250")
        self.top.transient(parent)
        self.top.grab_set()
        self.label = ttk.Label(self.top, text="Preparing...")
        self.label.pack(padx=12, pady=(12, 4), anchor="w")
        self.bar = ttk.Progressbar(self.top, length=380, mode="determinate")
        self.bar.pack(padx=12, pady=4)
        self.detail = ttk.Label(self.top, text="", foreground="#666")
        self.detail.pack(padx=12, pady=(0, 8), anchor="w")
        self.top.update_idletasks()

    def update(self, idx: int, total: int, key: Optional[str]):
        self.bar["maximum"] = max(total, 1)
        self.bar["value"] = idx
        if key:
            self.label.config(text=f"{idx}/{total}")
            self.detail.config(text=os.path.basename(key))
        else:
            self.label.config(text=f"Done ({total} files)")
            self.detail.config(text="")
        self.top.update_idletasks()

    def close(self):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


def _run_with_progress(parent, total: int, action):
    """Run a download action in a worker thread with a progress dialog.
    `action(progress_cb)` is invoked on the worker. progress_cb signature: (idx, total, key)."""
    dlg = _ProgressDialog(parent, title="Downloading from Tigris")
    state = {"done": False, "result": None, "error": None, "events": []}
    lock = threading.Lock()

    def progress(idx, total_, key):
        with lock:
            state["events"].append((idx, total_, key))

    def worker():
        try:
            state["result"] = action(progress)
        except Exception as e:
            state["error"] = e
        finally:
            state["done"] = True

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def pump():
        with lock:
            evts = state["events"]
            state["events"] = []
        for ev in evts:
            dlg.update(*ev)
        if state["done"]:
            dlg.close()
            if state["error"]:
                raise state["error"]
        else:
            dlg.top.after(80, pump)

    dlg.top.after(80, pump)
    dlg.top.wait_window()
    if state["error"]:
        raise state["error"]
    return state["result"]


# ---------- shared tree-building ----------

class _BucketTree:
    """Common logic for building a lazy-loaded ttk.Treeview of the bucket."""

    def __init__(self, parent, *, expand_files: bool):
        self.expand_files = expand_files
        self.tree = ttk.Treeview(parent, columns=("size",), show="tree headings")
        self.tree.heading("#0", text="Path")
        self.tree.heading("size", text="Size")
        self.tree.column("size", width=110, anchor="e")
        self.tree.bind("<<TreeviewOpen>>", self._on_open)

        # Populate top-level types
        for t in _sc.list_device_types():
            iid = f"{_K_TYPE}:{t}"
            self.tree.insert("", "end", iid=iid, text=t, open=False)
            self.tree.insert(iid, "end", iid=f"{iid}/{_PLACEHOLDER}", text="loading...")

    def _on_open(self, _evt):
        iid = self.tree.focus()
        children = self.tree.get_children(iid)
        if not (len(children) == 1 and children[0].endswith(_PLACEHOLDER)):
            return  # already loaded
        self.tree.delete(children[0])
        self._populate(iid)

    def _populate(self, iid: str):
        kind, payload = _split_iid(iid)
        if kind == _K_TYPE:
            type_id = payload
            for dev in _sc.list_devices(type_id):
                child_iid = f"{_K_DEVICE}:{dev}"
                self.tree.insert(iid, "end", iid=child_iid, text=dev, open=False)
                self.tree.insert(child_iid, "end", iid=f"{child_iid}/{_PLACEHOLDER}", text="loading...")
        elif kind == _K_DEVICE:
            device_id = payload
            for d in _sc.list_dates(device_id):
                if d == "models":
                    child_iid = f"{_K_KIND}:{device_id}|models"
                    self.tree.insert(iid, "end", iid=child_iid, text="models", open=False)
                    self.tree.insert(child_iid, "end", iid=f"{child_iid}/{_PLACEHOLDER}", text="loading...")
                else:
                    child_iid = f"{_K_DATE}:{device_id}|{d}"
                    self.tree.insert(iid, "end", iid=child_iid, text=d, open=False)
                    self.tree.insert(child_iid, "end", iid=f"{child_iid}/{_PLACEHOLDER}", text="loading...")
        elif kind == _K_DATE:
            device_id, date = payload.split("|", 1)
            listing = _sc.list_session(device_id, date)
            for k in ("train", "test"):
                files = listing.train if k == "train" else listing.test
                if not files:
                    continue
                child_iid = f"{_K_KIND}:{device_id}|{date}|{k}"
                self.tree.insert(iid, "end", iid=child_iid, text=k, open=False)
                if self.expand_files:
                    for full_key in files:
                        leaf_iid = f"{_K_FILE}:{full_key}"
                        self.tree.insert(
                            child_iid, "end", iid=leaf_iid,
                            text=os.path.basename(full_key),
                        )
                else:
                    self.tree.insert(child_iid, "end", iid=f"{child_iid}/{_PLACEHOLDER}", text=f"{len(files)} files")
            if listing.tests_txt:
                self.tree.insert(
                    iid, "end", iid=f"{_K_FILE}:{listing.tests_txt}", text="tests.txt"
                )
        elif kind == _K_KIND and payload.endswith("|models"):
            device_id = payload.split("|", 1)[0]
            for compound in _sc.list_models(device_id):
                child_iid = f"{_K_MODEL}:{device_id}|{compound}"
                self.tree.insert(iid, "end", iid=child_iid, text=compound, open=False)
                self.tree.insert(child_iid, "end", iid=f"{child_iid}/{_PLACEHOLDER}", text="loading...")
        elif kind == _K_KIND:
            # train/test that wasn't preloaded with files (expand_files=False)
            device_id, date, k = payload.split("|", 2)
            listing = _sc.list_session(device_id, date)
            files = listing.train if k == "train" else listing.test
            for full_key in files:
                self.tree.insert(iid, "end", iid=f"{_K_FILE}:{full_key}", text=os.path.basename(full_key))
        elif kind == _K_MODEL:
            device_id, compound = payload.split("|", 1)
            prefix = f"{_sc.models_prefix(device_id)}{compound}/"
            for full_key in sorted(_sc.list_prefix(prefix, recursive=True)):
                rel = full_key.removeprefix(prefix)
                self.tree.insert(iid, "end", iid=f"{_K_FILE}:{full_key}", text=rel)


# ---------- pick_session ----------

def pick_session(parent: Optional[tk.Misc] = None, *, title: str = "Select session") -> Optional[str]:
    """Show a tree dialog rooted at types -> devices -> dates. User selects a date.
    Downloads all session files to cache and returns the local session directory.
    Returns None if cancelled.

    Drop-in replacement for filedialog.askdirectory() at the date-directory level.
    """
    owns_root = parent is None
    if owns_root:
        parent = tk.Tk()
        parent.withdraw()

    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("560x520+200+150")
    top.transient(parent)
    top.grab_set()

    container = ttk.Frame(top)
    container.pack(fill="both", expand=True, padx=8, pady=8)

    # Don't preload files for session-picker — user only picks a date
    bt = _BucketTree(container, expand_files=False)
    yscroll = ttk.Scrollbar(container, orient="vertical", command=bt.tree.yview)
    bt.tree.configure(yscrollcommand=yscroll.set)
    bt.tree.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    btn_frame = ttk.Frame(top)
    btn_frame.pack(fill="x", padx=8, pady=(0, 8))

    state = {"selected": None}

    def on_ok():
        sel = bt.tree.focus()
        if not sel:
            return
        kind, payload = _split_iid(sel)
        if kind != _K_DATE:
            return  # not a date node — ignore
        state["selected"] = payload  # "device_id|date"
        top.destroy()

    def on_cancel():
        top.destroy()

    ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="right", padx=4)
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=4)
    ttk.Label(btn_frame, text="Pick a date directory and click OK.", foreground="#666").pack(side="left")

    top.wait_window()
    if owns_root:
        try:
            parent.destroy()
        except tk.TclError:
            pass

    if not state["selected"]:
        return None

    device_id, date = state["selected"].split("|", 1)
    # Use download_session() so we honor whatever cache-path translation
    # storage_core does (dotted-date dirs, .csv.gz -> .csv, etc.) — building
    # the path here would diverge from where files actually land.
    listing = _sc.list_session(device_id, date)
    total = len(listing.all_keys())

    def action(progress):
        return _sc.download_session(device_id, date, progress=progress)

    session_dir = _run_with_progress(parent if not owns_root else None, total, action)
    return str(session_dir)


# ---------- pick_files ----------

def pick_files(
    parent: Optional[tk.Misc] = None,
    *,
    title: str = "Select files",
    extensions: Optional[Sequence[str]] = None,
) -> tuple[str, ...]:
    """Show a tree dialog with files at the leaves. User multi-selects files.
    Downloads picked files to cache and returns the list of local paths.

    Drop-in replacement for filedialog.askopenfilenames(). Returns () if cancelled.

    extensions: if given, only files whose names end with one of these are selectable.
                Filtering is informational here (everything is shown), but selections
                are filtered before download.
    """
    owns_root = parent is None
    if owns_root:
        parent = tk.Tk()
        parent.withdraw()

    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("620x560+200+150")
    top.transient(parent)
    top.grab_set()

    container = ttk.Frame(top)
    container.pack(fill="both", expand=True, padx=8, pady=8)

    bt = _BucketTree(container, expand_files=True)
    bt.tree.configure(selectmode="extended")
    yscroll = ttk.Scrollbar(container, orient="vertical", command=bt.tree.yview)
    bt.tree.configure(yscrollcommand=yscroll.set)
    bt.tree.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    btn_frame = ttk.Frame(top)
    btn_frame.pack(fill="x", padx=8, pady=(0, 8))

    state = {"keys": []}

    def on_ok():
        keys: list[str] = []
        for iid in bt.tree.selection():
            kind, payload = _split_iid(iid)
            if kind != _K_FILE:
                continue
            if extensions and not any(payload.lower().endswith(ext.lower()) for ext in extensions):
                continue
            keys.append(payload)
        state["keys"] = keys
        top.destroy()

    def on_cancel():
        state["keys"] = []
        top.destroy()

    ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="right", padx=4)
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=4)
    hint = "Ctrl-click for multi-select. Pick files (leaves), not folders."
    ttk.Label(btn_frame, text=hint, foreground="#666").pack(side="left")

    top.wait_window()
    if owns_root:
        try:
            parent.destroy()
        except tk.TclError:
            pass

    keys = state["keys"]
    if not keys:
        return ()

    def action(progress):
        return _sc.download_files(keys, progress=progress)

    paths = _run_with_progress(parent if not owns_root else None, len(keys), action)
    return tuple(str(p) for p in paths)


# ---------- pick_sessions (3-pane: devices | sessions | files) ----------

def _short_device_id(device_id: str) -> str:
    """'10-00000042' -> '10-42', '10-0000000a' -> '10-0a'.

    Strip leading zeros from the ID portion. If the result is shorter than
    2 chars (e.g. a single trailing 'a'), keep the last 2 chars instead so
    abbreviations remain unambiguous when discussed verbally.
    """
    if "-" not in device_id:
        return device_id
    type_part, id_part = device_id.split("-", 1)
    stripped = id_part.lstrip("0")
    if len(stripped) < 2:
        stripped = id_part[-2:]
    return f"{type_part}-{stripped}"


def _device_matches(device_id: str, query: str) -> bool:
    """Case-insensitive substring match against either the full id or the
    short form (see _short_device_id). Lets users type any of the natural
    abbreviations they use verbally: '42', '10-42', '00042', '0a', '10-0a'."""
    if not query:
        return True
    q = query.lower()
    return q in device_id.lower() or q in _short_device_id(device_id).lower()


def pick_sessions(
    parent: Optional[tk.Misc] = None,
    *,
    title: str = "Select sessions",
    same_device: bool = True,
    download: bool = False,
) -> tuple[str, ...]:
    """3-pane session picker (devices | sessions | files-in-session).

    Designed for the calibration submitter workflow: the user knows the
    device serial they care about and wants to find it fast, then pick
    one or more sessions for that device. Files pane is informational so
    the user can sanity-check what's actually in a session before submitting.

    Search is forgiving of the abbreviations we use verbally — type '42' or
    '10-42' or '0a' to filter the device list (see _device_matches).

    Returns:
        Tuple of local session-dir paths (strings). Empty tuple on cancel.

    Args:
        parent: optional Tk root/Toplevel. If None, the picker spins up its
            own hidden root for standalone use.
        title: window title.
        same_device: defensive — should always be true given the UI only
            allows one device at a time, but kept as a kwarg for API symmetry.
        download: if True, download each picked session into the cache
            before returning (with a progress dialog). Default False —
            the submitter only needs the directory shape; the daemon
            downloads on its end.
    """
    from tkinter import messagebox

    owns_root = parent is None
    if owns_root:
        parent = tk.Tk()
        parent.withdraw()

    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("1100x600+150+120")
    top.transient(parent)
    top.grab_set()

    state: dict = {
        "all_devices": [],
        "type_filter": "All",
        "search_text": "",
        "filtered_devices": [],
        "selected_device": None,
        "session_listings_cache": {},  # (device_id, iso) -> SessionListing
        "result_picks": [],            # list[(device_id, iso)]
    }

    # --- top: type filter + search ---
    top_bar = ttk.Frame(top, padding=(10, 10, 10, 4))
    top_bar.pack(fill="x")

    ttk.Label(top_bar, text="Type:").pack(side="left")
    type_var = tk.StringVar(value="All")
    type_radios_frame = ttk.Frame(top_bar)
    type_radios_frame.pack(side="left", padx=(6, 16))

    ttk.Label(top_bar, text="Search:").pack(side="left")
    search_var = tk.StringVar()
    search_entry = ttk.Entry(top_bar, textvariable=search_var, width=24)
    search_entry.pack(side="left", padx=(6, 0))
    ttk.Label(
        top_bar,
        text="(matches '42', '10-42', '0a', etc.)",
        foreground="#888",
    ).pack(side="left", padx=(8, 0))

    # --- middle: 3 panes ---
    panes = ttk.PanedWindow(top, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=10, pady=(4, 4))

    dev_frame = ttk.LabelFrame(panes, text="Devices")
    dev_listbox = tk.Listbox(dev_frame, exportselection=False, activestyle="dotbox")
    dev_scroll = ttk.Scrollbar(dev_frame, command=dev_listbox.yview)
    dev_listbox.config(yscrollcommand=dev_scroll.set)
    dev_listbox.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
    dev_scroll.pack(side="right", fill="y", pady=4)
    panes.add(dev_frame, weight=2)

    sess_frame = ttk.LabelFrame(panes, text="Sessions")
    sess_tree = ttk.Treeview(
        sess_frame, columns=("counts",), selectmode="extended", show="tree headings",
    )
    sess_tree.heading("#0", text="Date")
    sess_tree.heading("counts", text="train / test")
    sess_tree.column("#0", width=140, anchor="w")
    sess_tree.column("counts", width=110, anchor="center")
    sess_scroll = ttk.Scrollbar(sess_frame, command=sess_tree.yview)
    sess_tree.config(yscrollcommand=sess_scroll.set)
    sess_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
    sess_scroll.pack(side="right", fill="y", pady=4)
    panes.add(sess_frame, weight=2)

    files_frame = ttk.LabelFrame(panes, text="Files in highlighted session")
    files_tree = ttk.Treeview(
        files_frame, columns=("kind",), selectmode="none", show="tree headings",
    )
    files_tree.heading("#0", text="Filename")
    files_tree.heading("kind", text="Kind")
    files_tree.column("#0", width=240, anchor="w")
    files_tree.column("kind", width=70, anchor="center")
    files_scroll = ttk.Scrollbar(files_frame, command=files_tree.yview)
    files_tree.config(yscrollcommand=files_scroll.set)
    files_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
    files_scroll.pack(side="right", fill="y", pady=4)
    panes.add(files_frame, weight=3)

    # --- bottom: status + buttons ---
    btn_frame = ttk.Frame(top, padding=(10, 4, 10, 10))
    btn_frame.pack(fill="x")
    status_var = tk.StringVar(value="Loading devices...")
    ttk.Label(btn_frame, textvariable=status_var, foreground="#666").pack(side="left")

    def _close():
        if top.winfo_exists():
            top.destroy()

    cancel_btn = ttk.Button(btn_frame, text="Cancel", command=_close)
    ok_btn = ttk.Button(btn_frame, text="OK")
    cancel_btn.pack(side="right", padx=4)
    ok_btn.pack(side="right", padx=4)

    # --- logic ---

    def refresh_devices():
        dev_listbox.delete(0, "end")
        type_filter = state["type_filter"]
        q = state["search_text"]
        filtered = []
        for dev in state["all_devices"]:
            if type_filter != "All" and not dev.startswith(f"{type_filter}-"):
                continue
            if not _device_matches(dev, q):
                continue
            filtered.append(dev)
        state["filtered_devices"] = filtered
        for dev in filtered:
            short = _short_device_id(dev)
            label = f"{dev}    ({short})" if short != dev else dev
            dev_listbox.insert("end", label)
        if filtered:
            status_var.set(f"{len(filtered)} device(s) match.")
        else:
            status_var.set("No devices match. Adjust the type filter or search.")

    def on_search_change(*_):
        state["search_text"] = search_var.get().strip()
        refresh_devices()

    def on_type_change(*_):
        state["type_filter"] = type_var.get()
        refresh_devices()

    search_var.trace_add("write", on_search_change)
    type_var.trace_add("write", on_type_change)

    def on_device_selected(_evt=None):
        sel = dev_listbox.curselection()
        if not sel:
            return
        device_id = state["filtered_devices"][sel[0]]
        if state["selected_device"] == device_id:
            return
        state["selected_device"] = device_id
        sess_tree.delete(*sess_tree.get_children())
        files_tree.delete(*files_tree.get_children())
        status_var.set(f"Loading sessions for {device_id}...")
        threading.Thread(
            target=_work_load_sessions, args=(device_id,), daemon=True,
        ).start()

    dev_listbox.bind("<<ListboxSelect>>", on_device_selected)

    def _work_load_sessions(device_id: str):
        try:
            dates = [d for d in _sc.list_dates(device_id) if d != "models"]
            top.after(0, lambda: _populate_sessions(device_id, dates))
        except Exception as e:
            top.after(0, lambda: status_var.set(f"Error loading sessions: {e}"))

    def _populate_sessions(device_id: str, dates: list[str]):
        # Most-recent first so the common case (just-calibrated device) is
        # at the top.
        dates_sorted = sorted(dates, reverse=True)
        for d in dates_sorted:
            sess_tree.insert("", "end", iid=d, text=d, values=("loading...",))
        if dates_sorted:
            sess_tree.selection_set(dates_sorted[0])
            sess_tree.focus(dates_sorted[0])
            _on_session_focus(dates_sorted[0])
            status_var.set(f"{len(dates_sorted)} session(s) for {device_id}.")
        else:
            status_var.set(f"No sessions for {device_id}.")
        threading.Thread(
            target=_work_load_session_counts,
            args=(device_id, dates_sorted),
            daemon=True,
        ).start()

    def _work_load_session_counts(device_id: str, dates: list[str]):
        for d in dates:
            try:
                listing = _sc.list_session(device_id, d)
                state["session_listings_cache"][(device_id, d)] = listing
                counts = f"{len(listing.train)} / {len(listing.test)}"
                top.after(0, lambda i=d, c=counts: sess_tree.set(i, "counts", c))
            except Exception:
                top.after(0, lambda i=d: sess_tree.set(i, "counts", "?"))

    def _on_session_focus(date: str):
        device_id = state["selected_device"]
        if not device_id:
            return
        files_tree.delete(*files_tree.get_children())
        cache_key = (device_id, date)
        cached = state["session_listings_cache"].get(cache_key)
        if cached is not None:
            _populate_files(cached)
            return
        files_tree.insert("", "end", text="loading...")

        def work():
            try:
                listing = _sc.list_session(device_id, date)
                state["session_listings_cache"][cache_key] = listing
                top.after(0, lambda l=listing: _populate_files(l))
            except Exception as e:
                msg = f"err: {e}"
                top.after(0, lambda: (
                    files_tree.delete(*files_tree.get_children()),
                    files_tree.insert("", "end", text=msg),
                ))

        threading.Thread(target=work, daemon=True).start()

    def _populate_files(listing):
        files_tree.delete(*files_tree.get_children())

        def _basename(key: str) -> str:
            name = key.rsplit("/", 1)[-1]
            return name[:-3] if name.endswith(".csv.gz") else name

        for k in listing.train:
            files_tree.insert("", "end", text=_basename(k), values=("train",))
        for k in listing.test:
            files_tree.insert("", "end", text=_basename(k), values=("test",))
        if listing.tests_txt:
            files_tree.insert("", "end", text="tests.txt", values=("misc",))

    def on_session_select(_evt):
        focused = sess_tree.focus()
        if focused:
            _on_session_focus(focused)

    sess_tree.bind("<<TreeviewSelect>>", on_session_select)

    def on_ok():
        device_id = state["selected_device"]
        if not device_id:
            messagebox.showwarning("No device", "Pick a device first.", parent=top)
            return
        sel = sess_tree.selection()
        if not sel:
            messagebox.showwarning(
                "No sessions", "Pick at least one session.", parent=top,
            )
            return
        state["result_picks"] = [(device_id, iid) for iid in sel]
        _close()

    ok_btn.config(command=on_ok)
    top.protocol("WM_DELETE_WINDOW", _close)

    # --- initial load: types + devices ---

    def _work_initial_load():
        try:
            types = _sc.list_device_types()
            all_devices: list[str] = []
            for t in types:
                all_devices.extend(_sc.list_devices(t))
            top.after(0, lambda: _populate_initial(types, sorted(all_devices)))
        except Exception as e:
            top.after(0, lambda: status_var.set(f"Error loading bucket: {e}"))

    def _populate_initial(types: list[str], devices: list[str]):
        ttk.Radiobutton(
            type_radios_frame, text="All", variable=type_var, value="All",
        ).pack(side="left")
        for t in types:
            ttk.Radiobutton(
                type_radios_frame, text=t, variable=type_var, value=t,
            ).pack(side="left", padx=2)
        state["all_devices"] = devices
        refresh_devices()
        search_entry.focus_set()

    threading.Thread(target=_work_initial_load, daemon=True).start()

    top.wait_window()
    if owns_root:
        try:
            parent.destroy()
        except tk.TclError:
            pass

    picks = state["result_picks"]
    if not picks:
        return ()

    if same_device and len({d for d, _ in picks}) > 1:
        # Defensive — UI only allows one device, but if that ever changes...
        return ()

    if not download:
        return tuple(str(_sc.local_session_dir(d, dt)) for d, dt in picks)

    # Download path (with progress dialog) — preserved for callers that pass
    # download=True.
    listings = [_sc.list_session(d, dt) for d, dt in picks]
    total = sum(len(l.all_keys()) for l in listings)

    def action(progress):
        results = []
        cumulative = 0
        for (device_id, date), listing in zip(picks, listings):
            n_keys = len(listing.all_keys())

            def session_progress(idx, _local_total, key, base=cumulative, _total=total):
                progress(base + idx, _total, key)

            results.append(
                _sc.download_session(device_id, date, progress=session_progress)
            )
            cumulative += n_keys
        progress(total, total, None)
        return results

    sessions = _run_with_progress(parent if not owns_root else None, total, action)
    return tuple(str(p) for p in sessions)


__all__ = ["pick_session", "pick_sessions", "pick_files"]
