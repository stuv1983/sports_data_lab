"""The draft, as a board somebody can actually read.

The database holds every draft, trade and signing Draftguru records --
6,810 of them, 1981 to 2025 -- and until now the only way to reach any of
it was a grid square ("father-son selection") or an Advanced Search token.
Nothing showed a draft.

What makes a draft board worth reading is the context beside it, which is
why the notes are on the page rather than in a document. A 1986 board with
no West Australians in it looks broken until you know they were excluded
that year; two Fitzroy picks inside the first five in 1997 look like a
trade until you know about priority picks. `afl/data_notes.py` holds those
rules, dated to the drafts they governed, and the year picker pulls the
ones that apply.

Career games come from our own `players` table wherever a draft row is
linked to a player, because that is the number the rest of this app shows
and it is current to the last round loaded. Draftguru's own count is the
fallback for the 1,700-odd rows that never resolved to a player -- mostly
selections who did not play a senior game, where the count is zero from
either source.
"""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import streamlit as st

import components
import names

from . import recruitment

ANY = "Any"

#: Link statuses the rest of the codebase treats as resolved. Matching
#: awards_page.py: `from_draft` is a person the draft itself identified.
TRUSTED = ("from_draft", "unique", "resolved")

#: The join every query here needs: a draft row, the player it resolved to
#: where it resolved to one, and that player's career.
_LINKED = f"""
    FROM draft d
    LEFT JOIN person_links pl
           ON pl.dg_person_id = d.dg_person_id
          AND pl.match_status IN {TRUSTED}
    LEFT JOIN players pr ON pr.player_id = pl.player_id
"""

#: Career games, ours where we have them. See the module docstring.
_GAMES = "COALESCE(pr.career_games, d.games)"
_GOALS = "COALESCE(pr.career_goals, d.goals)"


def _revision(db):
    try:
        stat = os.stat(db)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


@st.cache_data(show_spinner=False)
def _options(revision, _con) -> dict:
    """Every value each filter can take, from the table itself.

    Read rather than listed, so a draft type or signing kind that appears
    in a later import offers itself without an edit here.
    """
    def column(sql):
        return [row[0] for row in _con.execute(sql) if row[0]]

    return {
        "years": column("SELECT DISTINCT draft_year FROM draft "
                        "WHERE draft_year IS NOT NULL ORDER BY draft_year DESC"),
        "types": column("SELECT draft_type, COUNT(*) FROM draft "
                        "WHERE draft_type IS NOT NULL AND TRIM(draft_type) <> '' "
                        "GROUP BY draft_type ORDER BY COUNT(*) DESC"),
        "clubs": column("SELECT DISTINCT club FROM draft "
                        "WHERE club IS NOT NULL AND TRIM(club) <> '' "
                        "ORDER BY club"),
        "signings": column("SELECT signing_kind, COUNT(*) FROM draft "
                           "WHERE signing_kind IS NOT NULL "
                           "AND TRIM(signing_kind) <> '' "
                           "GROUP BY signing_kind ORDER BY COUNT(*) DESC"),
        "sources": _sources(revision, _con)["Source"].tolist(),
    }


@st.cache_data(show_spinner=False)
def _sources(revision, _con) -> pd.DataFrame:
    """Every place a player was recruited through, and what came of them.

    A path is credited to all of it, not only its last step: a junior club
    that sent a player to a talent-league club had a hand in the career,
    and crediting only the final step would answer "which talent-league
    clubs exist" rather than "where do players come from".

    Counted per draft record rather than per player, because a player
    drafted twice -- rookie-listed, delisted, taken again -- came through
    the same junior club both times and it did produce him once. The
    games column deduplicates, so a twice-drafted player's career is not
    counted twice against his old club.
    """
    frame = pd.read_sql_query(
        f"""SELECT d.original_club AS path, d.dg_person_id AS person,
                   {_GAMES} AS games
            {_LINKED}
            WHERE d.original_club IS NOT NULL
              AND TRIM(d.original_club) <> ''""", _con)
    frame["Source"] = frame["path"].map(recruitment.path)
    frame = frame.explode("Source").dropna(subset=["Source"])

    careers = (frame.drop_duplicates(["Source", "person"])
                    .groupby("Source")["games"].agg(
                        Games="sum", Played=lambda s: int((s > 0).sum())))
    out = (frame.groupby("Source").size().rename("Selections").to_frame()
           .join(careers).reset_index())
    out["Kind"] = out["Source"].map(recruitment.kind)
    out = out[["Source", "Kind", "Selections", "Played", "Games"]]
    return out.sort_values(["Selections", "Source"],
                           ascending=[False, True]).reset_index(drop=True)


