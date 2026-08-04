import sqlite3
import csv
import os
from pathlib import Path

# Configuration
DB_PATH = "data/mlb/mlb.db"
RAW_DATA_DIR = "data/mlb/raw/"

def create_schema(cursor):
    """Creates the core tables for the MLB database."""
    print("Creating database schema...")
    
    # Players Table (Sourced from People.csv)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            given_name TEXT,
            birth_year INTEGER,
            birth_country TEXT,
            debut_date TEXT,
            final_game_date TEXT
        )
    """)
    
    # Batting Table (Sourced from Batting.csv)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batting (
            player_id TEXT,
            year INTEGER,
            stint INTEGER,
            team_id TEXT,
            league_id TEXT,
            games INTEGER,
            at_bats INTEGER,
            runs INTEGER,
            hits INTEGER,
            doubles INTEGER,
            triples INTEGER,
            home_runs INTEGER,
            rbis INTEGER,
            stolen_bases INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            FOREIGN KEY (player_id) REFERENCES players (player_id)
        )
    """)
    
    # Pitching Table (Sourced from Pitching.csv)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching (
            player_id TEXT,
            year INTEGER,
            stint INTEGER,
            team_id TEXT,
            league_id TEXT,
            wins INTEGER,
            losses INTEGER,
            games INTEGER,
            games_started INTEGER,
            saves INTEGER,
            innings_pitchedouts INTEGER,
            hits_allowed INTEGER,
            earned_runs INTEGER,
            home_runs_allowed INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            era REAL,
            FOREIGN KEY (player_id) REFERENCES players (player_id)
        )
    """)
    
    # Create indexes for faster Streamlit querying
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batting_player ON batting(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitching_player ON pitching(player_id)")

def clean_val(val):
    """Converts empty CSV strings to None for SQLite NULLs."""
    return None if val == "" else val

def ingest_people(cursor):
    """Loads player biographic data."""
    print("Ingesting People.csv...")
    file_path = os.path.join(RAW_DATA_DIR, "People.csv")
    
    with open(file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [(
            clean_val(row["playerID"]),
            clean_val(row["nameFirst"]),
            clean_val(row["nameLast"]),
            clean_val(row["nameGiven"]),
            clean_val(row["birthYear"]),
            clean_val(row["birthCountry"]),
            clean_val(row["debut"]),
            clean_val(row["finalGame"])
        ) for row in reader]
        
    cursor.executemany("""
        INSERT OR IGNORE INTO players 
        (player_id, first_name, last_name, given_name, birth_year, birth_country, debut_date, final_game_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    print(f"Loaded {len(rows)} players.")

def ingest_batting(cursor):
    """Loads batting statistics."""
    print("Ingesting Batting.csv...")
    file_path = os.path.join(RAW_DATA_DIR, "Batting.csv")
    
    with open(file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [(
            clean_val(row["playerID"]), clean_val(row["yearID"]), clean_val(row["stint"]),
            clean_val(row["teamID"]), clean_val(row["lgID"]), clean_val(row["G"]),
            clean_val(row["AB"]), clean_val(row["R"]), clean_val(row["H"]),
            clean_val(row["2B"]), clean_val(row["3B"]), clean_val(row["HR"]),
            clean_val(row["RBI"]), clean_val(row["SB"]), clean_val(row["BB"]),
            clean_val(row["SO"])
        ) for row in reader]

    cursor.executemany("""
        INSERT INTO batting 
        (player_id, year, stint, team_id, league_id, games, at_bats, runs, hits, 
         doubles, triples, home_runs, rbis, stolen_bases, walks, strikeouts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    print(f"Loaded {len(rows)} batting records.")

def ingest_pitching(cursor):
    """Loads pitching statistics."""
    print("Ingesting Pitching.csv...")
    file_path = os.path.join(RAW_DATA_DIR, "Pitching.csv")
    
    with open(file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [(
            clean_val(row["playerID"]), clean_val(row["yearID"]), clean_val(row["stint"]),
            clean_val(row["teamID"]), clean_val(row["lgID"]), clean_val(row["W"]),
            clean_val(row["L"]), clean_val(row["G"]), clean_val(row["GS"]),
            clean_val(row["SV"]), clean_val(row["IPouts"]), clean_val(row["H"]),
            clean_val(row["ER"]), clean_val(row["HR"]), clean_val(row["BB"]),
            clean_val(row["SO"]), clean_val(row["ERA"])
        ) for row in reader]

    cursor.executemany("""
        INSERT INTO pitching 
        (player_id, year, stint, team_id, league_id, wins, losses, games, games_started, 
         saves, innings_pitchedouts, hits_allowed, earned_runs, home_runs_allowed, walks, strikeouts, era)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    print(f"Loaded {len(rows)} pitching records.")

def main():
    # Ensure database directory exists
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    
    # Connect and build
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        create_schema(cursor)
        
        try:
            ingest_people(cursor)
            ingest_batting(cursor)
            ingest_pitching(cursor)
            conn.commit()
            print("\nMLB database build complete!")
        except FileNotFoundError as e:
            print(f"\nError: {e}")
            print("Please ensure you have extracted the Lahman CSV files into 'data/mlb/raw/'.")

if __name__ == "__main__":
    main()