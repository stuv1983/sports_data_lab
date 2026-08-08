import sys
sys.path.append(r'c:\sports_data_lab')
import json
from afl import historic_grids as HG
from sports import AFL
import db_pool
con = db_pool.get_con('data/afl/afl_temp.db', '1')
cur = con.execute('SELECT grid_num, date, source, rows_json, cols_json FROM historic_grids')
for r in cur.fetchall():
    cols = tuple(json.loads(r[4]))
    rows = tuple(json.loads(r[3]))
    grid = HG.HistoricGrid(number=r[0], date=r[1], source=r[2], rows=rows, cols=cols)
    report = HG.analyse(grid, con, AFL)
    print(f"Grid {r[0]}: Playable={report.authentic_playable}, Error={not report.authentic_playable and report.status or None}")