def _where(year, draft_type, club, signing, name, con,
           source=ANY) -> tuple[str, list]:
    """The filter clause and its parameters, shared by every query below."""
    where, params = ["1=1"], []
    if source != ANY:
        # A whole segment of the path, not a substring of it: "Geelong"
        # must not answer for "Geelong U18" and "Geelong College".
        where.append(recruitment.segment_match_sql("d.original_club"))
        params.append(source)
    if year != ANY:
        where.append("d.draft_year = ?")
        params.append(int(year))
    if draft_type != ANY:
        where.append("d.draft_type = ?")
        params.append(draft_type)
    if club != ANY:
        where.append("d.club = ?")
        params.append(club)
    if signing != ANY:
        where.append("d.signing_kind = ?")
        params.append(signing)
    if name and name.strip():
        # Letters alone, the rule every other name box on the site uses:
        # "o'brien" finds OBrien, an accent is no barrier, and nothing the
        # reader types becomes a LIKE wildcard.
        folded = names.search_key(name)
        if folded:
            con.create_function("search_key", 1, names.search_key,
                                deterministic=True)
            where.append("search_key(d.player) LIKE ?")
            params.append(f"%{folded}%")
        else:
            where.append("1=0")
    return " AND ".join(where), params


@st.cache_data(show_spinner=False, max_entries=120)
def _board(where, params, revision, _con) -> pd.DataFrame:
    """The selections themselves, in the order they were made."""
    return pd.read_sql_query(
        f"""SELECT d.draft_year AS Year, d.draft_type AS Draft,
                   d.pick AS Pick, d.pick_note AS "Pick note",
                   d.player AS Player, d.club AS Club,
                   d.signing AS Signing, d.original_club AS "Recruited from",
                   d.draft_age AS Age, d.height_cm AS "Height (cm)",
                   d.grade AS Grade, {_GAMES} AS Games, {_GOALS} AS Goals,
                   d.awards_text AS Awards, pl.player_id
            {_LINKED}
            WHERE {where}
            ORDER BY d.draft_year DESC, d.draft_type,
                     d.pick IS NULL, d.pick, d.player
            LIMIT 2000""",
        _con, params=params).pipe(_as_counts)


#: Careers, one row per person rather than per selection. 1,473 of the
#: 5,057 people in this table were drafted more than once -- rookie-listed,
#: delisted and taken again, or traded -- and counting a career once per
#: selection inflated every total that mattered: Josh Kennedy's 290 games
#: were counted twice, once for Hawthorn's 2006 draft and again for
#: Sydney's 2009 trade. `dg_person_id` is filled on every row, so it is a
#: complete key to group by.
_ONE_PER_PERSON = f"""
    SELECT d.dg_person_id AS person, MAX(COALESCE({_GAMES}, 0)) AS games
    {_LINKED} WHERE {{where}} GROUP BY d.dg_person_id
"""


@st.cache_data(show_spinner=False, max_entries=120)
def _summary(where, params, revision, _con) -> tuple:
    """(selections, clubs, players who played, games those players played).

    Selections are records because that is what a draft board lists; the
    other two are people, because a career belongs to a person however
    many times he was drafted.
    """
    records, clubs = _con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT d.club) {_LINKED} WHERE {where}",
        params).fetchone()
    played, games = _con.execute(
        f"""SELECT COUNT(*), SUM(games)
            FROM ({_ONE_PER_PERSON.format(where=where)})
            WHERE games > 0""", params).fetchone()
    return records, clubs, played, games


