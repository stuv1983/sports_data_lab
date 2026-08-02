#!/usr/bin/env python3
"""
gridley.py -- Query the local AFL database for players matching Gridley
grid constraints, ranked by how obscure they are.

Run build_db.py first to create gridley.db.

EXAMPLES
--------
Players who kicked 3+ goals AND had 30+ disposals in the SAME game:
    python gridley.py --min-goals 3 --min-disposals 30

...but only in the 1990s, rarest first:
    python gridley.py --min-goals 3 --min-disposals 30 --from 1990 --to 1999

Players who turned out for both Fitzroy and Richmond:
    python gridley.py --club Fitzroy --club Richmond

Obscure Hawthorn players who kicked 5 in a game:
    python gridley.py --club Hawthorn --min-goals 5 --max-career-games 50

A one-club player with under 20 career games who played a final:
    python gridley.py --one-club --max-career-games 20 --min-finals 1

Look up everything about a specific player:
    python gridley.py --player "Peter Bosustow"

List the club names the database understands:
    python gridley.py --clubs
"""

import argparse
import sqlite3
import sys

from data_paths import default_db

SINGLE_GAME_STATS = [
    "disposals", "kicks", "handballs", "marks", "goals", "behinds",
    "tackles", "hitouts", "inside50s", "clearances", "rebounds",
    "contested", "contested_marks", "marks_i50", "one_percenters",
    "bounces", "goal_assists", "frees_for", "frees_against", "brownlow",
]


def connect(db):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        sys.exit(f"Cannot open {db}. Run:  python build_db.py")
    con.row_factory = sqlite3.Row
    return con


def build_query(a):
    """Compose the SQL. Single-game stat thresholds all apply to ONE game."""
    where, params = [], []

    for stat in SINGLE_GAME_STATS:
        v = getattr(a, f"min_{stat}", None)
        if v is not None:
            where.append(f"g.{stat} >= ?")
            params.append(v)

    if a.__dict__.get("from_season") is not None:
        where.append("g.season >= ?")
        params.append(a.from_season)
    if a.to_season is not None:
        where.append("g.season <= ?")
        params.append(a.to_season)
    if a.venue:
        where.append("LOWER(g.venue) LIKE ?")
        params.append(f"%{a.venue.lower()}%")
    if a.opponent:
        where.append("LOWER(g.opponent) LIKE ?")
        params.append(f"%{a.opponent.lower()}%")
    if a.finals_only:
        where.append("g.is_final = 1")

    # A club given alongside single-game stats means: achieved it FOR that club.
    stat_filters_present = bool(where)
    if a.club and len(a.club) == 1 and stat_filters_present:
        where.append("(LOWER(g.club_now) LIKE ? OR LOWER(g.club_hist) LIKE ?)")
        params += [f"%{a.club[0].lower()}%"] * 2

    game_where = " AND ".join(where) if where else "1=1"

    sql = f"""
        SELECT p.player_id, p.player, p.debut_season, p.final_season,
               p.career_games, p.career_goals, p.career_brownlow,
               p.finals_played, p.clubs_hist, p.clubs_now, p.n_clubs,
               p.obscurity,
               q.n_qualifying, q.eg_season, q.eg_round, q.eg_club,
               q.eg_opp, q.eg_disposals, q.eg_goals
        FROM players p
        JOIN (
            SELECT player_id, n_qualifying, eg_season, eg_round, eg_club,
                   eg_opp, eg_disposals, eg_goals
            FROM (
                SELECT g.player_id,
                       COUNT(*) OVER (PARTITION BY g.player_id) AS n_qualifying,
                       g.season AS eg_season, g.round AS eg_round,
                       g.club_hist AS eg_club, g.opponent AS eg_opp,
                       g.disposals AS eg_disposals, g.goals AS eg_goals,
                       ROW_NUMBER() OVER (
                           PARTITION BY g.player_id
                           ORDER BY g.season, g.date
                       ) AS rn
                FROM games g
                WHERE {game_where}
            ) WHERE rn = 1
        ) q ON q.player_id = p.player_id
        WHERE 1=1
    """

    # Career-level filters.
    if a.max_career_games is not None:
        sql += " AND p.career_games <= ?"
        params.append(a.max_career_games)
    if a.min_career_games is not None:
        sql += " AND p.career_games >= ?"
        params.append(a.min_career_games)
    if a.min_career_goals is not None:
        sql += " AND p.career_goals >= ?"
        params.append(a.min_career_goals)
    if a.min_finals is not None:
        sql += " AND p.finals_played >= ?"
        params.append(a.min_finals)
    if a.one_club:
        sql += " AND p.n_clubs = 1"
    if a.min_clubs is not None:
        sql += " AND p.n_clubs >= ?"
        params.append(a.min_clubs)
    if a.debut_from is not None:
        sql += " AND p.debut_season >= ?"
        params.append(a.debut_from)
    if a.debut_to is not None:
        sql += " AND p.debut_season <= ?"
        params.append(a.debut_to)

    # Multiple clubs = played for ALL of them across the career.
    if a.club and (len(a.club) > 1 or not stat_filters_present):
        for c in a.club:
            sql += """ AND EXISTS (SELECT 1 FROM games x WHERE x.player_id = p.player_id
                        AND (LOWER(x.club_now) LIKE ? OR LOWER(x.club_hist) LIKE ?))"""
            params += [f"%{c.lower()}%"] * 2

    order = {
        "obscurity": "p.obscurity DESC, p.career_games ASC",
        "games": "p.career_games ASC, p.obscurity DESC",
        "oldest": "p.final_season ASC",
        "newest": "p.final_season DESC",
        "name": "p.player ASC",
    }[a.sort]
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(a.limit)
    return sql, params


