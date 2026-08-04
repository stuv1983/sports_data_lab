import sqlite3

import data_paths

#: Staging output, never data/nba/nba.db -- see data_paths.staging_db().
DB_PATH = str(data_paths.staging_db("nba", "nba_bbr.db"))

def seed_test_players():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("Seeding core players table from staging data...")
    
    # Grab unique players from the staging table that have a valid key
    # We use INSERT OR IGNORE in case you run this multiple times
    cur.execute("""
        INSERT OR IGNORE INTO players (player_name, bbr_key)
        SELECT DISTINCT player_name, player_key 
        FROM nba_bbr_leader_records
        WHERE player_key IS NOT NULL
    """)
    
    seeded_count = cur.rowcount
    print(f"Successfully seeded {seeded_count} unique players into the core 'players' table.")
    
    con.commit()
    con.close()

if __name__ == "__main__":
    seed_test_players()