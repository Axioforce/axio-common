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
    """iid format: '<kind>:<payload>'. Payload encoding depends on kind."""
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


# ---------- pick_sessions (multi-select) ----------

def pick_sessions(
    parent: Optional[tk.Misc] = None,
    *,
    title: str = "Select sessions",
    same_device: bool = True,
    download: bool = False,
) -> tuple[str, ...]:
    """Multi-select version of pick_session(). Returns local session-directory
    paths for every date directory the user picked. Returns () if cancelled or
    nothing valid was selected.

    same_device: if True (default), OK is rejected unless all picks share the
                 same device — calibration training runs against one device
                 at a time.
    download:    if True, download each picked session into the cache before
                 returning (with a progress dialog). Default False — the
                 submitter only needs the directory shape and a file listing
                 to fill out a job config; the daemon handles the actual
                 download. Set True for picker-driven flows that need the
                 bytes locally (analysis scripts, etc.).

    Drop-in for the loop pattern around filedialog.askdirectory() that
    GUI_NeuralNet_API.edit_selected_dirs uses.
    """
    from tkinter import messagebox

    owns_root = parent is None
    if owns_root:
        parent = tk.Tk()
        parent.withdraw()

    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("560x560+200+150")
    top.transient(parent)
    top.grab_set()

    container = ttk.Frame(top)
    container.pack(fill="both", expand=True, padx=8, pady=8)

    bt = _BucketTree(container, expand_files=False)
    bt.tree.configure(selectmode="extended")
    yscroll = ttk.Scrollbar(container, orient="vertical", command=bt.tree.yview)
    bt.tree.configure(yscrollcommand=yscroll.set)
    bt.tree.pack(side="left", fill="both", expand=True)
    yscroll.pack(side="right", fill="y")

    btn_frame = ttk.Frame(top)
    btn_frame.pack(fill="x", padx=8, pady=(0, 8))

    state: dict = {"picks": []}  # list of (device_id, date)

    def on_ok():
        picks: list[tuple[str, str]] = []
        for iid in bt.tree.selection():
            kind, payload = _split_iid(iid)
            if kind != _K_DATE:
                continue
            device_id, date = payload.split("|", 1)
            picks.append((device_id, date))
        if not picks:
            messagebox.showwarning(
                "Nothing selected",
                "Pick one or more date directories (not files or folders above them).",
                parent=top,
            )
            return
        if same_device and len({d for d, _ in picks}) > 1:
            messagebox.showerror(
                "Multiple devices",
                "Selected sessions must all be from the same device.",
                parent=top,
            )
            return
        state["picks"] = picks
        top.destroy()

    def on_cancel():
        state["picks"] = []
        top.destroy()

    ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="right", padx=4)
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=4)
    hint = "Ctrl/Shift-click for multi-select. Pick date directories."
    ttk.Label(btn_frame, text=hint, foreground="#666").pack(side="left")

    top.wait_window()
    if owns_root:
        try:
            parent.destroy()
        except tk.TclError:
            pass

    picks = state["picks"]
    if not picks:
        return ()

    if not download:
        # No-I/O path: the submitter only needs the directory shape. Compute
        # local_session_dir for each pick and hand them back. The daemon
        # will fetch bytes when the job runs.
        return tuple(str(_sc.local_session_dir(d, dt)) for d, dt in picks)

    # Pre-list each session so we know the total file count for the progress
    # bar before we start downloading.
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
