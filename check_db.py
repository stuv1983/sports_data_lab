import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "gridley.db"
con = sqlite3.connect(db)
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(f"{db}: {len(tables)} table(s)")
for name in tables:
    count = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(f"  {name:30} {count:>10,}")
