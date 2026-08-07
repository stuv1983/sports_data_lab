import sqlite3
import csv
import json
from pathlib import Path
from datetime import datetime

import data_paths

#: Staging output, never data/nba/nba.db -- see data_paths.staging_db().
DB_PATH = data_paths.staging_db("nba", "nba_bbr.db")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "nba" / "nba_bbr_reference_schema.sql"

# Update these paths if your sample data is stored elsewhere
LEADERBOARDS_CSV = Path("data/nba/raw/basketball_reference/indexes/leaderboard_records.csv")
AWARDS_CSV = Path("data/nba/raw/basketball_reference/indexes/awards_catalog.csv")

def setup_staging_schema(conn: sqlite3.Connection):
    """Executes the SQL schema file to create staging tables."""
    print(f"Applying schema from {SCHEMA_PATH}...")
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()

def ingest_leaderboards(conn: sqlite3.Connection):
    """Loads leaderboard_records.csv into the staging table."""
    if not LEADERBOARDS_CSV.exists():
        print(f"Skipping leaderboards: {LEADERBOARDS_CSV} not found.")
        return

    print(f"Ingesting {LEADERBOARDS_CSV}...")
    imported_at = datetime.utcnow().isoformat()
    
    insert_sql = """
        INSERT OR IGNORE INTO nba_bbr_leader_records (
            leaderboard_key, stat_code, record_type, competition_scope, league_scope,
            rank_source, player_name, player_url, player_key, is_hall_of_fame,
            is_active_snapshot, value_text, value_numeric, season_source, year_source,
            league, team_source, source_url, raw_row_json, imported_at
        ) VALUES (
            :leaderboard_key, :stat_code, :record_type, :competition_scope, :league_scope,
            :rank, :player_name, :player_url, :player_key, :is_hall_of_fame,
            :is_active_snapshot, :value_text, :value_numeric, :season, :year,
            :league, :team, :source_url, :raw_row_json, :imported_at
        )
    """
    
    with open(LEADERBOARDS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            # Map CSV fields to the dictionary expected by SQLite
            row['imported_at'] = imported_at
            
            # Safely handle booleans/integers
            row['is_hall_of_fame'] = 1 if str(row.get('is_hall_of_fame', '')).lower() == 'true' else 0
            row['is_active_snapshot'] = 1 if str(row.get('is_active_snapshot', '')).lower() == 'true' else 0
            
            # Safely parse numeric value
            try:
                row['value_numeric'] = float(row.get('value_numeric')) if row.get('value_numeric') else None
            except ValueError:
                row['value_numeric'] = None

            # Fallback for raw JSON if it's not present in your specific CSV version
            if 'raw_row_json' not in row:
                row['raw_row_json'] = json.dumps(row)

            conn.execute(insert_sql, row)
            count += 1
            
    conn.commit()
    print(f"Successfully staged {count} leaderboard records.")

def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        setup_staging_schema(conn)
        ingest_leaderboards(conn)
        # You can add a similar ingest_awards(conn) function here using AWARDS_CSV
        
if __name__ == "__main__":
    main()
