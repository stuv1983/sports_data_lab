import sqlite3
import csv

import data_paths

#: Staging output, never data/nba/nba.db -- see data_paths.staging_db().
DB_PATH = str(data_paths.staging_db("nba", "nba_bbr.db"))
UNRESOLVED_CSV = "unresolved_nba_links.csv"

def link_players():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("Starting Phase 1: Exact Key Match...")
    # Update staging records where the bbr_key exactly matches the core players table
    cur.execute("""
        UPDATE nba_bbr_leader_records
        SET 
            match_status = 'matched',
            player_id = (SELECT player_id FROM players WHERE players.bbr_key = nba_bbr_leader_records.player_key)
        WHERE 
            match_status = 'unresolved' 
            AND player_key IN (SELECT bbr_key FROM players WHERE bbr_key IS NOT NULL)
    """)
    phase_1_matches = cur.rowcount
    print(f"Phase 1 Matched: {phase_1_matches} records.")

    print("Starting Phase 2: Safe Name Match...")
    # Find staging records where the player name is 100% unique in BOTH tables
    cur.execute("""
        WITH UniqueCore AS (
            SELECT player_name, player_id FROM players 
            GROUP BY player_name HAVING COUNT(*) = 1
        ),
        UniqueStaging AS (
            SELECT player_name, player_key FROM nba_bbr_leader_records 
            WHERE match_status = 'unresolved'
            GROUP BY player_name HAVING COUNT(*) = 1
        )
        UPDATE nba_bbr_leader_records
        SET 
            match_status = 'matched',
            player_id = (SELECT player_id FROM UniqueCore WHERE UniqueCore.player_name = nba_bbr_leader_records.player_name)
        WHERE 
            match_status = 'unresolved'
            AND player_name IN (SELECT player_name FROM UniqueCore)
            AND player_name IN (SELECT player_name FROM UniqueStaging)
    """)
    phase_2_matches = cur.rowcount
    print(f"Phase 2 Matched: {phase_2_matches} records.")

    # Update the core table with the new keys we just safely matched by name
    cur.execute("""
        UPDATE players
        SET bbr_key = (
            SELECT player_key FROM nba_bbr_leader_records 
            WHERE nba_bbr_leader_records.player_id = players.player_id 
            LIMIT 1
        )
        WHERE bbr_key IS NULL
    """)

    print("Starting Phase 3: Exporting Unresolved Queue...")
    # Grab whatever is left
    cur.execute("""
        SELECT leaderboard_key, player_name, player_key, season_source 
        FROM nba_bbr_leader_records 
        WHERE match_status = 'unresolved'
    """)
    unresolved_rows = cur.fetchall()

    if unresolved_rows:
        with open(UNRESOLVED_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['leaderboard_key', 'player_name', 'player_key', 'season_source'])
            writer.writerows(unresolved_rows)
        print(f"Phase 3: Exported {len(unresolved_rows)} unresolved records to {UNRESOLVED_CSV} for manual review.")
    else:
        print("Phase 3: 0 unresolved records! Clean sweep.")

    con.commit()
    con.close()
    print("Identity resolution complete.")

if __name__ == "__main__":
    link_players()