@st.cache_data(show_spinner=False, max_entries=60)
def _classes(where, params, revision, _con) -> pd.DataFrame:
    """One row per draft: how many were taken and what became of them.

    The question a draft board raises but cannot answer -- was that a good
    year? -- needs the careers, which is why this counts games rather than
    selections.
    """
    # Selections count records -- so the number agrees with the board when
    # the reader filters to that year -- while the career columns count
    # each person once, however many times that year drafted him.
    return pd.read_sql_query(
        f"""SELECT Year, SUM(records) AS Selections,
                   SUM(games > 0) AS "Played",
                   SUM(games) AS Games, SUM(goals) AS Goals,
                   MAX(games) AS "Most games"
            FROM (SELECT d.draft_year AS Year, COUNT(*) AS records,
                         MAX(COALESCE({_GAMES}, 0)) AS games,
                         MAX(COALESCE({_GOALS}, 0)) AS goals
                  {_LINKED}
                  WHERE {where}
                  GROUP BY d.draft_year, d.dg_person_id)
            GROUP BY Year
            ORDER BY Year DESC""",
        _con, params=params).pipe(_as_counts)


@st.cache_data(show_spinner=False, max_entries=60)
def _best(where, params, revision, _con) -> pd.DataFrame:
    """The careers this selection produced, longest first, one row each.

    One row per person: Josh Kennedy came through Xavier College once and
    was drafted twice, and listing him twice reads as two players.

    Which of his selections to show is chosen by `ROW_NUMBER`, not by
    `GROUP BY` with bare columns. SQLite only promises bare columns come
    from the matching row when a query holds exactly one min/max
    aggregate; with a MIN(year) and a MAX(games) together it picks freely,
    and it produced Kennedy as "2006 · Trade · Sydney" -- the year of the
    Hawthorn draft that took him and the club of the trade three years
    later, a selection that never happened.

    The row shown is the earliest, preferring the national draft where a
    year holds more than one, because that is how a player entered the
    competition. The career columns are the person's, taken across every
    selection.
    """
    return pd.read_sql_query(
        f"""SELECT Player, Year, Draft, Pick, Club, Games, Goals, Awards,
                   player_id
            FROM (
              SELECT d.player AS Player, d.draft_year AS Year,
                     d.draft_type AS Draft, d.pick AS Pick, d.club AS Club,
                     d.awards_text AS Awards, pl.player_id,
                     MAX(COALESCE({_GAMES}, 0)) OVER person AS Games,
                     MAX(COALESCE({_GOALS}, 0)) OVER person AS Goals,
                     ROW_NUMBER() OVER (
                         PARTITION BY d.dg_person_id
                         ORDER BY d.draft_year,
                                  CASE WHEN LOWER(d.draft_type)
                                            LIKE '%national%'
                                       THEN 0 ELSE 1 END,
                                  d.pick) AS rn
              {_LINKED}
              WHERE {where}
              WINDOW person AS (PARTITION BY d.dg_person_id)
            )
            WHERE rn = 1 AND Games > 0
            ORDER BY Games DESC, Player
            LIMIT 25""",
        _con, params=params).pipe(_as_counts)


#: Columns that are counts, and must read as counts. A free-agency row has
#: no pick number, and one NaN in the column turns every pick in it into a
#: float -- so pick 3 draws as "3.0". Nullable integers hold the blank
#: without dragging the rest into decimals.
_COUNT_COLUMNS = ("Pick", "Age", "Height (cm)", "Games", "Goals",
                  "Selections", "Played", "Most games", "Year")


def _as_counts(frame: pd.DataFrame) -> pd.DataFrame:
    for column in _COUNT_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(
                frame[column], errors="coerce").astype("Int64")
    return frame


def _drop_empty(frame: pd.DataFrame) -> pd.DataFrame:
    """Hide columns this selection never filled.

    A pre-season draft records no signing and a 1988 row no grade. A column
    of blanks reads as missing data rather than as a field that does not
    apply to what is on screen.
    """
    return frame[[c for c in frame.columns if frame[c].notna().any()]]


