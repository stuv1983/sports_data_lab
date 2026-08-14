#!/usr/bin/env python3
"""Regression tests for the AFL Tables page-to-CSV scraper.

The parsers are exercised on miniature copies of the two page shapes,
because the property that matters is the *format handed to the loader*:
cells as text, non-breaking spaces cleared, and a blank spacer row after
every table -- the boundary the loader's parsers read between one match
(or one club's statistics) and the next.
"""

from __future__ import annotations

import pytest

from utils.afl import get_afl_results as G


SCORES_PAGE = """
<html><body>
<table width="100%"><tr><td>layout chrome that must not be read</td></tr></table>
<table border="1" width="100%">
  <tr><th>Richmond</th><td>3.2&nbsp;5.4</td><td>34</td></tr>
  <tr><th>Carlton</th><td>2.1&nbsp;4.3</td><td>27</td></tr>
</table>
<table border="1" width="100%">
  <tr><th>Geelong</th><td>10.10</td><td>70</td></tr>
</table>
</body></html>
"""

STATS_PAGE = """
<html><body>
<table class="sortable">
  <tr><th>#</th><th>Player</th><th>K</th></tr>
  <tr><td>4</td><td>Dustin&nbsp;Martin</td><td>22</td></tr>
</table>
<table class="sortable">
  <tr><th>#</th><th>Player</th><th>K</th></tr>
  <tr><td>35</td><td>Patrick Cripps</td><td>30</td></tr>
</table>
</body></html>
"""

STATS_PAGE_ID_ONLY = """
<html><body>
<table id="sortableTable0">
  <tr><th>Player</th></tr>
  <tr><td>Nick Daicos</td></tr>
</table>
</body></html>
"""


def test_each_match_table_is_read_and_the_layout_chrome_is_not():
    rows = G.match_rows(SCORES_PAGE)
    assert ["Richmond", "3.2 5.4", "34"] in rows
    assert ["Geelong", "10.10", "70"] in rows
    assert not any("layout" in cell for row in rows for cell in row)


def test_a_blank_spacer_row_separates_the_matches():
    """The loader reads a blank line as the boundary between matches."""
    rows = G.match_rows(SCORES_PAGE)
    carlton = rows.index(["Carlton", "2.1 4.3", "27"])
    assert rows[carlton + 1] == []


def test_non_breaking_spaces_become_ordinary_ones():
    """AFL Tables pads with \\xa0, which silently defeats every later
    comparison against a typed name -- the reason names.py exists."""
    rows = G.stats_rows(STATS_PAGE)
    assert ["4", "Dustin Martin", "22"] in rows


def test_both_clubs_statistics_are_kept_with_the_spacer_between():
    rows = G.stats_rows(STATS_PAGE)
    martin = rows.index(["4", "Dustin Martin", "22"])
    assert rows[martin + 1] == []
    assert ["35", "Patrick Cripps", "30"] in rows


def test_a_stats_table_marked_only_by_its_id_is_still_found():
    rows = G.stats_rows(STATS_PAGE_ID_ONLY)
    assert ["Nick Daicos"] in rows


def test_a_page_with_no_recognisable_tables_is_refused_with_a_reason():
    """A wrong URL must say what is missing, not save an empty CSV for
    the loader to reject later with a less helpful message."""
    with pytest.raises(G.ScrapeError):
        G.match_rows("<html><body><p>404</p></body></html>")
    with pytest.raises(G.ScrapeError):
        G.stats_rows(SCORES_PAGE)


def test_saved_csv_round_trips_the_rows(tmp_path):
    path = tmp_path / "round.csv"
    G.save_rows(G.match_rows(SCORES_PAGE), path)
    text = path.read_text(encoding="utf-8")
    assert "Richmond,3.2 5.4,34" in text
    # The spacer rows survive the write: one blank line per table.
    assert text.count("\r\n\r\n") + text.count("\n\n") >= 2
