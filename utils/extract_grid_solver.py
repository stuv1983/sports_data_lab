import os

with open("app.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Grid solver starts around line 893: `st.sidebar.markdown("---")`
# Wait, let's find the exact index.
start_idx = 0
for i, line in enumerate(lines):
    if line.startswith('st.sidebar.markdown("---")') and 'Grid setup' in lines[i+1] if i+1 < len(lines) else False:
        start_idx = i
        break

grid_solver_lines = lines[start_idx:]

header = """import streamlit as st
import pandas as pd
import accounts
import core
import ui_widgets
import components

SPORT = st.session_state.SPORT
con = st.session_state.con
DB_REVISION = st.session_state.DB_REVISION
SCHEMA = SPORT.schema
V = SPORT.vocab
C = SPORT.C
AVAILABLE = st.session_state.AVAILABLE

def get_con(db, rev):
    import db_pool
    return db_pool.get_con(db, rev)

def season_span(*args):
    return ui_widgets.season_span(*args)

player_picker = ui_widgets.player_picker

"""

with open(os.path.join("pages", "11_Grid_Solver.py"), "w", encoding="utf-8") as f:
    f.write(header)
    for line in grid_solver_lines:
        f.write(line)

