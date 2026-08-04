# constraints_mlb.py

SPORT_NAME = "MLB"
DB_PATH = "data/mlb/mlb.db"

# Vocabulary mapping for the shared Streamlit UI
VOCAB = {
    "club": "franchise",
    "match": "game",
    "finals": "postseason",
    "premiership": "World Series",
    "captain": "manager", # Lahman has manager data, captaincy is less formal in MLB
}

# Statistical columns expected in the database
STAT_SCHEMA = [
    "games",
    "at_bats",
    "runs",
    "hits",
    "doubles",
    "triples",
    "home_runs",
    "rbis",
    "stolen_bases",
    "walks",
    "strikeouts_batter",
    "wins",
    "losses",
    "era",
    "strikeouts_pitcher",
    "saves"
]

# Streamlit UI Themes
THEMES = {
    "light": {"primary": "#002D72", "background": "#FFFFFF", "text": "#000000"}, # Navy Blue
    "dark": {"primary": "#D50032", "background": "#121212", "text": "#FFFFFF"},  # Red
}