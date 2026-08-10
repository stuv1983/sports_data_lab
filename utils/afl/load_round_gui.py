#!/usr/bin/env python3
"""Browse to a round's CSVs and load them, without typing a path.

    python -m utils.afl.load_round_gui

The CSVs are written by hand and live wherever suits at the time -- a desktop
folder, a OneDrive share, a memory stick -- so the path is a thing that
changes rather than a fixed location under ``data/``. This is a browse window
over ``utils/afl/load_round_csv.py``: pick the folder, check what it holds,
load it. The chosen folder is remembered, and the command line falls back to
the same setting, so choosing it here means ``--dir`` can be left off there.

Everything it knows about reading a round comes from the loader. This module
holds no parsing, no database work and no rules about the data: it collects
three answers, calls ``load_round_csv.load``, and shows what it printed. A fix
to the loader therefore fixes the window too, and the two cannot disagree
about what a valid round looks like.

The load takes about a minute -- deriving matches and rebuilding the ladder
both read the whole games table -- so it runs on a worker thread and streams
its output into the log pane. Tk is not thread-safe, so the thread only ever
puts text on a queue and the main loop drains it.
"""
from __future__ import annotations

import io
import queue
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.afl import load_round_csv as L        # noqa: E402

POLL_MS = 100


class _Tee(io.TextIOBase):
    """A file object that puts everything written to it on a queue."""

    def __init__(self, sink: queue.Queue):
        self.sink = sink

    def write(self, text):
        if text:
            self.sink.put(text)
        return len(text)

    def flush(self):
        return None


def describe(folder: Path) -> tuple[str, list[str], int | None, str | None]:
    """What the folder looks like, before any of it is loaded.

    Returns a human summary, the files that could be the round summary in the
    order they should be offered, and the season and round guessed from the
    first of them. Any problem is reported as the summary line rather than
    raised: this runs on every folder chosen, including the wrong one.

    A folder often holds more than one candidate -- last round's summary left
    behind, a copy saved under a test name -- so the one whose name agrees
    with the folder's round is offered first. Guessing the round from
    whichever file sorted first would quietly offer to load round 22 out of a
    folder called rd23.
    """
    if not folder.is_dir():
        return "that folder does not exist", [], None, None
    try:
        candidates = sorted(p for p in folder.glob("*.csv") if p.is_file())
    except OSError as error:
        return f"could not read the folder: {error}", [], None, None
    if not candidates:
        return "no .csv files in this folder", [], None, None

    summaries, games = [], 0
    for path in candidates:
        try:
            if L.parse_game_file(path) is None:
                summaries.append(path.name)
            else:
                games += 1
        except L.LoadError as error:
            return f"{path.name}: {error}", [], None, None

    wanted = L.guess_round(folder.name)
    summaries.sort(key=lambda name: (
        L.guess_round(name) != wanted or wanted is None,   # folder's round
        L.guess_round(name) is None,                       # any round at all
        name.lower()))

    season = round_name = None
    for name in summaries:
        try:
            fixtures = L.parse_round_summary(folder / name)
        except L.LoadError:
            continue
        season = int(fixtures[0].match_date[:4])
        round_name = L.guess_round(name) or wanted
        break

    text = f"{games} game file(s), {len(summaries)} other CSV(s)"
    if not games:
        text += " -- no match pages found here"
    return text, summaries, season, round_name or wanted