def _notes(sport, year) -> None:
    """The rules that governed the draft on screen."""
    notes = sport.notes()
    if notes is None:
        return
    dated = notes.for_draft_year(year) if year != ANY else []
    background = notes.draft_background()
    if not dated and not background:
        return

    if dated:
        for note in dated:
            st.caption(f":material/gavel: **{note.seasons}** — {note.text}")
    with st.expander("How the draft works" if year == ANY
                     else f"How the draft worked in {year}"):
        for note in dated:
            st.markdown(f"**{note.seasons}** — {note.text}")
        for note in background:
            st.markdown(f"- {note.text}")


def _recruiting_sources(sport, con, revision, selected) -> None:
    """Which places produce AFL players, over the whole draft era.

    Deliberately not narrowed by the filters above. The question this
    answers -- where do players come from -- is about the whole draft era,
    and re-cutting it by the year on screen would turn a table about
    Sandringham into a table about one Sandringham draftee. The filters
    have the board and the metrics; this has the long view.
    """
    sources = _sources(revision, con)
    if sources.empty:
        return

    heading = f"Where players are recruited from ({len(sources):,} places)"
    with st.expander(heading):
        st.caption(
            "Every step of every recruitment path Draftguru records, from "
            "the junior club to the talent-league or state-league club. A "
            f"player counts for each place he passed through, so the "
            f"{sport.vocab.games} column is the career of everyone who came "
            "through — not games played for that club."
        )
        kinds = [ANY, recruitment.CLUB, recruitment.TALENT_LEAGUE,
                 recruitment.SCHOOL]
        kind = st.radio(
            "Show", kinds, horizontal=True, key="dr_source_kind",
            format_func=lambda k: "Everywhere" if k == ANY else k,
            help="The sixteen talent-league clubs sit at the top of any "
                 "combined list because nearly every Victorian passes "
                 "through one. Split them out to see the junior clubs and "
                 "schools underneath.")
        shown = sources if kind == ANY else sources[sources["Kind"] == kind]
        rank = st.radio(
            "Rank by", ["Selections", "Games", "Played"], horizontal=True,
            key="dr_source_rank",
            format_func=lambda c: {
                "Selections": "Selections",
                "Games": f"{sport.vocab.games.capitalize()} produced",
                "Played": f"Played a senior {sport.vocab.game}"}[c])
        shown = shown.sort_values(
            [rank, "Source"], ascending=[False, True]).head(100)

        if selected != ANY:
            st.caption(f"The board above is filtered to **{selected}**. "
                       "This table is not — it covers every draft.")
        components.clickable_entity_table(
            shown, sport, con, key=sport.k("draft_sources"),
            column_config={
                "Games": st.column_config.ProgressColumn(
                    f"{sport.vocab.games.capitalize()} produced", format="%d",
                    min_value=0, max_value=int(shown["Games"].max() or 1)),
            })
        st.caption(
            "Pick a place in **Recruited from** above to see its selections."
        )


