#!/usr/bin/env python3
"""
NBA Sample Data Ingestion and Identity Resolution Pipeline
Target Directory: C:\\sports_data_lab\\data\\nba\\sample
"""

import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Paths configuration
BASE_DIR = Path("C:/sports_data_lab")
DATA_DIR = BASE_DIR / "data" / "nba"
SAMPLE_DIR = DATA_DIR / "sample"
import data_paths

#: Staging output, never data/nba/nba.db -- see data_paths.staging_db().
DB_PATH = data_paths.staging_db("nba", "nba_bbr.db")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "nba" / "nba_bbr_reference_schema.sql"


def get_db_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def step_1_apply_schema(conn: sqlite3.Connection) -> None:
    print("=" * 60)
    print("STEP 1: Applying Database Schema")
    print("=" * 60)

    if not SCHEMA_PATH.exists():
        print(f"[-] Error: Schema file not found at {SCHEMA_PATH}")
        sys.exit(1)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8-sig")
    conn.executescript(schema_sql)
    conn.commit()
    print(f"[+] Schema successfully applied to {DB_PATH}")


def step_2_ingest_sample_data(conn: sqlite3.Connection) -> None:
    print("\n" + "=" * 60)
    print("STEP 2: Ingesting Sample Datasets")
    print("=" * 60)

    imported_at = datetime.now(timezone.utc).isoformat()

    # 2a. Ingest Core Players
    players_csv = SAMPLE_DIR / "sample_players.csv"
    players_json = SAMPLE_DIR / "players_full_index.json"

    if players_csv.exists():
        print(f"[*] Ingesting players from {players_csv}...")
        sql = """
            INSERT OR REPLACE INTO players (
                bbr_player_key, player_name, player_url, surname_letter,
                career_from, career_to, position, height_inches, height_text,
                weight_lb, birth_date, college, is_active, is_hall_of_fame,
                profile_path, image_path, source_url
            ) VALUES (
                :bbr_player_key, :player_name, :player_url, :surname_letter,
                :career_from, :career_to, :position, :height_inches, :height_text,
                :weight_lb, :birth_date, :college, :is_active, :is_hall_of_fame,
                :profile_path, :image_path, :source_url
            )
        """
        # Changed to utf-8-sig to handle BOM
        with open(players_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                row["career_from"] = int(row["career_from"]) if row.get("career_from") else None
                row["career_to"] = int(row["career_to"]) if row.get("career_to") else None
                row["height_inches"] = float(row["height_inches"]) if row.get("height_inches") else None
                row["weight_lb"] = int(row["weight_lb"]) if row.get("weight_lb") else None
                row["is_active"] = int(row.get("is_active", 0))
                row["is_hall_of_fame"] = int(row.get("is_hall_of_fame", 0))
                conn.execute(sql, row)
                count += 1
        conn.commit()
        print(f"[+] Loaded {count} player records.")

    elif players_json.exists():
        print(f"[*] Ingesting players from {players_json}...")
        with open(players_json, "r", encoding="utf-8-sig") as f:
            players_data = json.load(f)

        sql = """
            INSERT OR REPLACE INTO players (
                bbr_player_key, player_name, player_url, surname_letter,
                career_from, career_to, position, height_inches, height_text,
                weight_lb, birth_date, college, is_active, is_hall_of_fame,
                profile_path, image_path, source_url
            ) VALUES (
                :bbr_player_key, :player_name, :player_url, :surname_letter,
                :career_from, :career_to, :position, :height_inches, :height_text,
                :weight_lb, :birth_date, :college, :is_active, :is_hall_of_fame,
                :profile_path, :image_path, :source_url
            )
        """
        for row in players_data:
            conn.execute(sql, row)
        conn.commit()
        print(f"[+] Loaded {len(players_data)} player records.")

    # 2b. Ingest Seasons
    seasons_csv = SAMPLE_DIR / "sample_seasons.csv"
    if seasons_csv.exists():
        print(f"[*] Ingesting seasons from {seasons_csv}...")
        sql = """
            INSERT OR REPLACE INTO seasons (
                season, season_label, league, bbr_season_year,
                regular_games, playoff_games, games, schedule_complete, schedule_errors
            ) VALUES (
                :season, :season_label, :league, :bbr_season_year,
                :regular_games, :playoff_games, :games, :schedule_complete, :schedule_errors
            )
        """
        with open(seasons_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                row["bbr_season_year"] = int(row["bbr_season_year"]) if row.get("bbr_season_year") else None
                row["regular_games"] = int(row["regular_games"]) if row.get("regular_games") else 0
                row["playoff_games"] = int(row["playoff_games"]) if row.get("playoff_games") else 0
                row["games"] = int(row["games"]) if row.get("games") else 0
                row["schedule_complete"] = 1 if str(row.get("schedule_complete")).lower() == "true" else 0
                conn.execute(sql, row)
                count += 1
        conn.commit()
        print(f"[+] Loaded {count} season records.")

    # 2c. Ingest Games
    games_csv = SAMPLE_DIR / "sample_games.csv"
    if games_csv.exists():
        print(f"[*] Ingesting games from {games_csv}...")
        sql = """
            INSERT OR REPLACE INTO games (
                bbr_game_key, league, season, season_label, bbr_season_year,
                phase, game_date, game_time, visitor_team_name, visitor_team_key,
                visitor_points, home_team_name, home_team_key, home_points,
                overtime, attendance, arena, boxscore_url, game_path, source_url
            ) VALUES (
                :bbr_game_key, :league, :season, :season_label, :bbr_season_year,
                :phase, :game_date, :game_time, :visitor_team_name, :visitor_team_key,
                :visitor_points, :home_team_name, :home_team_key, :home_points,
                :overtime, :attendance, :arena, :boxscore_url, :game_path, :source_url
            )
        """
        with open(games_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                row["bbr_season_year"] = int(row["bbr_season_year"]) if row.get("bbr_season_year") else None
                row["visitor_points"] = int(row["visitor_points"]) if row.get("visitor_points") else 0
                row["home_points"] = int(row["home_points"]) if row.get("home_points") else 0
                row["attendance"] = int(row["attendance"]) if row.get("attendance") else None
                conn.execute(sql, row)
                count += 1
        conn.commit()
        print(f"[+] Loaded {count} game records.")

    # 2d. Ingest Leaderboard Records (if present)
    leaders_csv = SAMPLE_DIR / "leaderboard_records.csv"
    if leaders_csv.exists():
        print(f"[*] Ingesting staging leaderboards from {leaders_csv}...")
        sql = """
            INSERT OR IGNORE INTO nba_bbr_leader_records (
                leaderboard_key, stat_code, record_type, competition_scope, league_scope,
                rank_source, player_name, player_url, player_key, is_hall_of_fame,
                is_active_snapshot, value_text, value_numeric, season_source, year_source,
                league, team_source, team_id, source_url, raw_row_json, match_status, imported_at
            ) VALUES (
                :leaderboard_key, :stat_code, :record_type, :competition_scope, :league_scope,
                :rank, :player_name, :player_url, :player_key, :is_hall_of_fame,
                :is_active_snapshot, :value_text, :value_numeric, :season, :year,
                :league, :team, :team_id, :source_url, :raw_row_json, 'unresolved', :imported_at
            )
        """
        with open(leaders_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                row["imported_at"] = imported_at
                row["is_hall_of_fame"] = 1 if str(row.get("is_hall_of_fame")).lower() == "true" else 0
                row["is_active_snapshot"] = 1 if str(row.get("is_active_snapshot")).lower() == "true" else 0
                try:
                    row["value_numeric"] = float(row.get("value_numeric")) if row.get("value_numeric") else None
                except ValueError:
                    row["value_numeric"] = None
                if "raw_row_json" not in row:
                    row["raw_row_json"] = json.dumps(row)
                conn.execute(sql, row)
                count += 1
        conn.commit()
        print(f"[+] Staged {count} leaderboard records.")

    # 2e. Ingest Awards Catalog (if present)
    awards_csv = SAMPLE_DIR / "awards_catalog.csv"
    if awards_csv.exists():
        print(f"[*] Ingesting awards catalog from {awards_csv}...")
        sql = """
            INSERT OR IGNORE INTO nba_bbr_award_catalog (
                award_key, section, award_name, award_url, enabled, last_imported_at
            ) VALUES (
                :award_key, :section, :award_name, :award_url, 0, :imported_at
            )
        """
        with open(awards_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                row["imported_at"] = imported_at
                conn.execute(sql, row)
                count += 1
        conn.commit()
        print(f"[+] Loaded {count} award catalog records.")


def step_3_resolve_identities(conn: sqlite3.Connection) -> None:
    print("\n" + "=" * 60)
    print("STEP 3: Identity Resolution & Player Linking")
    print("=" * 60)

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM nba_bbr_leader_records WHERE match_status = 'unresolved'")
        unresolved_before = cursor.fetchone()[0]

        print(f"[*] Unresolved records pending resolution: {unresolved_before}")

        if unresolved_before == 0:
            print("[!] No unresolved records found in staging.")
            return

        match_sql = """
            UPDATE nba_bbr_leader_records
            SET 
                match_status = 'matched',
                player_id = (
                    SELECT id FROM players 
                    WHERE players.bbr_player_key = nba_bbr_leader_records.player_key 
                    LIMIT 1
                )
            WHERE match_status = 'unresolved'
              AND EXISTS (
                  SELECT 1 FROM players 
                  WHERE players.bbr_player_key = nba_bbr_leader_records.player_key
              );
        """
        cursor.execute(match_sql)
        matched_count = cursor.rowcount
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM nba_bbr_leader_records WHERE match_status = 'unresolved'")
        unresolved_after = cursor.fetchone()[0]

        print(f"[+] Successfully linked {matched_count} records to core players.")
        print(f"[+] Remaining unresolved records: {unresolved_after}")
    except sqlite3.OperationalError:
        print("[!] Skipped matching: Leaderboard records table likely empty or not imported.")


def print_summary(conn: sqlite3.Connection) -> None:
    print("\n" + "=" * 60)
    print("DATABASE SUMMARY DIAGNOSTICS")
    print("=" * 60)
    cursor = conn.cursor()

    tables = ["players", "seasons", "games", "nba_bbr_leader_records", "nba_bbr_award_catalog"]
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            print(f"  {table:<25}: {count:>6,} rows")
        except sqlite3.OperationalError:
            print(f"  {table:<25}: Table not created.")

    try:
        cursor.execute("SELECT match_status, COUNT(*) FROM nba_bbr_leader_records GROUP BY match_status")
        statuses = cursor.fetchall()
        if statuses:
            print("\n  Staging Match Statuses:")
            for status, count in statuses:
                print(f"    - {status:<15}: {count:>6,} rows")
    except sqlite3.OperationalError:
        pass


def main():
    print(f"Starting NBA Data Pipeline Steps 1-3")
    print(f"Base Directory : {BASE_DIR}")
    print(f"Sample Source  : {SAMPLE_DIR}")
    print(f"Database Path  : {DB_PATH}")

    conn = get_db_connection()
    try:
        step_1_apply_schema(conn)
        step_2_ingest_sample_data(conn)
        step_3_resolve_identities(conn)
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
