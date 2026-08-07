import re

with open("query_filters.py", "r", encoding="utf-8") as f:
    content = f.read()

replacements = [
    # 1. played / season
    (
        'f"EXISTS (SELECT 1 FROM {s.games} yr "\\\n'
        '                f"WHERE yr.{s.player_id}=p.{s.player_id} "\\\n'
        '                f"AND yr.{s.season} BETWEEN ? AND ?)"',
        'f"p.{s.player_id} IN (SELECT yr.{s.player_id} FROM {s.games} yr "\\\n'
        '                f"WHERE yr.{s.season} BETWEEN ? AND ?)"'
    ),
    # 2. postseason
    (
        'f"EXISTS (SELECT 1 FROM {s.games} pg "\\\n'
        '                f"WHERE pg.{s.player_id}=p.{s.player_id} "\\\n'
        '                f"AND pg.{s.is_final}=1)"',
        'f"p.{s.player_id} IN (SELECT pg.{s.player_id} FROM {s.games} pg "\\\n'
        '                f"WHERE pg.{s.is_final}=1)"'
    ),
    # 3. captain boolean
    (
        '"NOT EXISTS (SELECT 1 FROM captaincies cp "\\\n'
        '                    "WHERE cp.player_id=p." + s.player_id + " "\\\n'
        '                    "AND cp.match_status IN (\'unique\',\'resolved\'))"',
        'f"p.{s.player_id} NOT IN (SELECT cp.player_id FROM captaincies cp "\\\n'
        '                    "WHERE cp.match_status IN (\'unique\',\'resolved\'))"'
    ),
    # 4. award
    (
        '"EXISTS (SELECT 1 FROM awards a JOIN person_links al "\\\n'
        '                "ON al.dg_person_id=a.dg_person_id "\\\n'
        '                f"WHERE al.player_id=p.{s.player_id} "\\\n'
        '                "AND al.match_status IN (\'from_draft\',\'unique\',\'resolved\') "\\\n'
        '                "AND a.award_slug=?)"',
        'f"p.{s.player_id} IN (SELECT al.player_id FROM awards a JOIN person_links al "\\\n'
        '                "ON al.dg_person_id=a.dg_person_id "\\\n'
        '                "WHERE al.match_status IN (\'from_draft\',\'unique\',\'resolved\') "\\\n'
        '                "AND a.award_slug=?)"'
    ),
    # 5. drafted_by
    (
        '"EXISTS (SELECT 1 FROM draft d JOIN draft_links dl "\\\n'
        '                "ON dl.draft_rowid=d.rowid "\\\n'
        '                f"WHERE dl.player_id=p.{s.player_id} "\\\n'
        '                "AND dl.match_status IN (\'unique\',\'resolved\') "\\\n'
        '                "AND LOWER(d.club) LIKE ?)"',
        'f"p.{s.player_id} IN (SELECT dl.player_id FROM draft d JOIN draft_links dl "\\\n'
        '                "ON dl.draft_rowid=d.rowid "\\\n'
        '                "WHERE dl.match_status IN (\'unique\',\'resolved\') "\\\n'
        '                "AND LOWER(d.club) LIKE ?)"'
    ),
    # 6. club_all
    (
        'f"EXISTS (SELECT 1 FROM {s.games} ca "\\\n'
        '            f"WHERE ca.{s.player_id}=p.{s.player_id} "\\\n'
        '            f"AND (LOWER(ca.{s.club_now})=LOWER(?) "\\\n'
        '            f"OR LOWER(ca.{s.club_hist})=LOWER(?)))"',
        'f"p.{s.player_id} IN (SELECT ca.{s.player_id} FROM {s.games} ca "\\\n'
        '            f"WHERE (LOWER(ca.{s.club_now})=LOWER(?) "\\\n'
        '            f"OR LOWER(ca.{s.club_hist})=LOWER(?)))"'
    ),
    # 7. club_any
    (
        'f"EXISTS (SELECT 1 FROM {s.games} co "\\\n'
        '            f"WHERE co.{s.player_id}=p.{s.player_id} AND ("\\\n'
        '            + " OR ".join(f"({mark})" for mark in marks) + "))"',
        'f"p.{s.player_id} IN (SELECT co.{s.player_id} FROM {s.games} co "\\\n'
        '            f"WHERE (" + " OR ".join(f"({mark})" for mark in marks) + "))"'
    ),
    # 8. game_conditions
    (
        'f"EXISTS (SELECT 1 FROM {s.games} gm "\\\n'
        '            f"WHERE gm.{s.player_id}=p.{s.player_id} AND "\\\n'
        '            + " AND ".join(game_conditions) + ")"',
        'f"p.{s.player_id} IN (SELECT gm.{s.player_id} FROM {s.games} gm "\\\n'
        '            f"WHERE " + " AND ".join(game_conditions) + ")"'
    ),
    # 9. season_conditions
    (
        'f"EXISTS (SELECT 1 FROM {s.games} ss "\\\n'
        '            f"WHERE ss.{s.player_id}=p.{s.player_id} "\\\n'
        '            f"GROUP BY ss.{s.player_id}, ss.{s.season} HAVING "\\\n'
        '            + " AND ".join(season_conditions) + ")"',
        'f"p.{s.player_id} IN (SELECT ss.{s.player_id} FROM {s.games} ss "\\\n'
        '            f"GROUP BY ss.{s.player_id}, ss.{s.season} HAVING "\\\n'
        '            + " AND ".join(season_conditions) + ")"'
    ),
    # 10. avg_conditions
    (
        'f"EXISTS (SELECT 1 FROM {s.games} av "\\\n'
        '            f"WHERE av.{s.player_id}=p.{s.player_id} "\\\n'
        '            f"GROUP BY av.{s.player_id}, av.{s.season} "\\\n'
        '            # Read from core rather than repeated here: a season average\\\n'
        '            # means the same thing in a query as it does in a grid square,\\\n'
        '            # and two copies of the floor is how they stop meaning that.\\\n'
        '            f"HAVING COUNT(*) >= {core.Generic.SEASON_AVG_MIN_GAMES} AND "\\\n'
        '            + " AND ".join(avg_conditions) + ")"',
        'f"p.{s.player_id} IN (SELECT av.{s.player_id} FROM {s.games} av "\\\n'
        '            f"GROUP BY av.{s.player_id}, av.{s.season} "\\\n'
        '            # Read from core rather than repeated here: a season average\\\n'
        '            # means the same thing in a query as it does in a grid square,\\\n'
        '            # and two copies of the floor is how they stop meaning that.\\\n'
        '            f"HAVING COUNT(*) >= {core.Generic.SEASON_AVG_MIN_GAMES} AND "\\\n'
        '            + " AND ".join(avg_conditions) + ")"'
    ),
    # 11. career_conditions
    (
        'f"EXISTS (SELECT 1 FROM {s.games} cr "\\\n'
        '            f"WHERE cr.{s.player_id}=p.{s.player_id} "\\\n'
        '            f"GROUP BY cr.{s.player_id} HAVING "\\\n'
        '            + " AND ".join(career_conditions) + ")"',
        'f"p.{s.player_id} IN (SELECT cr.{s.player_id} FROM {s.games} cr "\\\n'
        '            f"GROUP BY cr.{s.player_id} HAVING "\\\n'
        '            + " AND ".join(career_conditions) + ")"'
    ),
    # 12. captain_conditions
    (
        '"EXISTS (SELECT 1 FROM captaincies cp "\\\n'
        '            f"WHERE cp.player_id=p.{s.player_id} "\\\n'
        '            "AND cp.match_status IN (\'unique\',\'resolved\') AND "\\\n'
        '            + " AND ".join(captain_conditions) + ")"',
        'f"p.{s.player_id} IN (SELECT cp.player_id FROM captaincies cp "\\\n'
        '            "WHERE cp.match_status IN (\'unique\',\'resolved\') AND "\\\n'
        '            + " AND ".join(captain_conditions) + ")"'
    )
]

for orig, new in replacements:
    if orig not in content:
        print(f"FAILED TO FIND:\n{orig}\n")
    content = content.replace(orig, new)

with open("query_filters.py", "w", encoding="utf-8") as f:
    f.write(content)
