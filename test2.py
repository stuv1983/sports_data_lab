import sys
sys.path.append(r'c:\sports_data_lab')
import json
from afl import historic_grids as HG
from sports import AFL
import db_pool
con = db_pool.get_con('data/afl/afl_temp.db', '1')

cur = con.execute('SELECT grid_num, date, source, rows_json, cols_json FROM historic_grids ORDER BY grid_num LIMIT 1')
r = cur.fetchone()
cols = tuple(json.loads(r[4]))
rows = tuple(json.loads(r[3]))

print(f"Testing Grid {r[0]}")
print(f"Rows: {rows}")
print(f"Cols: {cols}")

grid = HG.HistoricGrid(number=r[0], date=r[1], source=r[2], rows=rows, cols=cols)

print("Starting analyse...")
report = HG.analyse(grid, con, AFL)
print("Finished analyse")