def draft_page(sport, con: sqlite3.Connection) -> None:
    """Every selection, with the rules that governed it."""
    st.markdown("# Draft")

    if not _has_table(con, "draft"):
        hint = sport.loader_hints.get("draft_available", "")
        st.info(f"Draft data is not loaded. {hint}")
        return

    revision = _revision(sport.db)
    options = _options(revision, con)
    if not options["years"]:
        st.info("The draft table is empty.")
        return

    first, last = min(options["years"]), max(options["years"])
    st.caption(
        f"Every selection, trade and signing recorded from {first} to "
        f"{last} — national, rookie, pre-season, mid-season and pre-draft, "
        "with what each player's career became."
    )

    f1, f2, f3 = st.columns(3)
    year = f1.selectbox("Draft year", [ANY, *options["years"]],
                        format_func=lambda y: "Every year" if y == ANY
                        else str(y), key="dr_year")
    draft_type = f2.selectbox("Type", [ANY, *options["types"]],
                              format_func=lambda t: "Every type" if t == ANY
                              else t, key="dr_type")
    club = f3.selectbox(sport.vocab.title_case("club"),
                        [ANY, *options["clubs"]],
                        format_func=lambda c: f"Every {sport.vocab.club}"
                        if c == ANY else c, key="dr_club")

    g1, g2, g3 = st.columns(3)
    signing = g1.selectbox(
        "Recruited under", [ANY, *options["signings"]],
        format_func=lambda s: "Any rule" if s == ANY else s, key="dr_signing",
        help="How the player arrived: the father-son rule, a club academy, "
             "a zone, free agency, an international signing.")
    source = g2.selectbox(
        "Recruited from", [ANY, *options["sources"]],
        format_func=lambda s: "Anywhere" if s == ANY else s, key="dr_source",
        help="Any step of the path a player took to the draft — the junior "
             "club, the school, the talent-league or state-league club. "
             "Ordered by how many selections came through each.")
    name = g3.text_input("Player contains", placeholder="surname…",
                         key="dr_name")

    _notes(sport, year)

    where, params = _where(year, draft_type, club, signing, name, con,
                           source=source)
    records, clubs, played, games = _summary(where, params, revision, con)
    if not records:
        st.info("No selections match those filters.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Selections", f"{records:,}")
    m2.metric(sport.vocab.title_case("clubs"), f"{clubs:,}")
    m3.metric(f"Played a senior {sport.vocab.game}",
              f"{played or 0:,}",
              delta=f"{(played or 0) / records:.0%} of selections",
              delta_color="off")
    m4.metric(f"{sport.vocab.games.capitalize()} played",
              f"{int(games or 0):,}")

    board = _board(where, params, revision, con)
    player_ids = board["player_id"].tolist()
    table = _drop_empty(board.drop(columns=["player_id"]))
    st.caption("Select a row to see that player's full career.")
    components.clickable_player_table(
        table, player_ids, sport, con, key=sport.k("draft_board"))
    if len(board) == 2000:
        st.caption("Showing the first 2,000 selections — narrow the "
                   "filters to see the rest.")
    st.download_button(
        "Download these selections as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=f"afl_draft_{year if year != ANY else 'all'}.csv",
        mime="text/csv", key="dr_download")

    unlinked = int(board["player_id"].isna().sum())
    if unlinked:
        st.caption(
            f"{unlinked:,} of {len(board):,} rows here could not be matched "
            f"to a player in this database, so they open no career. They "
            f"are listed because the selection was still made — most never "
            f"played a senior {sport.vocab.game}."
        )

    # -- what became of them ---------------------------------------------
    best = _best(where, params, revision, con)
    if not best.empty:
        with st.expander(f"Longest careers from this selection "
                         f"({len(best)})"):
            components.clickable_player_table(
                _drop_empty(best.drop(columns=["player_id"])),
                best["player_id"].tolist(), sport, con,
                key=sport.k("draft_best"))

    # -- where they came from --------------------------------------------
    _recruiting_sources(sport, con, revision, source)

    # -- year on year ----------------------------------------------------
    # Only where the reader has not already narrowed to one year: a single
    # row restating the metrics above is not a comparison.
    if year == ANY:
        classes = _classes(where, params, revision, con)
        if len(classes) > 1:
            with st.expander(f"Draft by draft ({len(classes)} years)"):
                st.caption(
                    f"How many were taken each year and what they went on "
                    f"to play. A recent draft has had less time to add "
                    f"{sport.vocab.games}, so the last few years are always "
                    f"low."
                )
                # 'Year' is already one of the columns a click opens as a
                # season, and a draft year is not a season -- but the
                # season it closed is exactly what a reader wants next.
                components.clickable_entity_table(
                    classes, sport, con, key=sport.k("draft_classes"),
                    column_config={
                        "Games": st.column_config.ProgressColumn(
                            "Games", format="%d",
                            min_value=0,
                            max_value=int(classes["Games"].max() or 1)),
                    })