class RoundLoader:
    def __init__(self, root):
        from tkinter import StringVar, ttk

        self.root = root
        self.output: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title("Load an AFL round from CSV")
        root.minsize(760, 520)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self.folder = StringVar()
        self.summary = StringVar()
        self.season = StringVar()
        self.round_name = StringVar()
        self.status = StringVar(value="Choose the folder holding this "
                                      "round's CSVs.")

        form = ttk.Frame(root, padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.folder).grid(
            row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(form, text="Browse...", command=self.browse).grid(
            row=0, column=2)

        ttk.Label(form, text="Round summary").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.summary_box = ttk.Combobox(form, textvariable=self.summary,
                                        state="readonly")
        self.summary_box.grid(row=1, column=1, columnspan=2, sticky="ew",
                              padx=(8, 0), pady=(8, 0))

        numbers = ttk.Frame(form)
        numbers.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(numbers, text="Season").grid(row=0, column=0, sticky="w")
        ttk.Entry(numbers, textvariable=self.season, width=8).grid(
            row=0, column=1, padx=(8, 20))
        ttk.Label(numbers, text="Round").grid(row=0, column=2, sticky="w")
        ttk.Entry(numbers, textvariable=self.round_name, width=8).grid(
            row=0, column=3, padx=(8, 0))

        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.check_button = ttk.Button(buttons, text="Check (writes nothing)",
                                       command=lambda: self.start(True))
        self.check_button.grid(row=0, column=0)
        self.load_button = ttk.Button(buttons, text="Load into database",
                                      command=lambda: self.start(False))
        self.load_button.grid(row=0, column=1, padx=(8, 0))
        self.progress = ttk.Progressbar(buttons, mode="indeterminate",
                                        length=180)
        self.progress.grid(row=0, column=2, padx=(16, 0))

        log_frame = ttk.Frame(root, padding=(12, 0, 12, 12))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = self._make_log(log_frame)

        ttk.Label(root, textvariable=self.status, padding=(12, 0, 12, 10),
                  wraplength=740).grid(row=2, column=0, sticky="ew")

        start = L.remembered_dir()
        if start:
            self.folder.set(str(start))
            self.inspect(start)
        root.after(POLL_MS, self.drain)

    def _make_log(self, parent):
        from tkinter import Text, ttk

        text = Text(parent, wrap="word", height=18, state="disabled",
                    font=("Consolas", 9))
        text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=bar.set)
        return text

    # ------------------------------------------------------------------

    def browse(self):
        from tkinter import filedialog

        current = self.folder.get().strip()
        chosen = filedialog.askdirectory(
            title="Folder holding this round's CSV files",
            initialdir=current or str(Path.home()))
        if chosen:
            folder = Path(chosen)
            self.folder.set(str(folder))
            self.inspect(folder)

    def inspect(self, folder: Path):
        """Describe the folder and fill in whatever can be guessed from it."""
        self.say(f"\n{folder}")
        text, summaries, season, round_name = describe(folder)
        self.say(f"  {text}")

        self.summary_box.configure(values=summaries)
        self.summary.set(summaries[0] if summaries else "")
        if len(summaries) > 1:
            self.say(f"  using {summaries[0]} as the round summary; "
                     f"others here: {', '.join(summaries[1:])}")

        if season:
            self.season.set(str(season))
        if round_name:
            self.round_name.set(round_name)
        self.status.set("Check the folder, then load it." if summaries
                        else "No round summary found in this folder.")

    # ------------------------------------------------------------------

    def start(self, dry_run: bool):
        if self.worker and self.worker.is_alive():
            return
        folder = Path(self.folder.get().strip())
        if not folder.is_dir():
            self.status.set("That folder does not exist.")
            return
        season = self.season.get().strip()
        round_name = self.round_name.get().strip()
        if not season.isdigit():
            self.status.set("Season must be a year, e.g. 2026.")
            return
        if not round_name:
            self.status.set("Round is required, e.g. 23 or GF.")
            return

        self.check_button.state(["disabled"])
        self.load_button.state(["disabled"])
        self.progress.start(12)
        self.status.set("Checking..." if dry_run
                        else "Loading. This takes about a minute.")
        self.say("\n" + "-" * 68)

        summary = self.summary.get().strip() or None
        self.worker = threading.Thread(
            target=self.run, daemon=True,
            args=(folder, int(season), round_name, summary, dry_run))
        self.worker.start()

    def run(self, folder, season, round_name, summary, dry_run):
        """The loader, on a worker thread. Only touches the queue."""
        tee = _Tee(self.output)
        outcome = "done"
        try:
            with redirect_stdout(tee), redirect_stderr(tee):
                L.load(L.DEFAULT_DB, folder, season, round_name,
                       summary=summary, dry_run=dry_run)
        except L.LoadError as error:
            self.output.put(f"\nStopped: {error}\n")
            outcome = "refused"
        except Exception as error:                              # noqa: BLE001
            self.output.put(f"\nFailed: {error!r}\n")
            outcome = "failed"
        self.output.put(("__finished__", outcome, dry_run))

    def drain(self):
        """Move whatever the worker printed into the log pane."""
        try:
            while True:
                item = self.output.get_nowait()
                if isinstance(item, tuple):
                    self.finish(item[1], item[2])
                else:
                    self.say(item, newline=False)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self.drain)

    def finish(self, outcome, dry_run):
        self.progress.stop()
        self.check_button.state(["!disabled"])
        self.load_button.state(["!disabled"])
        if outcome == "refused":
            self.status.set("Nothing was written. Fix the CSVs and check "
                            "again.")
        elif outcome == "failed":
            self.status.set("The load failed. See the log above.")
        elif dry_run:
            self.status.set("Checks passed and nothing was written. "
                            "Load it when you are ready.")
        else:
            self.status.set("Loaded. Run recompute_obscurity if anyone "
                            "debuted -- the log says whether they did.")

    def say(self, text, newline=True):
        self.log.configure(state="normal")
        self.log.insert("end", text + ("\n" if newline else ""))
        self.log.see("end")
        self.log.configure(state="disabled")


def main(argv: list[str] | None = None) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter is not available in this Python. Use the command "
              "line: python -m utils.afl.load_round_csv --help",
              file=sys.stderr)
        return 1
    if not L.DEFAULT_DB.exists():
        print(f"no AFL database at {L.DEFAULT_DB}", file=sys.stderr)
        return 1
    root = tk.Tk()
    RoundLoader(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
