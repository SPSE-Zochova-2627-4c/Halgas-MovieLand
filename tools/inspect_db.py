import sqlite3
import os
p = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'movies.db')
print('DB path:', p)
if not os.path.exists(p):
    print('DB not found')
    raise SystemExit(1)
con = sqlite3.connect(p)
cur = con.cursor()
cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='movie'")
print('\nmovie table SQL:')
print(cur.fetchone())
cur.execute("PRAGMA index_list('movie')")
idxs = cur.fetchall()
print('\nindex_list:')
for r in idxs:
    print(r)
    name = r[1]
    cur.execute(f"PRAGMA index_info('{name}')")
    print('  info:', cur.fetchall())
    cur.execute(f"SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,))
    print('  create sql:', cur.fetchone())
cur.close()
con.close()
