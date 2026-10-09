from jinja2 import Environment, FileSystemLoader
import sqlite3

env = Environment(loader=FileSystemLoader('../templates'))
import os
BASE = os.path.dirname(os.path.dirname(__file__))
env = Environment(loader=FileSystemLoader(os.path.join(BASE, 'templates')))
conn = sqlite3.connect(os.path.join(BASE, 'instance', 'movies.db'))
cur = conn.cursor()
cur.execute("SELECT movie_id,title,poster FROM movie WHERE category='diary' ORDER BY date DESC LIMIT 4")
rows = cur.fetchall()
movies = [{'movie_id': r[0], 'title': r[1], 'poster': r[2]} for r in rows]
user = {'username': 'testuser'}

tmpl = env.get_template('profile.html')
out = tmpl.render(user=user, movies=movies)

print('HAS_MOVIE_TITLE_ELEMENTS=', '<div class="movie-title">' in out)
# print a short snippet around the first movie-title occurrence
if '<div class="movie-title">' in out:
    idx = out.find('<div class="movie-title">')
    print(out[idx:idx+200])
else:
    print('No movie-title elements found in rendered output')

# also print the full rendered HTML to a file for inspection
with open('rendered_profile_debug.html', 'w', encoding='utf-8') as f:
    f.write(out)

print('\nWrote rendered_profile_debug.html')
