#!/usr/bin/env python3
"""
NBA Players Table Patch
Adds missing aggregate and identifier columns to the 'players' table
to satisfy the core application's schema validation.
"""

import sqlite3

import data_paths

#: Staging output, never data/nba/nba.db -- see data_paths.staging_db().
DB_PATH = data_paths.staging_db("nba", "nba_bbr.db")

def patch_players_table():
    print(f"Applying players patch to {DB_PATH}...")
    
    missing_columns = {
        'birth_year': 'INTEGER',
        'career_games': 'INTEGER',
        'career_minutes': 'INTEGER',
        'career_points': 'INTEGER',
        'clubs_hist': 'TEXT',
        'debut_season': 'INTEGER',
        'final_season': 'INTEGER',
        'n_clubs': 'INTEGER',
        'name_key': 'TEXT',
        'obscurity': 'REAL',
        'player': 'TEXT',
        'player_id': 'INTEGER',
        'playoffs_played': 'INTEGER'
    }

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 1. Inject the missing columns
        for col, col_type in missing_columns.items():
            try:
                cursor.execute(f"ALTER TABLE players ADD COLUMN {col} {col_type}")
                print(f" [+] Added column: {col}")
            except sqlite3.OperationalError:
                # Column likely already exists
                pass 
                
        # 2. Map existing data and set defaults for aggregates
        try:
            cursor.execute("""
                UPDATE players 
                SET player = player_name,
                    player_id = id,
                    debut_season = career_from,
                    final_season = career_to,
                    name_key = bbr_player_key,
                    birth_year = CAST(SUBSTR(birth_date, 1, 4) AS INTEGER),
                    career_games = COALESCE(career_games, 0),
                    career_minutes = COALESCE(career_minutes, 0),
                    career_points = COALESCE(career_points, 0),
                    clubs_hist = COALESCE(clubs_hist, ''),
                    n_clubs = COALESCE(n_clubs, 0),
                    obscurity = COALESCE(obscurity, 0.0),
                    playoffs_played = COALESCE(playoffs_played, 0)
            """)
            conn.commit()
            print(" [+] Mapped existing player data and initialized career aggregates to 0.")
        except Exception as e:
            print(f" [!] Could not map data: {e}")

    print("Patch complete. You can now start Streamlit.")

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
    else:
        patch_players_table()
