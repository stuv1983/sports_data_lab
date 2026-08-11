#!/usr/bin/env python3
"""Fetch a round's AFL Tables pages and save them as the loader's CSVs.

    python -m utils.afl.get_afl_results

The manual round load (``utils/afl/load_round_csv.py``) reads a folder of
CSVs: one round summary and one player-statistics page per match. When a
round is missing upstream those pages still exist on AFL Tables, so this
window turns them into that folder without retyping anything: paste the
Season Scores URL for the round summary, paste a match page URL for its
player statistics, choose where to save, done.

Only the tables are read; nothing is interpreted. Which columns a game
file must hold, which totals must agree -- all of that is the loader's
business, checked when the folder is loaded, so the two cannot disagree
about what a valid round looks like. The window is deliberately dumb for
the same reason ``load_round_gui.py`` is: a fix to the loader must never
need a matching fix here.

Scraping is separated from the window so the parsers can be tested
without a network or a display: ``match_rows`` and ``stats_rows`` take
page HTML and return the CSV rows, and the GUI only fetches, calls them
and writes the file.
"""
from __future__ import annotations

import csv
from pathlib import Path

#: AFL Tables responds in well under this; a hung request should surface
#: as an error in the window, not a frozen window.
REQUEST_TIMEOUT = 10


class ScrapeError(RuntimeError):
    """The page was fetched but holds nothing this tool recognises."""


def fetch(url: str) -> str:
    """One page, as text. Network trouble surfaces as requests' errors."""
    import requests

    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _table_rows(tables) -> list[list[str]]:
    """Every table's rows as text cells, a blank spacer row after each.

    The spacer is part of the format, not decoration: the loader's parsers
    read a blank line as the boundary between one match (or one club's
    statistics) and the next. Cells are joined with spaces and cleared of
    the non-breaking spaces AFL Tables pads with, which otherwise defeat
    every later comparison against a typed name.
    """
    rows: list[list[str]] = []
    for table in tables:
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True).replace("\xa0", " ")
                     for cell in tr.find_all(("td", "th"))]
            if cells:
                rows.append(cells)
        rows.append([])
    return rows


def match_rows(html: str) -> list[list[str]]:
    """The Season Scores page's match tables, as round-summary CSV rows.

    AFL Tables draws each match of the round as its own bordered,
    full-width table, and those two attributes are what tell the match
    tables apart from the layout tables around them.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", attrs={"border": "1", "width": "100%"})
    if not tables:
        raise ScrapeError(
            "no match tables on this page -- is this the Season Scores "
            "page for the round?")
    return _table_rows(tables)


def stats_rows(html: str) -> list[list[str]]:
    """A match page's player-statistics tables, as game-file CSV rows.

    The statistics tables are the sortable ones -- usually marked with the
    ``sortable`` class, on some pages only with an id beginning
    ``sortableTable``, so the id is the fallback. A match page holds one
    per club, and the blank row between them is part of the format.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="sortable")
    if not tables:
        tables = soup.find_all(
            "table", id=lambda value: value and value.startswith(
                "sortableTable"))
    if not tables:
        raise ScrapeError(
            "no player-statistics tables on this page -- is this a match "
            "page?")
    return _table_rows(tables)


def save_rows(rows: list[list[str]], path: Path | str) -> None:
    """Write the rows as the loader's CSV."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)


class ResultsFetcher:
    """Two URL boxes and two save buttons; everything else is the parsers."""

    def __init__(self, root):
        from tkinter import ttk

        self.root = root
        root.title("Get AFL results")

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        self.scores_url = self._section(
            frame, 0, "Season Scores URL (round summary)",
            "Save the round summary", self.save_summary)
        self.stats_url = self._section(
            frame, 3, "Match page URL (player statistics)",
            "Save the player statistics", self.save_statistics)

        self.status = ttk.Label(frame, text="", wraplength=430)
        self.status.grid(row=6, column=0, sticky="we", pady=(12, 0))

    def _section(self, frame, row, label, button, command):
        from tkinter import ttk

        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
        entry = ttk.Entry(frame, width=56)
        entry.grid(row=row + 1, column=0, sticky="we", pady=(0, 4))
        ttk.Button(frame, text=button, command=command).grid(
            row=row + 2, column=0, sticky="w", pady=(0, 12))
        return entry

    def save_summary(self):
        self._save(self.scores_url, match_rows, "round summary")

    def save_statistics(self):
        self._save(self.stats_url, stats_rows, "player statistics")

    def _save(self, entry, parse, what):
        """Fetch, parse and write one page behind a save dialog.

        Fetched *before* the dialog: a wrong URL should fail while the
        mistake is still on screen, not after a filename was chosen.
        """
        from tkinter import filedialog, messagebox

        url = entry.get().strip()
        if not url:
            messagebox.showwarning(
                "No URL", f"Paste the URL for the {what} first.")
            return
        try:
            rows = parse(fetch(url))
        except ScrapeError as error:
            messagebox.showerror("Nothing recognised", str(error))
            return
        except Exception as error:                       # noqa: BLE001
            messagebox.showerror("Could not fetch the page", str(error))
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        try:
            save_rows(rows, path)
        except OSError as error:
            messagebox.showerror("Could not save", str(error))
            return
        self.status.config(
            text=f"Saved the {what} to {path}. The folder of saved CSVs "
                 "is what utils/afl/load_round_csv.py loads.")


def main() -> int:
    import tkinter as tk

    root = tk.Tk()
    ResultsFetcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
