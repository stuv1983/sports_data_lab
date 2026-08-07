#!/usr/bin/env python3
import json
import sqlite3
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_paths import sport_db
from afl import historic_grids as HG

def migrate():
    db_path = sport_db("afl")
    print(f"Migrating AFL grids into {db_path}...")
    
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS historic_grids (
                grid_num INTEGER PRIMARY KEY,
                date TEXT,
                source TEXT,
                rows_json TEXT,
                cols_json TEXT,
                unsupported_json TEXT,
                note TEXT
            )
        """)
        
        inserted = 0
        for grid in HG.GRIDS:
            if not grid.number:
                continue
            
            try:
                con.execute("""
                    INSERT OR IGNORE INTO historic_grids (
                        grid_num, date, source, rows_json, cols_json, unsupported_json, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    grid.number,
                    grid.date,
                    grid.source,
                    json.dumps(grid.rows),
                    json.dumps(grid.cols),
                    json.dumps(grid.unsupported),
                    grid.note
                ))
                inserted += 1
            except Exception as e:
                print(f"Error inserting grid {grid.number}: {e}")
                
        print(f"Inserted {inserted} grids.")

if __name__ == "__main__":
    migrate()