def show_player(con, name):
    rows = con.execute(
        "SELECT * FROM players WHERE LOWER(player) LIKE ? ORDER BY career_games DESC",
        (f"%{name.lower()}%",)).fetchall()
    if not rows:
        print(f"No player matching '{name}'.")
        return
    for r in rows[:6]:
        print(f"\n{r['player']}  ({r['debut_season']}-{r['final_season']})")
        print(f"  Clubs         : {r['clubs_hist'].replace('|', ', ')}")
        print(f"  Career games  : {r['career_games']}   Finals: {r['finals_played']}")
        print(f"  Career goals  : {fmt(r['career_goals'])}   "
              f"Brownlow votes: {fmt(r['career_brownlow'])}")
        print(f"  Best game     : {fmt(r['best_disposals'])} disposals, "
              f"{fmt(r['best_goals'])} goals")
        print(f"  Obscurity     : {r['obscurity']}/100")


def fmt(v):
    if v is None:
        return "-"
    return str(int(v)) if float(v).is_integer() else str(v)


def main():
    ap = argparse.ArgumentParser(
        description="Find obscure AFL players matching Gridley constraints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    ap.add_argument("--db", default=default_db("afl"))
    ap.add_argument("--player", help="Look up one player instead of searching")
    ap.add_argument("--clubs", action="store_true", help="List club names")

    for stat in SINGLE_GAME_STATS:
        ap.add_argument(f"--min-{stat.replace('_', '-')}", type=float,
                        dest=f"min_{stat}",
                        help=f"{stat} in a single game")

    ap.add_argument("--club", action="append",
                    help="Club (repeat for 'played for both')")
    ap.add_argument("--opponent", help="Opponent in the qualifying game")
    ap.add_argument("--venue", help="Venue substring, e.g. Windy Hill")
    ap.add_argument("--from", dest="from_season", type=int, help="Season >=")
    ap.add_argument("--to", dest="to_season", type=int, help="Season <=")
    ap.add_argument("--finals-only", action="store_true")
    ap.add_argument("--max-career-games", type=int)
    ap.add_argument("--min-career-games", type=int)
    ap.add_argument("--min-career-goals", type=int)
    ap.add_argument("--min-finals", type=int)
    ap.add_argument("--one-club", action="store_true")
    ap.add_argument("--min-clubs", type=int)
    ap.add_argument("--debut-from", type=int)
    ap.add_argument("--debut-to", type=int)
    ap.add_argument("--sort", default="obscurity",
                    choices=["obscurity", "games", "oldest", "newest", "name"])
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--sql", action="store_true", help="Print the SQL")

    a = ap.parse_args()
    con = connect(a.db)

    if a.clubs:
        for r in con.execute(
                "SELECT club_hist, COUNT(*) n, MIN(season) a, MAX(season) b "
                "FROM games GROUP BY club_hist ORDER BY club_hist"):
            print(f"  {r['club_hist']:<24} {r['a']}-{r['b']}  ({r['n']:,} games)")
        return

    if a.player:
        show_player(con, a.player)
        return

    sql, params = build_query(a)
    if a.sql:
        print(sql, "\n", params, "\n")

    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("No players match those constraints.")
        print("Tip: disposal/mark/tackle data only exists from 1965 onward.")
        return

    print(f"\n{len(rows)} match(es), most obscure first\n")
    hdr = (f"{'Player':<24}{'Career':<9}{'Gms':>4}{'Obsc':>6}  "
           f"{'Clubs':<28}{'Example':<22}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        career = f"{r['debut_season']}-{r['final_season']}"
        clubs = r["clubs_hist"].replace("|", ", ")
        clubs = clubs[:26] + ".." if len(clubs) > 28 else clubs
        eg = (f"{r['eg_season']} R{r['eg_round']} v {r['eg_opp'][:10]}"
              if r["eg_season"] else "")
        print(f"{r['player'][:23]:<24}{career:<9}{r['career_games']:>4}"
              f"{r['obscurity']:>6.0f}  {clubs:<28}{eg:<22}")

    print(f"\nObscurity is a fame proxy (0-100, higher = less likely picked): "
          f"career games, Brownlow votes,\ncareer goals, finals played and era. "
          f"It is not Gridley's actual crowd pick data.")


if __name__ == "__main__":
    main()
