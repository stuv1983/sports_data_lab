-- NBA Core & Basketball-Reference Staging Schema
-- Location: C:\sports_data_lab\nba_bbr_reference_schema.sql

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS nba_bbr_leader_records;
DROP TABLE IF EXISTS nba_bbr_award_catalog;
DROP TABLE IF EXISTS games;
DROP TABLE IF EXISTS seasons;
DROP TABLE IF EXISTS players;

PRAGMA foreign_keys = ON;

--------------------------------------------------------------------------------
-- 1. CORE TABLES (Players, Seasons, Games)
--------------------------------------------------------------------------------

CREATE TABLE players (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bbr_player_key      TEXT UNIQUE NOT NULL,
    player_name         TEXT NOT NULL,
    player_url          TEXT,
    surname_letter      TEXT,
    career_from         INTEGER,
    career_to           INTEGER,
    position            TEXT,
    height_inches       REAL,
    height_text         TEXT,
    weight_lb           INTEGER,
    birth_date          TEXT,
    college             TEXT,
    is_active           INTEGER NOT NULL DEFAULT 0,
    is_hall_of_fame     INTEGER NOT NULL DEFAULT 0,
    profile_path        TEXT,
    image_path          TEXT,
    source_url          TEXT
);

CREATE INDEX ix_players_bbr_key ON players(bbr_player_key);
CREATE INDEX ix_players_name ON players(player_name);

CREATE TABLE seasons (
    season              TEXT PRIMARY KEY,
    season_label        TEXT NOT NULL,
    league              TEXT NOT NULL,
    bbr_season_year     INTEGER,
    regular_games       INTEGER DEFAULT 0,
    playoff_games       INTEGER DEFAULT 0,
    games               INTEGER DEFAULT 0,
    schedule_complete   INTEGER DEFAULT 0,
    schedule_errors     TEXT
);

CREATE TABLE games (
    bbr_game_key        TEXT PRIMARY KEY,
    league              TEXT NOT NULL,
    season              TEXT NOT NULL,
    season_label        TEXT NOT NULL,
    bbr_season_year     INTEGER,
    phase               TEXT NOT NULL,
    game_date           TEXT NOT NULL,
    game_time           TEXT,
    visitor_team_name   TEXT NOT NULL,
    visitor_team_key    TEXT NOT NULL,
    visitor_points      INTEGER NOT NULL,
    home_team_name      TEXT NOT NULL,
    home_team_key       TEXT NOT NULL,
    home_points         INTEGER NOT NULL,
    overtime            TEXT,
    attendance          INTEGER,
    arena               TEXT,
    boxscore_url        TEXT NOT NULL,
    game_path           TEXT,
    source_url          TEXT NOT NULL,
    FOREIGN KEY(season) REFERENCES seasons(season)
);

CREATE INDEX ix_games_season ON games(season);
CREATE INDEX ix_games_teams ON games(home_team_key, visitor_team_key);

--------------------------------------------------------------------------------
-- 2. BASKETBALL REFERENCE STAGING TABLES
--------------------------------------------------------------------------------

CREATE TABLE nba_bbr_leader_records (
    leaderboard_key     TEXT NOT NULL,
    stat_code           TEXT NOT NULL,
    record_type         TEXT NOT NULL,
    competition_scope   TEXT NOT NULL,
    league_scope        TEXT,
    rank_source         TEXT,
    player_name         TEXT NOT NULL,
    player_url          TEXT,
    player_key          TEXT,
    player_id           INTEGER,
    is_hall_of_fame     INTEGER NOT NULL DEFAULT 0,
    is_active_snapshot  INTEGER NOT NULL DEFAULT 0,
    value_text          TEXT NOT NULL,
    value_numeric       REAL,
    season_source       TEXT,
    year_source         TEXT,
    league              TEXT,
    team_source         TEXT,
    team_id             TEXT,
    source_url          TEXT NOT NULL,
    raw_row_json        TEXT NOT NULL,
    match_status        TEXT NOT NULL DEFAULT 'unresolved',
    imported_at         TEXT NOT NULL,
    PRIMARY KEY (
        leaderboard_key,
        league_scope,
        player_key,
        season_source,
        year_source,
        value_text
    ),
    FOREIGN KEY(player_id) REFERENCES players(id)
);

CREATE INDEX ix_bbr_leaders_player ON nba_bbr_leader_records(player_key);
CREATE INDEX ix_bbr_leaders_stat_value ON nba_bbr_leader_records(stat_code, record_type, value_numeric);
CREATE INDEX ix_bbr_leaders_match_status ON nba_bbr_leader_records(match_status);

CREATE TABLE nba_bbr_award_catalog (
    award_key           TEXT PRIMARY KEY,
    section             TEXT,
    award_name          TEXT NOT NULL,
    award_url           TEXT NOT NULL UNIQUE,
    enabled             INTEGER NOT NULL DEFAULT 0,
    last_imported_at    TEXT
);