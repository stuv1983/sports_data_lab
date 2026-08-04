import sqlite3
from pathlib import Path

DB_PATH = Path("data/nba/nba.db")

def link_staged_players(conn: sqlite3.Connection):
    """
    Attempts to link unresolved BBR leaderboard records to the core NBA players table.
    Assumes your core players table has an identifier like 'bbr_key' or 'url_key'.
    """
    print("Starting identity resolution for staged leaderboard records...")
    
    # 1. Check how many are unresolved
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM nba_bbr_leader_records WHERE match_status = 'unresolved'")
    unresolved_count = cursor.fetchone()[0]
    print(f"Found {unresolved_count} unresolved records.")
    
    if unresolved_count == 0:
        return

    # 2. Perform the matching update
    # This query assumes your core `players` table is structured similarly to the AFL side, 
    # and has a `bbr_id` or `player_key` column derived from the Basketball Reference scrape.
    match_sql = """
        UPDATE nba_bbr_leader_records
        SET 
            match_status = 'matched',
            player_id = (
                SELECT id FROM players 
                WHERE players.bbr_key = nba_bbr_leader_records.player_key 
                LIMIT 1
            )
        WHERE match_status = 'unresolved'
          AND EXISTS (
              SELECT 1 FROM players 
              WHERE players.bbr_key = nba_bbr_leader_records.player_key
          )
    """
    
    try:
        cursor.execute(match_sql)
        matched_count = cursor.rowcount
        conn.commit()
        
        print(f"Successfully matched {matched_count} records to core players.")
        print(f"Remaining unresolved: {unresolved_count - matched_count}")
        
    except sqlite3.OperationalError as e:
        print(f"Notice: Could not run matching. Is the core 'players' table fully built yet? Error: {e}")

def main():
    if not DB_PATH.exists():
        print(f"Database {DB_PATH} not found. Please run the ingestion script first.")
        return
        
    with sqlite3.connect(DB_PATH) as conn:
        link_staged_players(conn)

if __name__ == "__main__":
    main()