from flask import Flask, render_template, request, jsonify, redirect
import requests
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, abort, flash
import os, uuid
from sqlalchemy.exc import IntegrityError


app = Flask(__name__)

# Avatar uploads
AVATAR_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'avatars')
os.makedirs(AVATAR_FOLDER, exist_ok=True)
app.config['AVATAR_FOLDER'] = AVATAR_FOLDER
ALLOWED_AVATAR_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

# Allowed categories that can be stored in the DB
ALLOWED_CATEGORIES = {"watchlist", "diary"}

# Load secrets from environment. For local development you can create a .env file
# (do NOT commit it). If python-dotenv is installed it will be loaded automatically.
try:
    # optional dependency; if present it loads a local .env file into os.environ
    from dotenv import load_dotenv
    # load default .env, then load api.env if present (api.env is convenient for local dev)
    load_dotenv('.env')
    load_dotenv('api.env')
except Exception:
    pass

# If python-dotenv isn't installed (or failed to load), try a tiny fallback loader
# for a local `api.env` file so local development works without extra dependencies.
if not os.getenv('TMDB_API_KEY'):
    env_path = os.path.join(os.path.dirname(__file__), 'api.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and (k not in os.environ):
                        os.environ[k] = v
        except Exception:
            # best-effort only; don't raise here
            pass

# Application secret key (Flask session). Prefer setting APP_SECRET in your environment.
APP_SECRET = os.getenv('APP_SECRET')
if not APP_SECRET:
    # generate a temporary secret for local testing only; do NOT rely on this in production
    APP_SECRET = os.urandom(24).hex()
    # use logger instead of print
    app.logger.warning("APP_SECRET not set in environment; using a temporary generated secret.")

app.secret_key = APP_SECRET

# TMDB API key must be provided via the TMDB_API_KEY environment variable.
# This prevents accidentally committing the key to version control.
API_KEY = os.getenv('TMDB_API_KEY')
if not API_KEY:
    raise RuntimeError("TMDB_API_KEY environment variable is not set.\n"
                       "Set TMDB_API_KEY in your environment or create a local .env file based on .env.example.")

# database
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///movies.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.String(300), nullable=True)
    avatar_path = db.Column(db.String(300), nullable=True)  # relative path under static/avatars/
    # relationship: a user can have many movies
    movies = db.relationship('Movie', backref='owner', lazy=True)

# model
class Movie(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer)
    title = db.Column(db.String(200))
    category = db.Column(db.String(50))
    poster = db.Column(db.String(300))  # 👈 PRIDAJ
    review = db.Column(db.Text)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    rating = db.Column(db.Integer)
    liked = db.Column(db.Boolean, default=False)
    # mark if this diary entry is a rewatch (user logged it again)
    rewatch = db.Column(db.Boolean, default=False)
    # associate a movie with a user (optional for existing records)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # runtime in minutes and comma-separated genres (populated from TMDB on save)
    runtime = db.Column(db.Integer, nullable=True)
    genres = db.Column(db.String(500), nullable=True)


class Top4Slot(db.Model):
    """A per-user slot for a Top 4 movie. slot is 1..4."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    slot = db.Column(db.Integer, nullable=False)
    movie_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(300))
    poster = db.Column(db.String(300))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'slot', name='uq_user_slot'),
    )

def get_current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None

def _tmdb_runtime_genres(data):
    """Extract runtime (int|None) and genres (comma-separated str|None) from a TMDB response dict."""
    runtime = data.get('runtime') or None
    genre_list = [g.get('name') for g in (data.get('genres') or []) if g.get('name')]
    genres_str = ','.join(genre_list) if genre_list else None
    return runtime, genres_str


# vytvorenie DB (spusti raz) + lightweight migration: add user_id column if missing
with app.app_context():
    db.create_all()

    # Ensure movie.user_id exists in SQLite (safe non-destructive ALTER)
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('movie')")
        cols = [row[1] for row in cur.fetchall()]
        if 'user_id' not in cols:
            cur.execute("ALTER TABLE movie ADD COLUMN user_id INTEGER;")
            conn.commit()
        cur.close()
        conn.close()
    except Exception:
        # if anything goes wrong here, don't crash the app at startup; fall back to create_all only
        pass

    # Ensure movie.rewatch column exists (safe ALTER)
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('movie')")
        cols = [row[1] for row in cur.fetchall()]
        if 'rewatch' not in cols:
            cur.execute("ALTER TABLE movie ADD COLUMN rewatch BOOLEAN DEFAULT 0;")
            conn.commit()
        cur.close()
        conn.close()
    except Exception:
        # non-fatal
        pass

    # Fix any ratings outside the 1-5 range left by previous import bugs.
    # Old imports doubled the scale (stored 1-10); halve anything > 5 and clamp.
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE movie SET rating = MAX(1, MIN(5, CAST(rating / 2 AS INTEGER))) WHERE rating > 5")
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

    # Ensure movie.runtime and movie.genres columns exist (safe ALTER)
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('movie')")
        cols = [row[1] for row in cur.fetchall()]
        if 'runtime' not in cols:
            cur.execute("ALTER TABLE movie ADD COLUMN runtime INTEGER;")
            conn.commit()
        if 'genres' not in cols:
            cur.execute("ALTER TABLE movie ADD COLUMN genres VARCHAR(500);")
            conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

    # Ensure there is a uniqueness constraint/index to prevent duplicate entries per user/movie/category.
    # If duplicates exist, attempt to deduplicate (keep the lowest id) and then create the unique index.
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()

        # find duplicate groups
        cur.execute("""
            SELECT user_id, movie_id, category, MIN(id) as keep_id
            FROM movie
            WHERE user_id IS NOT NULL
            GROUP BY user_id, movie_id, category
            HAVING COUNT(*) > 1
        """)
        dups = cur.fetchall()
        for row in dups:
            user_id, movie_id, category, keep_id = row
            # delete duplicates keeping the one with keep_id
            cur.execute(
                "DELETE FROM movie WHERE user_id = ? AND movie_id = ? AND category = ? AND id <> ?",
                (user_id, movie_id, category, keep_id)
            )

        conn.commit()

        # create unique index (if supported). This will prevent future duplicates at DB level.
        try:
            # Create a unique index for watchlist entries only so users can't add the same movie to watchlist twice
            # SQLite supports partial indexes using WHERE in modern versions.
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_user_movie_watchlist ON movie (user_id, movie_id) WHERE category = 'watchlist';")
            conn.commit()
        except Exception:
            # ignore if for some reason index creation fails (older SQLite)
            pass

        cur.close()
        conn.close()
    except Exception:
        # non-fatal
        pass
    # Defensive cleanup: if there is an accidental UNIQUE index covering (user_id, movie_id, category)
    # drop it so we can allow multiple 'diary' entries (rewatches). This scans sqlite indexes and
    # removes any unique index whose columns exactly match those three columns.
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        # get list of indexes
        cur.execute("PRAGMA index_list('movie')")
        indexes = cur.fetchall() or []
        for idx in indexes:
            # idx row: (seq, name, unique, origin, partial)
            name = idx[1]
            unique = idx[2]
            if not unique:
                continue
            # inspect index columns
            cur.execute(f"PRAGMA index_info('{name}')")
            cols = [r[2] for r in cur.fetchall() or []]
            # normalize
            cols_set = set(cols)
            if cols_set == set(['user_id', 'movie_id', 'category']):
                try:
                    cur.execute(f"DROP INDEX IF EXISTS '{name}'")
                    conn.commit()
                    app.logger.info("Dropped conflicting unique index %s on movie(%s)", name, cols)
                except Exception:
                    app.logger.exception("Failed to drop index %s", name)
        cur.close()
        conn.close()
    except Exception:
        # non-fatal
        pass


@app.route("/")
def home():
    user = get_current_user()
    return render_template("index.html", user=user)


# 🔍 SEARCH
@app.route("/search")
def search():
    query = request.args.get("q")
    if not query:
        return jsonify({"results": [], "page": 1, "total_pages": 0})

    # detect optional year suffix in the query (e.g. "Wall-E 2008") and strip it before querying TMDB
    import re
    year_hint = None
    title_search = (query or "").strip()
    m = re.search(r"(\b|\s)(19\d{2}|20\d{2})$", title_search)
    if m:
        year_hint = m.group(2)
        # strip year from title search for matching and for the TMDB query
        title_search = title_search[:m.start()].strip()

    # allow client to request additional pages from TMDB
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1

    # use the stripped title_search for the TMDB query if present, otherwise fall back to original query
    search_term = title_search if title_search else query
    url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": API_KEY, "query": search_term, "page": page}
    # if a year was supplied, include it in the TMDB query to bias/filter results
    if year_hint:
        params["year"] = year_hint
    response = requests.get(url, params=params)
    data = response.json()

    results = []
    for movie in data.get("results", []):
        poster_path = movie.get('poster_path')
        movie_year = (movie.get('release_date') or '')[:4]
        results.append({
            "id": movie.get("id"),
            "title": movie.get("title"),
            "year": movie_year,
            "poster": f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None
        })

    # scoring: prefer exact title + year, then exact title, then prefix, then contains
    def score_item(item):
        t = (item.get('title') or '').lower()
        q = (title_search or '').lower()
        # default score (higher is worse)
        s = 100
        if q:
            # exact title and year match (best)
            if t == q and year_hint and item.get('year') == year_hint:
                s = -10
            # exact title
            elif t == q:
                s = 0
            # prefix match
            elif t.startswith(q):
                s = 10
            # contains
            elif q in t:
                s = 20
            else:
                s = 50
        else:
            # no specific title search, keep TMDB order (use popularity fallback)
            s = 50

        # small boost if year matches
        if year_hint and item.get('year') == year_hint:
            s -= 5

        return s

    # sort by our score then keep original order as tiebreaker
    results_sorted = sorted(results, key=lambda it: (score_item(it)))

    return jsonify({
        "results": results_sorted,
        "page": data.get('page', page),
        "total_pages": data.get('total_pages', 1)
    })


# 🎬 DETAIL
@app.route("/movie/<int:movie_id>")
def movie_detail(movie_id):
    # Include credits and videos so we can show director, cast and a trailer link
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}&append_to_response=credits,videos,images&include_image_language=en,null"

    response = requests.get(url)
    data = response.json()

    # Director
    director = "Unknown"
    director_info = None
    for person in data.get("credits", {}).get("crew", []):
        if person.get("job") == "Director":
            director = person.get("name")
            dp = person.get('profile_path')
            director_info = {
                'id': person.get('id'),
                'name': person.get('name'),
                'profile': (f"https://image.tmdb.org/t/p/w185{dp}" if dp else None)
            }
            break

    # Runtime (minutes) and genres
    runtime = data.get('runtime')
    genres = [g.get('name') for g in data.get('genres', [])]

    # Full cast ordered by TMDB 'order' (most prominent first)
    cast_raw = data.get('credits', {}).get('cast', []) or []
    cast_raw_sorted = sorted(cast_raw, key=lambda c: c.get('order', 9999))
    top_cast = []
    for c in cast_raw_sorted[:6]:
        profile = c.get('profile_path')
        top_cast.append({
            'id': c.get('id'),
            'name': c.get('name'),
            'character': c.get('character'),
            'profile': (f"https://image.tmdb.org/t/p/w185{profile}" if profile else None)
        })
    full_cast = []
    for c in cast_raw_sorted:
        profile = c.get('profile_path')
        full_cast.append({
            'id': c.get('id'),
            'name': c.get('name'),
            'character': c.get('character'),
            'profile': (f"https://image.tmdb.org/t/p/w185{profile}" if profile else None)
        })

    # Collect all YouTube trailers/teasers
    all_videos = data.get('videos', {}).get('results', []) or []
    trailer_key = None
    for v in all_videos:
        if v.get('site') == 'YouTube' and v.get('type') == 'Trailer':
            trailer_key = v.get('key')
            break
    if not trailer_key:
        for v in all_videos:
            if v.get('site') == 'YouTube' and v.get('type') in ('Teaser', 'Clip'):
                trailer_key = v.get('key')
                break

    # Collect all posters and backdrops from TMDB images
    raw_posters = data.get('images', {}).get('posters', []) or []
    raw_backdrops = data.get('images', {}).get('backdrops', []) or []
    # Sort by vote_average desc, include default poster_path first if not in list
    default_poster_path = data.get('poster_path')
    default_backdrop_path = data.get('backdrop_path')

    # Filter posters: English or no language (null), sorted by vote_average desc
    ALLOWED_LANGS = {'en', 'xx', None, ''}
    poster_paths = []
    seen_p = set()
    if default_poster_path:
        poster_paths.append(default_poster_path)
        seen_p.add(default_poster_path)
    # Sort: first by language preference (en/null first), then by vote_average desc
    def poster_sort_key(p):
        lang = p.get('iso_639_1') or ''
        lang_rank = 0 if lang in ('en', 'xx', '') else 1
        return (lang_rank, -(p.get('vote_average') or 0), -(p.get('vote_count') or 0))
    for p in sorted(raw_posters, key=poster_sort_key):
        lang = p.get('iso_639_1') or ''
        if lang not in ALLOWED_LANGS and lang not in ('en', 'xx', ''):
            continue  # skip non-English
        fp = p.get('file_path')
        if fp and fp not in seen_p:
            poster_paths.append(fp)
            seen_p.add(fp)

    backdrop_paths = []
    seen_b = set()
    if default_backdrop_path:
        backdrop_paths.append(default_backdrop_path)
        seen_b.add(default_backdrop_path)
    for b in sorted(raw_backdrops, key=lambda x: -(x.get('vote_average') or 0)):
        fp = b.get('file_path')
        if fp and fp not in seen_b:
            backdrop_paths.append(fp)
            seen_b.add(fp)

    movie = {
        "id": data.get("id"),
        "title": data.get("title"),
        "year": (data.get("release_date") or "")[:4],
        "overview": data.get("overview"),
        "director": director,
        "director_info": director_info,
        "poster": (f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else None),
        "backdrop": (f"https://image.tmdb.org/t/p/w1280{data.get('backdrop_path')}" if data.get('backdrop_path') else None),
        "runtime": runtime,
        "genres": genres,
        "cast": top_cast,
        "full_cast": full_cast,
        "trailer_key": trailer_key,
        "poster_paths": poster_paths[:30],
        "backdrop_paths": backdrop_paths[:15],
        "imdb_id": data.get("imdb_id"),
        "tmdb_vote_average": data.get("vote_average"),
        "tmdb_vote_count": data.get("vote_count"),
    }

    # Movieland community rating: average of all diary entries with a rating for this movie
    from sqlalchemy import func as sql_func
    ml_row = db.session.query(
        sql_func.avg(Movie.rating).label('avg'),
        sql_func.count(Movie.rating).label('cnt')
    ).filter(
        Movie.movie_id == movie_id,
        Movie.category == 'diary',
        Movie.rating != None
    ).one()
    movie['ml_avg_rating'] = round(float(ml_row.avg), 2) if ml_row.avg is not None else None
    movie['ml_rating_count'] = ml_row.cnt or 0

    # Per-user flags so the template can show the correct button states
    user = get_current_user()
    in_watchlist = False
    has_diary = False
    diary_id = None
    preferred_poster = None
    preferred_backdrop = None
    if user:
        w = Movie.query.filter_by(movie_id=movie_id, category='watchlist', user_id=user.id).first()
        if w:
            in_watchlist = True
        # Get all diary entries for this movie, newest first
        diary_entries = Movie.query.filter_by(movie_id=movie_id, category='diary', user_id=user.id).order_by(Movie.date.desc(), Movie.id.desc()).all()
        user_rating = None
        if diary_entries:
            has_diary = True
            diary_id = diary_entries[0].id
            # Use the most recent entry that has a rating
            for entry in diary_entries:
                if entry.rating is not None:
                    user_rating = entry.rating
                    break
        pref = MovieImagePref.query.filter_by(user_id=user.id, movie_id=movie_id).first()
        if pref:
            if pref.poster_path:
                preferred_poster = f"https://image.tmdb.org/t/p/w500{pref.poster_path}"
                movie['poster'] = preferred_poster
            if pref.backdrop_path:
                preferred_backdrop = f"https://image.tmdb.org/t/p/w1280{pref.backdrop_path}"
                movie['backdrop'] = preferred_backdrop

    pref = MovieImagePref.query.filter_by(user_id=user.id, movie_id=movie_id).first() if user else None
    user_rating = user_rating if user else None
    return render_template("movie.html", movie=movie, in_watchlist=in_watchlist, has_diary=has_diary, diary_id=diary_id, pref=pref, user_rating=user_rating)



# 🎭 PERSON DETAIL (for cast/director modal)
@app.route("/api/person/<int:person_id>")
def person_detail(person_id):
    url = f"https://api.themoviedb.org/3/person/{person_id}?api_key={API_KEY}&append_to_response=movie_credits"
    resp = requests.get(url)
    data = resp.json()

    profile_path = data.get('profile_path')
    all_movies = data.get('movie_credits', {}).get('cast', []) or []
    crew_directed = [m for m in (data.get('movie_credits', {}).get('crew', []) or [])
                     if m.get('job') == 'Director']
    all_movies = all_movies + crew_directed
    seen_ids = set()
    unique_movies = []
    for m in sorted(all_movies, key=lambda x: x.get('popularity', 0), reverse=True):
        if m['id'] not in seen_ids:
            seen_ids.add(m['id'])
            unique_movies.append(m)

    def movie_obj(m):
        pp = m.get('poster_path')
        return {
            'id': m.get('id'),
            'title': m.get('title'),
            'year': (m.get('release_date') or '')[:4],
            'poster': f"https://image.tmdb.org/t/p/w185{pp}" if pp else None,
        }

    top_movies = [movie_obj(m) for m in unique_movies[:6]]
    all_movies_list = [movie_obj(m) for m in unique_movies]

    bio = data.get('biography') or ''

    return jsonify({
        'id': person_id,
        'name': data.get('name'),
        'profile': f"https://image.tmdb.org/t/p/w185{profile_path}" if profile_path else None,
        'bio': bio,
        'known_for': data.get('known_for_department'),
        'birthday': data.get('birthday'),
        'place_of_birth': data.get('place_of_birth'),
        'top_movies': top_movies,
        'all_movies': all_movies_list,
    })


# ➕ ADD TO LIST
@app.route("/add/<int:movie_id>/<category>")
def add_movie(movie_id, category):
    user = get_current_user()
    if not user:
        # If this is an AJAX request, return JSON 401 so the client can handle it deterministically
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': False, 'error': 'not_logged_in', 'redirect': url_for('login')}), 401
        flash('Please log in to add movies to your lists.')
        return redirect(url_for('login'))

    # validate category to avoid arbitrary values
    if category not in ALLOWED_CATEGORIES:
        app.logger.warning("Attempt to add movie to invalid category: %s", category)
        flash('Invalid list/category.')
        return redirect(request.referrer or url_for('home'))

    app.logger.debug("ADD MOVIE: user=%s movie_id=%s category=%s", user.id if user else None, movie_id, category)

    # support a force flag (query or form) to explicitly create duplicates when desired
    force = (request.args.get('force') == '1') or (request.form.get('force') == '1')
    app.logger.debug("ADD MOVIE: force=%s (args=%s form=%s)", force, request.args.get('force'), request.form.get('force'))

    # First, check whether the target category already contains the movie for this user
    existing_target = Movie.query.filter_by(movie_id=movie_id, category=category, user_id=user.id).first()
    if existing_target:
        app.logger.debug("movie already in target category: %s (existing_id=%s)", category, existing_target.id)
        # If diary already exists and force flag is present, create a new diary entry
        if category == 'diary' and force:
            # fetch TMDB details and create a new diary entry
            url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
            response = requests.get(url)
            data = response.json()

            new_movie = Movie(
                movie_id=movie_id,
                title=data.get("title"),
                category='diary',
                poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
                date=datetime.utcnow(),
                user_id=user.id
            )
            db.session.add(new_movie)
            db.session.commit()
            app.logger.debug("add_movie: force-created new diary id=%s for user=%s tmdb=%s", new_movie.id, user.id, movie_id)
            edit_url = url_for('edit_review', id=new_movie.id)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"added": True, "moved": False, "movie_db_id": new_movie.id, "movie_id": new_movie.movie_id, "redirect": edit_url})
            return redirect(edit_url)

        # If diary already exists, send user to edit that review
        if category == 'diary':
            edit_url = url_for('edit_review', id=existing_target.id)
            app.logger.debug("add_movie: diary exists -> returning edit for existing id=%s", existing_target.id)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"added": False, "exists": True, "redirect": edit_url, "movie_db_id": existing_target.id})
            return redirect(edit_url)

        # For other categories, simply return that it's present
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"added": False, "exists": True, "movie_db_id": existing_target.id, "movie_id": existing_target.movie_id})
        return redirect(request.referrer or url_for('home'))

    # Special handling for adding to diary: avoid creating a diary entry here if not necessary.
    if category == 'diary':
        # If the movie exists in watchlist, move it to diary and send the user to edit that record
        existing_watch = Movie.query.filter_by(movie_id=movie_id, category='watchlist', user_id=user.id).first()
        if existing_watch:
            existing_watch.category = 'diary'
            existing_watch.date = datetime.utcnow()
            db.session.commit()
            app.logger.debug("add_movie: moved watchlist id=%s to diary for user=%s tmdb=%s", existing_watch.id, user.id, movie_id)
            edit_url = url_for('edit_review', id=existing_watch.id)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"added": True, "moved": True, "movie_db_id": existing_watch.id, "movie_id": existing_watch.movie_id, "redirect": edit_url})
            return redirect(edit_url)

        # Not present anywhere for this user yet -> redirect user to the review page so they can create the diary entry
        review_url = url_for('review_page', movie_id=movie_id)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"added": True, "redirect": review_url})
        return redirect(review_url)

    # For non-diary categories (e.g., watchlist) create the movie record if missing
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()

    _rt, _genres = _tmdb_runtime_genres(data)
    movie = Movie(
        movie_id=movie_id,
        title=data.get("title"),
        category=category,
        poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
        user_id=user.id,
        runtime=_rt,
        genres=_genres
    )

    db.session.add(movie)
    db.session.commit()
    app.logger.debug("movie added id=%s", movie.id)

    # if called via AJAX, return JSON so client can handle it
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({"added": True, "movie_db_id": movie.id, "movie_id": movie.movie_id})

    # non-AJAX fallback: redirect back to referrer or home
    return redirect(request.referrer or url_for('home'))


# 📚 WATCHLIST
@app.route("/watchlist")
def watchlist():
    user = get_current_user()
    if not user:
        flash('Please log in to view your Watchlist.')
        return redirect(url_for('login'))

    movies = Movie.query.filter_by(category="watchlist", user_id=user.id).order_by(Movie.date.desc()).all()

    # Load all image prefs for this user in one query
    prefs = {p.movie_id: p for p in MovieImagePref.query.filter_by(user_id=user.id).all()}

    # group by month (reuse same template structure as diary)
    from collections import OrderedDict
    groups = OrderedDict()
    for m in movies:
        p = prefs.get(m.movie_id)
        if p and p.poster_path:
            m.poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}"
        key = m.date.strftime("%B %Y")
        groups.setdefault(key, []).append(m)

    groups_list = list(groups.items())
    return render_template("list.html", groups=groups_list, title="Watchlist")


# 📓 DIARY
@app.route("/diary")
def diary():
    user = get_current_user()
    if not user:
        flash('Please log in to view your Diary.')
        return redirect(url_for('login'))

    movies = Movie.query.filter_by(category="diary", user_id=user.id).order_by(Movie.date.desc()).all()

    # Load all image prefs for this user in one query
    prefs = {p.movie_id: p for p in MovieImagePref.query.filter_by(user_id=user.id).all()}

    # Build rewatch siblings map: entry.id -> [other diary entries for same film]
    from collections import OrderedDict, defaultdict
    all_entries_by_movie = defaultdict(list)
    for m in movies:
        all_entries_by_movie[m.movie_id].append(m)
    rewatches_map = {}
    for movie_id, entries in all_entries_by_movie.items():
        if len(entries) > 1:
            for e in entries:
                rewatches_map[e.id] = [s for s in entries if s.id != e.id]

    # group movies by month (e.g. "April 2026") in display order
    groups = OrderedDict()
    for m in movies:
        p = prefs.get(m.movie_id)
        if p and p.poster_path:
            m.poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}"
        key = m.date.strftime("%B %Y")
        groups.setdefault(key, []).append(m)

    # convert to list of (month, movies) tuples to preserve order in templates
    groups_list = list(groups.items())

    return render_template("list.html", groups=groups_list, title="Diary", rewatches_map=rewatches_map)

@app.route("/delete/<int:id>")
def delete_movie(id):
    user = get_current_user()
    movie = Movie.query.get_or_404(id)
    if not user or movie.user_id != user.id:
        abort(403)

    db.session.delete(movie)
    db.session.commit()

    return redirect(request.referrer or url_for('home'))

@app.route("/move/<int:id>")
def move_movie(id):
    user = get_current_user()
    movie = Movie.query.get_or_404(id)
    if not user or movie.user_id != user.id:
        abort(403)
    # If moving from watchlist -> diary: change category and open the review editor
    if movie.category == "watchlist":
        movie.category = "diary"
        # update date to now to reflect when it was moved into the diary
        movie.date = datetime.utcnow()
        db.session.commit()
        # redirect the user to edit the newly-moved diary entry
        return redirect(url_for('edit_review', id=movie.id))

    # Otherwise toggle back to watchlist
    movie.category = "watchlist"
    db.session.commit()
    return redirect(request.referrer or url_for('home'))
@app.route("/review/<int:movie_id>")
def review_page(movie_id):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()

    movie = {
        "id": data.get("id"),
        "title": data.get("title"),
        "poster": (f"https://image.tmdb.org/t/p/w300{data.get('poster_path')}" if data.get('poster_path') else None)
    }

    user = get_current_user()
    existing_diary = None
    if user:
        existing_diary = Movie.query.filter_by(movie_id=movie_id, category='diary', user_id=user.id).first()

        # If the client previously set an intent to 'log again' (via /intent/log_again), honor it here by
        # creating a new diary entry automatically and redirecting the user to edit that new entry.
        try:
            key = f'log_again_for_{user.id}_{movie_id}'
            if session.get(key):
                # clear the intent flag so it doesn't apply again
                session.pop(key, None)
                session.modified = True
                app.logger.debug('review_page: detected session log_again key=%s for user=%s; creating new diary entry', key, user.id)
                # fetch TMDB details for title/poster
                url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
                resp = requests.get(url)
                data = resp.json()
                new_movie = Movie(
                    movie_id=movie_id,
                    title=data.get('title'),
                    category='diary',
                    poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
                    date=datetime.utcnow(),
                    user_id=user.id
                )
                db.session.add(new_movie)
                db.session.commit()
                app.logger.debug('review_page: created forced diary id=%s for user=%s', new_movie.id, user.id)
                return redirect(url_for('edit_review', id=new_movie.id))
        except Exception:
            # if anything goes wrong creating the forced diary, fall back to showing the review page
            app.logger.exception('review_page: failed to auto-create forced diary entry')

    # respect a query param that pre-checks the "Log again" option
    allow_duplicate_prefill = (request.args.get('allow_duplicate') == '1')

    return render_template("review.html", movie=movie, existing_diary=existing_diary, allow_duplicate=allow_duplicate_prefill)


@app.route("/rewatch/<int:movie_id>")
def rewatch_page(movie_id):
    # show a dedicated rewatch page where a new diary entry (rewatch) can be created
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()

    movie = {
        "id": data.get("id"),
        "title": data.get("title"),
        "poster": (f"https://image.tmdb.org/t/p/w300{data.get('poster_path')}" if data.get('poster_path') else None)
    }

    return render_template("rewatch.html", movie=movie)


@app.route("/save_rewatch/<int:movie_id>", methods=["POST"])
def save_rewatch(movie_id):
    # Save a new diary entry for a rewatch — always create a new diary record even if one exists
    user = get_current_user()
    if not user:
        flash('Please log in to save a rewatch.')
        return redirect(url_for('login'))

    review_text = request.form.get("review")
    rating = request.form.get("rating")
    liked = True if request.form.get("liked") == "on" else False

    # fetch TMDB details for title/poster
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()

    _rt, _genres = _tmdb_runtime_genres(data)
    movie = Movie(
        movie_id=movie_id,
        title=data.get("title"),
        category="diary",
        poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
        review=review_text,
        rating=(int(rating) if rating and rating.strip() and rating.lstrip('-').isdigit() else None),
        liked=liked,
        date=datetime.utcnow(),
        rewatch=True,
        user_id=user.id,
        runtime=_rt,
        genres=_genres
    )

    # Defensive: if there is a unique index accidentally preventing multiple diary rows,
    # try to drop it now so the insert can succeed. This mirrors the startup cleanup but
    # ensures rewatch creation works even without restarting the app.
    try:
        conn = db.engine.raw_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA index_list('movie')")
        for idx in (cur.fetchall() or []):
            name = idx[1]
            unique = idx[2]
            if not unique:
                continue
            cur.execute(f"PRAGMA index_info('{name}')")
            cols = [r[2] for r in cur.fetchall() or []]
            if set(cols) == set(['user_id', 'movie_id', 'category']):
                try:
                    cur.execute(f"DROP INDEX IF EXISTS '{name}'")
                    conn.commit()
                    app.logger.info("Dropped conflicting unique index %s before rewatch insert", name)
                except Exception:
                    app.logger.exception("Failed to drop index %s", name)
        cur.close(); conn.close()
    except Exception:
        # non-fatal; continue and let commit surface any remaining errors
        app.logger.exception('Error while attempting to drop conflicting indexes')

    db.session.add(movie)
    db.session.commit()

    return redirect(url_for('diary'))


@app.route('/intent/log_again', methods=['POST'])
def intent_log_again():
    """Set a short-lived session flag that the user intends to 'log again' for a specific movie.
    This is useful when the client navigates to /review and we want the server to honor creating a new diary entry.
    """
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401

    data = request.get_json() or {}
    try:
        movie_id = int(data.get('movie_id'))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400

    # store intent in session under a per-user key, expire on next save
    key = f'log_again_for_{user.id}_{movie_id}'
    session[key] = True
    # mark session modified so Flask saves the cookie
    session.modified = True
    app.logger.debug("intent_log_again set session key=%s for user=%s; session_keys=%s; cookie=%s", key, user.id, list(session.keys()), request.cookies.get(app.session_cookie_name))
    return jsonify({'ok': True})


@app.route('/add_force/<int:movie_id>/diary', methods=['POST'])
def add_force_diary(movie_id):
    """Create a new diary entry for the current user for movie_id even if one exists.
    Returns JSON with movie_db_id and redirect to edit page so client can open the editor immediately.
    """
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in', 'redirect': url_for('login')}), 401

    # fetch TMDB details for title/poster
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    resp = requests.get(url)
    data = resp.json()

    movie = Movie(
        movie_id=movie_id,
        title=data.get('title'),
        category='diary',
        poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
        date=datetime.utcnow(),
        rewatch=True,
        user_id=user.id
    )

    db.session.add(movie)
    db.session.commit()

    app.logger.debug("add_force: created diary movie id=%s for user=%s (tmdb=%s)", movie.id, user.id, movie.movie_id)
    edit_url = url_for('edit_review', id=movie.id)
    return jsonify({'ok': True, 'movie_db_id': movie.id, 'redirect': edit_url})


@app.route('/debug/movies/<int:movie_id>')
def debug_movies_for_user(movie_id):
    """Diagnostic: return the current user's Movie rows for the given TMDB movie_id.
    This is safe for local debugging only; do NOT enable in production or commit with open access.
    """
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401

    rows = Movie.query.filter_by(movie_id=movie_id, user_id=user.id).order_by(Movie.id.asc()).all()
    out = []
    for r in rows:
        out.append({
            'id': r.id,
            'movie_id': r.movie_id,
            'title': r.title,
            'category': r.category,
            'date': r.date.isoformat() if r.date else None,
            'review': (r.review[:100] + '...') if r.review and len(r.review) > 100 else r.review
        })
    return jsonify({'ok': True, 'rows': out})

@app.route("/save_review/<int:id>", methods=["POST"])
def save_review(id):
    app.logger.debug("save_review form: %s", request.form)

    review_text = request.form.get("review")
    rating = request.form.get("rating")

    url = f"https://api.themoviedb.org/3/movie/{id}?api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()

    user = get_current_user()
    if not user:
        flash('Please log in to save a review.')
        return redirect(url_for('login'))

    # Check if a diary entry exists for this user/movie
    existing = Movie.query.filter_by(movie_id=id, category="diary", user_id=user.id).first()

    # support allow_duplicate either from form POST or query string (when review page was opened with ?allow_duplicate=1)
    allow_duplicate = (request.form.get('allow_duplicate') == '1') or (request.args.get('allow_duplicate') == '1')

    # Fallback: some browsers may not include the form value; inspect the referrer URL for the allow_duplicate query param
    if not allow_duplicate and request.referrer:
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(request.referrer)
            qs = parse_qs(parsed.query)
            if qs.get('allow_duplicate') and '1' in qs.get('allow_duplicate'):
                allow_duplicate = True
        except Exception:
            pass

    # Finally, check for a session flag set by the client when the user explicitly chose "Log again"
    try:
        key = f'log_again_for_{user.id}_{id}'
        if not allow_duplicate and session.get(key):
            allow_duplicate = True
            # clear the intent flag so it doesn't apply again
            session.pop(key, None)
            session.modified = True
            app.logger.debug("save_review: detected session log_again key=%s for user=%s; allow_duplicate now %s; session_keys=%s; cookie=%s", key, user.id, allow_duplicate, list(session.keys()), request.cookies.get(app.session_cookie_name))
    except Exception:
        pass

    if existing and not allow_duplicate:
        # if a diary exists and user did not explicitly allow duplicate, redirect to diary (or show edit)
        # Redirect to edit page for the existing diary entry
        return redirect(url_for('edit_review', id=existing.id))

    # either no existing diary or duplicates allowed -> create a new diary entry
    _rt, _genres = _tmdb_runtime_genres(data)
    movie = Movie(
        movie_id=id,
        title=data.get("title"),
        category="diary",
        poster=(f"https://image.tmdb.org/t/p/w200{data.get('poster_path')}" if data.get('poster_path') else None),
        review=review_text,

        # safe numeric parse for rating
        rating=(int(rating) if rating and rating.strip() and rating.lstrip('-').isdigit() else None),

        liked=True if request.form.get("liked") == "on" else False,
        user_id=user.id,
        runtime=_rt,
        genres=_genres
    )

    db.session.add(movie)
    try:
        db.session.commit()
    except IntegrityError:
        # If an integrity error occurs (rare race), try to find the existing diary/watchlist and redirect to edit
        db.session.rollback()
        existing_conflict = Movie.query.filter_by(movie_id=id, category="diary", user_id=user.id).first()
        if existing_conflict:
            return redirect(url_for('edit_review', id=existing_conflict.id))
        # fallback: redirect to diary
        return redirect(url_for('diary'))

    return redirect(url_for('diary'))

@app.route("/review_view/<int:id>")
def review_view(id):
    user = get_current_user()
    movie = Movie.query.get_or_404(id)
    if not user or movie.user_id != user.id:
        abort(403)
    return render_template("review_view.html", movie=movie)

@app.route("/edit_review/<int:id>")
def edit_review(id):
    user = get_current_user()
    movie = Movie.query.get_or_404(id)
    if not user or movie.user_id != user.id:
        abort(403)
    return render_template("edit_review.html", movie=movie)

@app.route("/update_review/<int:id>", methods=["POST"])
def update_review(id):
    user = get_current_user()
    movie = Movie.query.get_or_404(id)
    if not user or movie.user_id != user.id:
        abort(403)

    movie.review = request.form.get("review")

    # ⭐ THIS MUST BE HERE
    rating = request.form.get("rating")
    try:
        movie.rating = int(rating) if rating and rating.strip() else None
    except ValueError:
        movie.rating = None

    # ❤️ like
    movie.liked = True if request.form.get("liked") == "on" else False

    db.session.commit()

    return redirect(url_for('diary'))

@app.route("/register", methods=["GET", "POST"])
def register():
    import re

    # Slur/bad-word list — extend as needed
    BANNED_WORDS = [
        "nigger","nigga","faggot","fag","kike","spic","chink","gook",
        "tranny","retard","cunt","whore","slut","bastard","bitch",
        "asshole","motherfucker","fuckface","dickhead","shithead",
        "nazi","kkk","wetback","cracker","honky","beaner","zipperhead",
    ]

    def contains_slur(text):
        lower = text.lower()
        for word in BANNED_WORDS:
            if word in lower:
                return True
        return False

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        # --- Username validations ---
        if len(username) < 3:
            flash("Username must be at least 3 characters.")
            return render_template("register.html")
        if len(username) > 20:
            flash("Username must be 20 characters or fewer.")
            return render_template("register.html")
        if " " in username:
            flash("Username cannot contain spaces.")
            return render_template("register.html")
        if not re.match(r'^[A-Za-z0-9_.-]+$', username):
            flash("Username may only contain letters, numbers, underscores, hyphens, and dots.")
            return render_template("register.html")
        if contains_slur(username):
            flash("Username contains a prohibited word.")
            return render_template("register.html")
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("That username is already taken.")
            return render_template("register.html")

        # --- Password validations ---
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("register.html")
        if password != confirm:
            flash("Passwords do not match.")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)

        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()

        # Auto-login the user after successful registration
        session["user_id"] = new_user.id
        flash(f"Welcome, {new_user.username}!")
        # Send new users to the home page (consistent with login)
        return redirect(url_for("home"))

    # GET -> render registration form
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            return redirect(url_for("home"))
        else:
            flash("Invalid credentials")
            return render_template("login.html")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route('/profile')
def profile():
    user = get_current_user()
    if not user:
        flash('Please log in to view your profile.')
        return redirect(url_for('login'))

    # last 4 diary movies for this user
    movies = Movie.query.filter_by(category='diary', user_id=user.id).order_by(Movie.date.desc()).limit(4).all()

    # Load ALL image prefs for this user in a single query
    prefs = {p.movie_id: p for p in MovieImagePref.query.filter_by(user_id=user.id).all()}

    # Apply poster prefs to recent diary movies
    for m in movies:
        p = prefs.get(m.movie_id)
        if p and p.poster_path:
            m.poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}"

    # load top4 slots (1..4) for this user
    slots = Top4Slot.query.filter_by(user_id=user.id).order_by(Top4Slot.slot).all()
    # convert to dict by slot for easy template access, include linked diary rating when available
    top4 = {}
    for s in slots:
        # try to find an existing diary Movie record for this user with the same TMDB movie_id to surface rating
        linked = Movie.query.filter_by(movie_id=s.movie_id, user_id=user.id, category='diary').first()
        rating = linked.rating if linked else None
        # apply user's preferred poster if set
        p = prefs.get(s.movie_id)
        poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}" if p and p.poster_path else s.poster
        top4[s.slot] = {
            'movie_id': s.movie_id,
            'title': s.title,
            'poster': poster,
            'rating': rating
        }

    return render_template('profile.html', user=user, movies=movies, top4=top4)


@app.route('/profile/update', methods=['POST'])
def profile_update():
    user = get_current_user()
    if not user:
        return jsonify({'success': False}), 401
    data = request.get_json()
    if 'bio' in data:
        user.bio = data['bio'][:300]
    db.session.commit()
    return jsonify({'success': True})


@app.route('/profile/avatar', methods=['POST'])
def profile_avatar():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'not logged in'}), 401
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': 'no file'}), 400
    f = request.files['avatar']
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        return jsonify({'success': False, 'error': 'bad file type'}), 400
    # Delete old avatar if it exists
    if user.avatar_path:
        old = os.path.join(app.config['AVATAR_FOLDER'], os.path.basename(user.avatar_path))
        if os.path.exists(old):
            os.remove(old)
    filename = f'{user.id}_{uuid.uuid4().hex[:8]}.{ext}'
    save_path = os.path.join(app.config['AVATAR_FOLDER'], filename)
    f.save(save_path)
    user.avatar_path = f'avatars/{filename}'
    db.session.commit()
    return jsonify({'success': True, 'url': f'/static/avatars/{filename}'})


@app.route('/profile/top4/set', methods=['POST'])
def set_top4_slot():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "not_logged_in"}), 401

    data = request.get_json() or {}
    try:
        slot = int(data.get('slot'))
        movie_id = int(data.get('movie_id'))
    except Exception:
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    if slot < 1 or slot > 4:
        return jsonify({"success": False, "error": "invalid_slot"}), 400

    # fetch TMDB details for the chosen movie to get title/poster
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    resp = requests.get(url)
    data = resp.json()

    title = data.get('title')
    poster_path = data.get('poster_path')
    poster = f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None

    # upsert slot
    existing = Top4Slot.query.filter_by(user_id=user.id, slot=slot).first()
    if existing:
        existing.movie_id = movie_id
        existing.title = title
        existing.poster = poster
    else:
        new = Top4Slot(user_id=user.id, slot=slot, movie_id=movie_id, title=title, poster=poster)
        db.session.add(new)

    db.session.commit()

    return jsonify({"success": True, "slot": slot, "movie_id": movie_id, "title": title, "poster": poster})


if __name__ == "__main__":
    # Run the app when executed directly. Keep debug mode local only.
    app.run(debug=True)

# ============================================================
# LISTS FEATURE
# ============================================================

class UserList(db.Model):
    """A named movie list belonging to a user. Can be normal or ranked."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    ranked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    entries = db.relationship('UserListEntry', backref='list', lazy=True,
                              cascade='all, delete-orphan',
                              order_by='UserListEntry.position')


class UserListEntry(db.Model):
    """A single movie entry in a user list, with an optional position for ranked lists."""
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Integer, db.ForeignKey('user_list.id'), nullable=False)
    movie_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(300))
    poster = db.Column(db.String(300))
    position = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint('list_id', 'movie_id', name='uq_list_movie'),
    )


# Run migrations for new tables
with app.app_context():
    db.create_all()


@app.route('/lists')
def lists_page():
    user = get_current_user()
    if not user:
        flash('Please log in to view your lists.')
        return redirect(url_for('login'))
    user_lists = UserList.query.filter_by(user_id=user.id).order_by(UserList.updated_at.desc()).all()
    return render_template('lists.html', user=user, user_lists=user_lists)


@app.route('/lists/create', methods=['POST'])
def create_list():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name_required'}), 400
    ranked = bool(data.get('ranked', False))
    description = (data.get('description') or '').strip()
    ul = UserList(user_id=user.id, name=name, ranked=ranked, description=description)
    db.session.add(ul)
    db.session.commit()
    return jsonify({'ok': True, 'list_id': ul.id, 'name': ul.name, 'ranked': ul.ranked})


@app.route('/lists/<int:list_id>')
def view_list(list_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        abort(403)
    return render_template('list_view.html', user=user, ul=ul)


@app.route('/lists/<int:list_id>/add', methods=['POST'])
def add_to_list(list_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    data = request.get_json() or {}
    try:
        movie_id = int(data.get('movie_id'))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid_movie_id'}), 400

    # check duplicate
    existing = UserListEntry.query.filter_by(list_id=list_id, movie_id=movie_id).first()
    if existing:
        return jsonify({'ok': False, 'error': 'already_in_list', 'entry_id': existing.id})

    # fetch TMDB info
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={API_KEY}"
    resp = requests.get(url)
    mdata = resp.json()
    title = mdata.get('title')
    poster_path = mdata.get('poster_path')
    poster = f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None

    # position = current count
    max_pos = db.session.query(db.func.max(UserListEntry.position)).filter_by(list_id=list_id).scalar() or 0
    entry = UserListEntry(list_id=list_id, movie_id=movie_id, title=title, poster=poster, position=max_pos + 1)
    db.session.add(entry)

    # touch updated_at on the list
    ul.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'entry_id': entry.id, 'movie_id': movie_id, 'title': title, 'poster': poster, 'position': entry.position})


@app.route('/lists/<int:list_id>/remove/<int:entry_id>', methods=['POST'])
def remove_from_list(list_id, entry_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    entry = UserListEntry.query.get_or_404(entry_id)
    if entry.list_id != list_id:
        return jsonify({'ok': False, 'error': 'not_in_list'}), 400
    db.session.delete(entry)
    ul.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/lists/<int:list_id>/reorder', methods=['POST'])
def reorder_list(list_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    data = request.get_json() or {}
    order = data.get('order', [])  # list of entry ids in new order
    for i, entry_id in enumerate(order, start=1):
        entry = UserListEntry.query.get(entry_id)
        if entry and entry.list_id == list_id:
            entry.position = i
    ul.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/lists/<int:list_id>/delete', methods=['POST'])
def delete_list(list_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    db.session.delete(ul)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/lists/<int:list_id>/edit', methods=['POST'])
def edit_list(list_id):
    user = get_current_user()
    ul = UserList.query.get_or_404(list_id)
    if not user or ul.user_id != user.id:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if name:
        ul.name = name
    if 'description' in data:
        ul.description = (data.get('description') or '').strip()
    if 'ranked' in data:
        ul.ranked = bool(data.get('ranked'))
    ul.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


# ============================================================
# MOVIE IMAGE PREFERENCES
# ============================================================

class MovieImagePref(db.Model):
    """Stores a user's preferred poster and backdrop path (TMDB file_path) for a movie."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    movie_id = db.Column(db.Integer, nullable=False)
    poster_path = db.Column(db.String(300))    # TMDB file_path e.g. /abc.jpg
    backdrop_path = db.Column(db.String(300))  # TMDB file_path

    __table_args__ = (
        db.UniqueConstraint('user_id', 'movie_id', name='uq_user_movie_pref'),
    )


with app.app_context():
    db.create_all()


@app.route('/movie/<int:movie_id>/set_image_pref', methods=['POST'])
def set_image_pref(movie_id):
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    data = request.get_json() or {}
    poster_path = data.get('poster_path')    # TMDB file_path or None
    backdrop_path = data.get('backdrop_path')

    pref = MovieImagePref.query.filter_by(user_id=user.id, movie_id=movie_id).first()
    if not pref:
        pref = MovieImagePref(user_id=user.id, movie_id=movie_id)
        db.session.add(pref)

    if poster_path is not None:
        pref.poster_path = poster_path
        new_poster_thumb = f"https://image.tmdb.org/t/p/w200{poster_path}"
        # Update all Movie rows (diary/watchlist) for this user+movie
        for m in Movie.query.filter_by(user_id=user.id, movie_id=movie_id).all():
            m.poster = new_poster_thumb
        # Update Top4Slot if this movie is in user's top4
        for slot in Top4Slot.query.filter_by(user_id=user.id, movie_id=movie_id).all():
            slot.poster = new_poster_thumb
    if backdrop_path is not None:
        pref.backdrop_path = backdrop_path

    db.session.commit()

    result = {}
    if poster_path is not None:
        result['poster_url'] = f"https://image.tmdb.org/t/p/w500{poster_path}"
    if backdrop_path is not None:
        result['backdrop_url'] = f"https://image.tmdb.org/t/p/w1280{backdrop_path}"

    return jsonify({'ok': True, **result})

# ============================================================
# FRIENDS & RECOMMENDATIONS
# ============================================================

class Friendship(db.Model):
    """Directed friendship request. status: 'pending' or 'accepted'."""
    id         = db.Column(db.Integer, primary_key=True)
    from_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    to_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status     = db.Column(db.String(20), default='pending')   # pending | accepted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('from_id', 'to_id', name='uq_friendship'),
    )

with app.app_context():
    db.create_all()


def get_friends(user_id):
    """Return list of User objects that are accepted friends of user_id."""
    accepted = Friendship.query.filter(
        ((Friendship.from_id == user_id) | (Friendship.to_id == user_id)),
        Friendship.status == 'accepted'
    ).all()
    friend_ids = []
    for f in accepted:
        friend_ids.append(f.to_id if f.from_id == user_id else f.from_id)
    return User.query.filter(User.id.in_(friend_ids)).all() if friend_ids else []


@app.route('/friends/search')
def friends_search():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'users': []})
    results = User.query.filter(
        User.username.ilike(f'%{q}%'),
        User.id != user.id
    ).limit(10).all()
    out = []
    for u in results:
        f = Friendship.query.filter(
            ((Friendship.from_id == user.id) & (Friendship.to_id == u.id)) |
            ((Friendship.from_id == u.id) & (Friendship.to_id == user.id))
        ).first()
        status = f.status if f else None
        direction = None
        if f:
            direction = 'sent' if f.from_id == user.id else 'received'
        out.append({'id': u.id, 'username': u.username, 'status': status, 'direction': direction})
    return jsonify({'users': out})


@app.route('/friends/request/<int:to_id>', methods=['POST'])
def send_friend_request(to_id):
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    if to_id == user.id:
        return jsonify({'ok': False, 'error': 'cannot_add_self'}), 400
    existing = Friendship.query.filter(
        ((Friendship.from_id == user.id) & (Friendship.to_id == to_id)) |
        ((Friendship.from_id == to_id) & (Friendship.to_id == user.id))
    ).first()
    if existing:
        return jsonify({'ok': False, 'error': 'already_exists', 'status': existing.status})
    f = Friendship(from_id=user.id, to_id=to_id, status='pending')
    db.session.add(f)
    db.session.commit()
    return jsonify({'ok': True, 'status': 'pending'})


@app.route('/friends/accept/<int:from_id>', methods=['POST'])
def accept_friend_request(from_id):
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    f = Friendship.query.filter_by(from_id=from_id, to_id=user.id, status='pending').first()
    if not f:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    f.status = 'accepted'
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/friends/decline/<int:from_id>', methods=['POST'])
def decline_friend_request(from_id):
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    f = Friendship.query.filter_by(from_id=from_id, to_id=user.id, status='pending').first()
    if not f:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/friends/remove/<int:other_id>', methods=['POST'])
def remove_friend(other_id):
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    f = Friendship.query.filter(
        ((Friendship.from_id == user.id) & (Friendship.to_id == other_id)) |
        ((Friendship.from_id == other_id) & (Friendship.to_id == user.id))
    ).first()
    if f:
        db.session.delete(f)
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/friends/pending')
def pending_requests():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    incoming = Friendship.query.filter_by(to_id=user.id, status='pending').all()
    out = []
    for f in incoming:
        sender = User.query.get(f.from_id)
        if sender:
            out.append({'id': f.from_id, 'username': sender.username})
    return jsonify({'requests': out})


@app.route('/friends/list')
def friends_list():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'not_logged_in'}), 401
    friends = get_friends(user.id)
    return jsonify({'friends': [{'id': f.id, 'username': f.username} for f in friends]})


@app.route('/recommendations')
def recommendations():
    """Return movie recommendations based on friends' liked/highly-rated diary entries."""
    user = get_current_user()
    if not user:
        return jsonify({'movies': [], 'logged_in': False})

    friends = get_friends(user.id)
    if not friends:
        return jsonify({'movies': [], 'logged_in': True, 'no_friends': True})

    friend_ids = [f.id for f in friends]
    friend_movies = Movie.query.filter(
        Movie.user_id.in_(friend_ids),
        Movie.category == 'diary',
        (Movie.liked == True) | (Movie.rating >= 4)
    ).order_by(Movie.date.desc()).limit(60).all()

    if not friend_movies:
        friend_movies = Movie.query.filter(
            Movie.user_id.in_(friend_ids),
            Movie.category == 'diary'
        ).order_by(Movie.date.desc()).limit(20).all()

    if not friend_movies:
        return jsonify({'movies': [], 'logged_in': True, 'no_activity': True})

    already_seen = set(
        m.movie_id for m in Movie.query.filter_by(user_id=user.id, category='diary').all()
    )
    in_watchlist = set(
        m.movie_id for m in Movie.query.filter_by(user_id=user.id, category='watchlist').all()
    )

    from collections import Counter
    movie_score = Counter()
    movie_meta  = {}
    for m in friend_movies:
        movie_score[m.movie_id] += 1
        if m.movie_id not in movie_meta:
            friend_user = User.query.get(m.user_id)
            movie_meta[m.movie_id] = {
                'tmdb_id': m.movie_id,
                'title': m.title,
                'poster': m.poster,
                'friend': friend_user.username if friend_user else '?'
            }

    # Use ALL friend movies as sources (including ones you've seen like Cars),
    # so recommendations are still generated based on their genres/keywords.
    # Already-seen movies are filtered out later in seen_recs when building recs.
    top_sources = sorted(
        list(movie_score.keys()),
        key=lambda mid: -movie_score[mid]
    )[:8]

    seen_recs = set(already_seen) | set(in_watchlist)
    recs = []
    rec_ids = set()

    # For each source movie: get its genres/keywords, then find popular movies
    # that match on ALL primary genres (AND logic) plus share keywords
    for source_id in top_sources[:5]:
        try:
            # Step 1: fetch genres + keywords for the source movie
            detail_r = requests.get(
                f'https://api.themoviedb.org/3/movie/{source_id}',
                params={'api_key': API_KEY, 'append_to_response': 'keywords'},
                timeout=4
            )
            detail_data = detail_r.json()
            genre_ids = [g['id'] for g in (detail_data.get('genres') or [])]
            if not genre_ids:
                continue

            # All genres must match (TMDB AND = comma-separated with_genres)
            # Limit to 3 genres max — enough to be specific, not so many it returns nothing
            match_genres = genre_ids[:3]

            # Grab top keywords to further narrow the vibe
            kw_list = (detail_data.get('keywords') or {}).get('keywords') or []
            top_keywords = [k['id'] for k in kw_list[:5]]

            # Step 2: discover with strict genre AND match + keyword boost, sorted by popularity
            params = {
                'api_key': API_KEY,
                'with_genres': ','.join(str(g) for g in match_genres),  # AND logic
                'sort_by': 'popularity.desc',
                'vote_count.gte': 300,
                'vote_average.gte': 6.0,
                'page': 1,
            }
            if top_keywords:
                # TMDB keyword filter: movies must share at least one of these keywords
                params['with_keywords'] = '|'.join(str(k) for k in top_keywords)  # OR across keywords

            r = requests.get(
                'https://api.themoviedb.org/3/discover/movie',
                params=params,
                timeout=4
            )
            candidates = (r.json().get('results') or [])

            # If keyword filter was too strict and returned nothing, retry without keywords
            if not candidates and top_keywords:
                params.pop('with_keywords', None)
                r2 = requests.get(
                    'https://api.themoviedb.org/3/discover/movie',
                    params=params,
                    timeout=4
                )
                candidates = (r2.json().get('results') or [])

            for item in candidates[:8]:
                tid = item.get('id')
                if not tid or tid in seen_recs or tid in rec_ids:
                    continue
                poster_path = item.get('poster_path')
                recs.append({
                    'id': tid,
                    'title': item.get('title'),
                    'year': (item.get('release_date') or '')[:4],
                    'poster': f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None,
                    'because': movie_meta[source_id]['friend'],
                    'because_movie': movie_meta[source_id]['title'],
                })
                rec_ids.add(tid)
                seen_recs.add(tid)
                if len(recs) >= 20:
                    break
        except Exception:
            continue
        if len(recs) >= 20:
            break

    # Pad with friends' own liked movies user hasn't seen
    if len(recs) < 6:
        for mid in top_sources:
            if mid in rec_ids or mid in already_seen:
                continue
            meta = movie_meta.get(mid)
            if not meta:
                continue
            recs.append({
                'id': mid,
                'title': meta['title'],
                'year': '',
                'poster': meta['poster'],
                'because': meta['friend'],
                'because_movie': None,
            })
            rec_ids.add(mid)
            if len(recs) >= 20:
                break

    return jsonify({'movies': recs[:20], 'logged_in': True})

# ============================================================
# PUBLIC USER PROFILES
# ============================================================

@app.route('/user/<username>')
def public_profile(username):
    viewed_user = User.query.filter_by(username=username).first_or_404()
    current = get_current_user()

    movies = Movie.query.filter_by(category='diary', user_id=viewed_user.id).order_by(Movie.date.desc()).limit(4).all()
    prefs  = {p.movie_id: p for p in MovieImagePref.query.filter_by(user_id=viewed_user.id).all()}

    for m in movies:
        p = prefs.get(m.movie_id)
        if p and p.poster_path:
            m.poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}"

    slots = Top4Slot.query.filter_by(user_id=viewed_user.id).order_by(Top4Slot.slot).all()
    top4  = {}
    for s in slots:
        p = prefs.get(s.movie_id)
        poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}" if p and p.poster_path else s.poster
        top4[s.slot] = {'movie_id': s.movie_id, 'title': s.title, 'poster': poster}

    # friendship status between current user and viewed user
    friendship_status = None
    if current and current.id != viewed_user.id:
        f = Friendship.query.filter(
            ((Friendship.from_id == current.id) & (Friendship.to_id == viewed_user.id)) |
            ((Friendship.from_id == viewed_user.id) & (Friendship.to_id == current.id))
        ).first()
        if f:
            friendship_status = f.status
            if f.status == 'pending':
                friendship_status = 'pending_sent' if f.from_id == current.id else 'pending_received'

    is_self = current and current.id == viewed_user.id

    return render_template('user_profile.html',
        viewed_user=viewed_user,
        current_user=current,
        movies=movies,
        top4=top4,
        friendship_status=friendship_status,
        is_self=is_self
    )


@app.route('/user/<username>/diary')
def public_diary(username):
    viewed_user = User.query.filter_by(username=username).first_or_404()
    current     = get_current_user()

    movies = Movie.query.filter_by(category='diary', user_id=viewed_user.id).order_by(Movie.date.desc()).all()
    prefs  = {p.movie_id: p for p in MovieImagePref.query.filter_by(user_id=viewed_user.id).all()}

    # Build a map: entry.id -> list of sibling diary entries for the same film
    # so each diary row can show an expandable rewatch history
    from collections import OrderedDict, defaultdict
    all_entries_by_movie = defaultdict(list)
    for m in movies:
        all_entries_by_movie[m.movie_id].append(m)

    rewatches_map = {}  # entry.id -> [sibling entries sorted date desc, excluding self]
    for movie_id, entries in all_entries_by_movie.items():
        if len(entries) > 1:
            for e in entries:
                rewatches_map[e.id] = [s for s in entries if s.id != e.id]

    groups = OrderedDict()
    for m in movies:
        p = prefs.get(m.movie_id)
        if p and p.poster_path:
            m.poster = f"https://image.tmdb.org/t/p/w200{p.poster_path}"
        key = m.date.strftime("%B %Y")
        groups.setdefault(key, []).append(m)

    groups_list = list(groups.items())
    return render_template('user_diary.html',
        viewed_user=viewed_user,
        current_user=current,
        groups=groups_list,
        rewatches_map=rewatches_map
    )

@app.route('/feed')
def feed():
    user = get_current_user()
    if not user:
        flash('Please log in to see your feed.')
        return redirect(url_for('login'))

    friends = get_friends(user.id)
    if not friends:
        return render_template('feed.html', user=user, events=[])

    friend_ids = [f.id for f in friends]
    friend_map = {f.id: f for f in friends}

    # Fetch the last 100 diary entries from friends, newest first
    entries = (
        Movie.query
        .filter(Movie.user_id.in_(friend_ids), Movie.category == 'diary')
        .order_by(Movie.date.desc())
        .limit(100)
        .all()
    )

    # Attach poster URLs using each user's image pref where available
    all_prefs = {}
    for f in friends:
        for p in MovieImagePref.query.filter_by(user_id=f.id).all():
            all_prefs[(f.id, p.movie_id)] = p

    events = []
    for e in entries:
        pref = all_prefs.get((e.user_id, e.movie_id))
        poster = e.poster
        if pref and pref.poster_path:
            poster = f"https://image.tmdb.org/t/p/w200{pref.poster_path}"
        elif poster and not poster.startswith('http'):
            poster = f"https://image.tmdb.org/t/p/w200{poster}"
        events.append({
            'user': friend_map[e.user_id],
            'entry': e,
            'poster': poster,
        })

    return render_template('feed.html', user=user, events=events)

@app.route('/popular')
def popular():
    """Return most-logged movies across all users, excluding what the current user has already seen."""
    from collections import Counter
    user = get_current_user()

    already_seen = set()
    if user:
        already_seen = set(m.movie_id for m in Movie.query.filter_by(user_id=user.id, category='diary').all())

    # Count diary entries per movie across all users
    rows = db.session.query(Movie.movie_id, Movie.title, Movie.poster, db.func.count(Movie.id).label('cnt'))\
        .filter(Movie.category == 'diary')\
        .group_by(Movie.movie_id)\
        .order_by(db.func.count(Movie.id).desc())\
        .limit(80).all()

    movies = []
    seen_ids = set()
    for row in rows:
        if row.movie_id in seen_ids:
            continue
        if not row.poster:
            continue
        movies.append({
            'id': row.movie_id,
            'title': row.title,
            'poster': row.poster,
            'count': row.cnt,
            'seen': row.movie_id in already_seen,
        })
        seen_ids.add(row.movie_id)
        if len(movies) >= 24:
            break

    return jsonify({'movies': movies})


# ── Settings & Letterboxd Import ──────────────────────────────────────────────

@app.route('/settings')
def settings():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    return render_template('settings.html', user=user)


@app.route('/settings/import-letterboxd', methods=['POST'])
def import_letterboxd():
    import zipfile, csv, io, time
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    f = request.files.get('file')
    if not f or not f.filename.endswith('.zip'):
        return jsonify({'error': 'Please upload a valid Letterboxd ZIP file.'}), 400

    import_watched = request.form.get('import_watched', '1') == '1'
    import_ratings = request.form.get('import_ratings', '1') == '1'

    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    except Exception:
        return jsonify({'error': 'Could not open ZIP file. Make sure it is a valid Letterboxd export.'}), 400

    # Letterboxd export contains three relevant files:
    # - diary.csv:   one row per diary entry (logged watches), has 'Watched Date' (actual watch date),
    #                'Date' (log date), 'Rating', 'Rewatch'. THIS IS THE SOURCE OF TRUTH.
    # - watched.csv: one row per watched film (includes films watched before diary feature was used),
    #                only has 'Date' (log date, NOT watch date). Used as fallback for films not in diary.
    # - ratings.csv: one row per rated film. A film can be rated without being logged as watched.
    #                We never create diary entries from this file alone.
    #
    # entries is a list (not dict) to support multiple diary entries (rewatches) per film.
    entries = []  # list of {name, year, rating, date, rewatch}

    def parse_csv_from_zip(filename):
        for name in zf.namelist():
            if name.lower().endswith(filename.lower()) and not any(
                p in name.lower() for p in ['deleted/', 'orphaned/']):
                with zf.open(name) as fh:
                    text = fh.read().decode('utf-8-sig')
                    return list(csv.DictReader(io.StringIO(text)))
        return []

    def parse_rating(raw):
        """Convert Letterboxd 0.5–5 rating to 1–5 integer (floor half-stars)."""
        try:
            r = float(raw)
            if r > 5:
                return max(1, min(5, int(r / 2)))
            return max(1, min(5, int(r))) if r > 0 else None
        except Exception:
            return None

    # Step 1: primary source — diary.csv (has real watch dates and rewatches)
    diary_keys = set()  # (name_lower, year) seen in diary
    for row in parse_csv_from_zip('diary.csv'):
        title    = (row.get('Name') or '').strip()
        year     = (row.get('Year') or '').strip()
        # 'Watched Date' is when they actually watched it; 'Date' is when they logged it
        date_str = (row.get('Watched Date') or row.get('Date') or '').strip()
        raw_rating = (row.get('Rating') or '').strip()
        rewatch  = (row.get('Rewatch') or '').strip().lower() == 'yes'
        if not title:
            continue
        rating_int = parse_rating(raw_rating) if import_ratings else None
        entries.append({'name': title, 'year': year, 'rating': rating_int,
                        'date': date_str, 'rewatch': rewatch})
        diary_keys.add((title.lower(), year))

    # Step 2: fallback — watched.csv for films not already covered by diary.csv
    if import_watched:
        for row in parse_csv_from_zip('watched.csv'):
            title    = (row.get('Name') or '').strip()
            year     = (row.get('Year') or '').strip()
            date_str = (row.get('Date') or '').strip()
            if not title:
                continue
            if (title.lower(), year) in diary_keys:
                continue  # already have this from diary.csv
            entries.append({'name': title, 'year': year, 'rating': None,
                            'date': date_str, 'rewatch': False})

    found = len(entries)
    imported = 0
    skipped = 0
    not_found = 0
    ratings_only_skipped = 0

    # Cache TMDB lookups so rewatches of the same film don't hit the API twice
    tmdb_cache = {}  # (title_lower, year) -> {tmdb_id, poster_path, runtime, genres, title}

    for info in entries:
        title  = info['name']
        year   = info['year']
        rating = info['rating']
        rewatch = info.get('rewatch', False)
        cache_key = (title.lower(), year)

        if cache_key not in tmdb_cache:
            search_url = f"https://api.themoviedb.org/3/search/movie?api_key={API_KEY}&query={requests.utils.quote(title)}&year={year}&page=1"
            try:
                resp = requests.get(search_url, timeout=8)
                results = resp.json().get('results', [])
            except Exception:
                not_found += 1
                tmdb_cache[cache_key] = None
                continue

            match = None
            for r in results:
                if (r.get('release_date') or '')[:4] == year:
                    match = r
                    break
            if not match and results:
                match = results[0]

            if not match:
                not_found += 1
                tmdb_cache[cache_key] = None
                continue

            tmdb_id = match['id']

            # Fetch full details for runtime + genres
            detail_runtime = None
            detail_genres = None
            detail_poster = None
            try:
                detail_resp = requests.get(
                    f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={API_KEY}",
                    timeout=8
                )
                detail_data = detail_resp.json()
                detail_runtime, detail_genres = _tmdb_runtime_genres(detail_data)
                detail_poster = detail_data.get('poster_path')
            except Exception:
                pass

            poster_path = detail_poster or match.get('poster_path')
            tmdb_cache[cache_key] = {
                'tmdb_id': tmdb_id,
                'poster_url': (f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None),
                'runtime': detail_runtime,
                'genres': detail_genres,
                'title': match.get('title', title),
            }
            time.sleep(0.25)
        else:
            if tmdb_cache[cache_key] is None:
                not_found += 1
                continue

        cached = tmdb_cache[cache_key]
        if cached is None:
            continue
        tmdb_id = cached['tmdb_id']

        # For non-rewatches, skip if this exact film is already in diary
        if not rewatch:
            existing = Movie.query.filter_by(movie_id=tmdb_id, category='diary', user_id=user.id).first()
            if existing:
                skipped += 1
                continue

        # Parse watch date
        entry_date = datetime.utcnow()
        if info['date']:
            try:
                entry_date = datetime.strptime(info['date'], '%Y-%m-%d')
            except Exception:
                pass

        new_entry = Movie(
            movie_id=tmdb_id,
            title=cached['title'],
            category='diary',
            poster=cached['poster_url'],
            rating=rating,
            date=entry_date,
            rewatch=rewatch,
            user_id=user.id,
            runtime=cached['runtime'],
            genres=cached['genres'],
        )
        db.session.add(new_entry)
        imported += 1

    db.session.commit()

    return jsonify({
        'found': found,
        'imported': imported,
        'skipped': skipped,
        'not_found': not_found,
        'ratings_only_skipped': ratings_only_skipped,
    })


# 🏆 LEADERBOARD
GENRE_ORDER = [
    'Action', 'Adventure', 'Animation', 'Comedy', 'Crime',
    'Documentary', 'Drama', 'Fantasy', 'Horror', 'Music',
    'Mystery', 'Romance', 'Science Fiction', 'Thriller', 'Western',
]

@app.route("/leaderboard")
def leaderboard():
    from collections import defaultdict

    users = User.query.all()

    # Build per-user stats
    user_stats = []
    for u in users:
        diary_entries = Movie.query.filter_by(category='diary', user_id=u.id).all()
        total_minutes = 0
        genre_minutes = defaultdict(int)
        for entry in diary_entries:
            rt = entry.runtime or 0
            total_minutes += rt
            if entry.genres:
                for g in entry.genres.split(','):
                    g = g.strip()
                    if g:
                        genre_minutes[g] += rt
        user_stats.append({
            'username': u.username,
            'avatar': u.avatar_path.replace('avatars/', '', 1) if u.avatar_path else None,
            'total_minutes': total_minutes,
            'genre_minutes': dict(genre_minutes),
            'film_count': len(diary_entries),
        })

    # Overall leaderboard
    overall = sorted(user_stats, key=lambda x: x['total_minutes'], reverse=True)

    # Per-genre leaderboards
    genre_boards = {}
    for genre in GENRE_ORDER:
        ranked = [u for u in user_stats if u['genre_minutes'].get(genre, 0) > 0]
        ranked = sorted(ranked, key=lambda x: x['genre_minutes'].get(genre, 0), reverse=True)
        if ranked:
            genre_boards[genre] = ranked

    current_user = get_current_user()
    return render_template('leaderboard.html',
                           overall=overall,
                           genre_boards=genre_boards,
                           current_user=current_user)


# 🧹 CLEANUP: remove diary entries that came from ratings.csv only (never watched)
@app.route('/settings/cleanup-ratings-only', methods=['POST'])
def cleanup_ratings_only():
    import zipfile, csv, io
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    f = request.files.get('file')
    if not f or not f.filename.endswith('.zip'):
        return jsonify({'error': 'Please upload a valid Letterboxd ZIP file.'}), 400

    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    except Exception:
        return jsonify({'error': 'Could not open ZIP file.'}), 400

    def parse_csv_from_zip(filename):
        for name in zf.namelist():
            if name.lower().endswith(filename.lower()):
                with zf.open(name) as fh:
                    text = fh.read().decode('utf-8-sig')
                    return list(csv.DictReader(io.StringIO(text)))
        return []

    # diary.csv contains only films the user actually logged as watched.
    # Any film NOT in diary.csv was never logged — it was only rated or only in watched history.
    # We keep films that appear in diary.csv or watched.csv (both are valid logged watches).
    watched_titles = set()
    for row in parse_csv_from_zip('diary.csv'):
        title = (row.get('Name') or '').strip()
        if title:
            watched_titles.add(title.lower())
    for row in parse_csv_from_zip('watched.csv'):
        title = (row.get('Name') or '').strip()
        if title:
            watched_titles.add(title.lower())

    # Any diary entry whose title is NOT in either CSV was imported incorrectly
    # (came from ratings.csv only in the old broken import).
    diary_entries = Movie.query.filter_by(category='diary', user_id=user.id).all()
    removed = 0
    for entry in diary_entries:
        title_lower = (entry.title or '').lower().strip()
        if title_lower not in watched_titles:
            db.session.delete(entry)
            removed += 1

    db.session.commit()
    return jsonify({'removed': removed})


# 🔧 BACKFILL runtime/genres for existing diary entries that are missing them
@app.route('/settings/backfill-runtime', methods=['POST'])
def backfill_runtime():
    import time
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    missing = Movie.query.filter_by(category='diary', user_id=user.id).filter(Movie.runtime == None).all()
    updated = 0
    failed = 0
    for entry in missing:
        try:
            resp = requests.get(
                f"https://api.themoviedb.org/3/movie/{entry.movie_id}?api_key={API_KEY}",
                timeout=8
            )
            data = resp.json()
            rt, genres_str = _tmdb_runtime_genres(data)
            entry.runtime = rt
            entry.genres = genres_str
            updated += 1
        except Exception:
            failed += 1
        time.sleep(0.15)

    db.session.commit()
    return jsonify({'updated': updated, 'failed': failed, 'remaining': failed})


@app.route('/settings/delete-account', methods=['POST'])
def delete_account():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    import os as _os

    # Delete avatar file from disk if it exists
    if user.avatar_path:
        avatar_file = _os.path.join(app.config['AVATAR_FOLDER'], _os.path.basename(user.avatar_path))
        try:
            if _os.path.exists(avatar_file):
                _os.remove(avatar_file)
        except Exception:
            pass

    # Delete all user data
    Movie.query.filter_by(user_id=user.id).delete()
    MovieImagePref.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()

    session.clear()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
#  AI FEATURES  (Gemini API)
# ─────────────────────────────────────────────
import json as _json

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
if OPENROUTER_API_KEY in ('', 'your_openrouter_api_key_here'): OPENROUTER_API_KEY = ''
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_MODEL = 'meta-llama/llama-3.1-8b-instruct'


def _gemini(prompt: str, system: str = '') -> str:
    """Call OpenRouter and return the text response."""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'HTTP-Referer': 'http://localhost:5000',
        'X-Title': 'MovieLand',
    }
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    # Try the configured model, then fall back without :free suffix if needed
    models_to_try = [OPENROUTER_MODEL]
    if OPENROUTER_MODEL.endswith(':free'):
        models_to_try.append(OPENROUTER_MODEL[:-5])

    last_err = None
    for model in models_to_try:
        body = {
            'model': model,
            'messages': messages,
            'max_tokens': 512,
            'temperature': 0.7,
        }
        r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=30)
        if r.ok:
            data = r.json()
            choices = data.get('choices', [])
            if choices:
                return choices[0]['message']['content']
            raise Exception('OpenRouter nevrátil žiadnu odpoveď')
        try:
            err = r.json().get('error', {})
            last_err = err.get('message', r.text)
        except Exception:
            last_err = r.text

    raise Exception(last_err or 'OpenRouter chyba')


def _tmdb_poster(tmdb_id):
    """Fetch poster URL for a TMDB movie id."""
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={API_KEY}',
            timeout=6
        )
        d = r.json()
        p = d.get('poster_path')
        return f'https://image.tmdb.org/t/p/w154{p}' if p else None
    except Exception:
        return None


def _tmdb_search_first(title: str):
    """Search TMDB for a title, return (tmdb_id, poster_url, year) or (None,None,None)."""
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/search/movie?api_key={API_KEY}&query={requests.utils.quote(title)}&language=sk',
            timeout=6
        )
        results = r.json().get('results', [])
        if not results:
            return None, None, None
        m = results[0]
        tmdb_id = m.get('id')
        poster = f'https://image.tmdb.org/t/p/w154{m["poster_path"]}' if m.get('poster_path') else None
        year = (m.get('release_date') or '')[:4] or None
        return tmdb_id, poster, year
    except Exception:
        return None, None, None


# ── 3.1  AI Chat page ────────────────────────
@app.route('/ai-chat')
def ai_chat_page():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    avatar = '/static/' + user.avatar_path if user.avatar_path else None
    return render_template('ai_chat.html', avatar=avatar)


# ── 3.1  AI Chat API ─────────────────────────
@app.route('/api/ai-chat', methods=['POST'])
def api_ai_chat():
    if not get_current_user():
        return jsonify({'error': 'You need to be logged in to use the AI chat.'}), 401
    if not OPENROUTER_API_KEY:
        return jsonify({'error': 'OpenRouter API kľúč nie je nastavený. Doplň OPENROUTER_API_KEY do súboru api.env (získaš ho zadarmo na openrouter.ai/keys)'}), 503

    data = request.get_json(force=True)
    user_msg = (data.get('message') or '').strip()
    if not user_msg:
        return jsonify({'error': 'Prázdna správa'}), 400

    # Keep a rolling history in the session (last 10 turns)
    history = session.get('ai_chat_history', [])
    history.append({'role': 'user', 'text': user_msg})
    history = history[-10:]

    history_text = '\n'.join(
        f"{'Používateľ' if h['role'] == 'user' else 'Asistent'}: {h['text']}"
        for h in history
    )

    system = (
        "You are a film assistant for MovieLand. Recommend movies based on what the user writes. "
        "Always reply in English. "
        "Na konci každej odpovede vráť JSON blok takto (nič iné za ním): "
        '```json\n{"movies":[{"title":"...","year":"...","reason":"..."}]}\n```'
        "List 2-4 movies. If the question is not about films, return an empty list."
    )

    prompt = f"História rozhovoru:\n{history_text}\n\nOdpovedz na poslednú správu používateľa."

    try:
        raw = _gemini(prompt, system)
    except Exception as e:
        return jsonify({'error': f'Gemini chyba: {str(e)}'}), 502

    # Extract JSON block — handle both ```json fences and raw {...} at end
    movies = []
    text = raw
    import re as _re
    try:
        json_str = None
        if '```json' in raw:
            json_str = raw.split('```json')[1].split('```')[0].strip()
            text = raw.split('```json')[0].strip()
        else:
            # Find last {...} block that looks like our movies JSON
            match = _re.search(r'(\{[^{}]*"movies"\s*:\s*\[.*?\]\s*\})', raw, _re.DOTALL)
            if match:
                json_str = match.group(1).strip()
                text = raw[:match.start()].strip()

        if json_str:
            parsed = _json.loads(json_str)
            movies_raw = parsed.get('movies', [])
            for m in movies_raw:
                title = m.get('title', '')
                tmdb_id, poster, year_tmdb = _tmdb_search_first(title)
                movies.append({
                    'title': title,
                    'year': m.get('year') or year_tmdb,
                    'reason': m.get('reason', ''),
                    'tmdb_id': tmdb_id,
                    'poster': poster,
                })
    except Exception:
        pass

    history.append({'role': 'ai', 'text': text})
    session['ai_chat_history'] = history
    session.modified = True

    return jsonify({'text': text, 'movies': movies})


# ── 3.2  AI Identify page ────────────────────
@app.route('/ai-identify')
def ai_identify_page():
    return redirect(url_for('ai_chat_page'))


# ── 3.2  AI Identify API ─────────────────────
@app.route('/api/ai-identify', methods=['POST'])
def api_ai_identify():
    if not get_current_user():
        return jsonify({'error': 'You need to be logged in to use this feature.'}), 401
    if not OPENROUTER_API_KEY:
        return jsonify({'error': 'OpenRouter API kľúč nie je nastavený. Doplň OPENROUTER_API_KEY do súboru api.env (získaš ho zadarmo na openrouter.ai/keys)'}), 503

    data = request.get_json(force=True)
    desc = (data.get('description') or '').strip()
    if not desc:
        return jsonify({'error': 'Prázdny opis'}), 400

    system = (
        "You are a film expert. The user describes a film and you identify which film it is. "
        "Odpovedaj VÝLUČNE vo formáte JSON (žiadny iný text). "
        'Formát: {"main":{"title":"...","year":"...","director":"...","explanation":"...","confidence":"high|medium"},'
        '"alternatives":[{"title":"...","year":"...","reason":"..."}]}'
        "Uveď 0-3 alternatívy. Vysvetlenie píš po slovensky."
    )

    try:
        raw = _gemini(desc, system)
    except Exception as e:
        return jsonify({'error': f'Gemini chyba: {str(e)}'}), 502

    try:
        # Strip possible markdown fences
        clean = raw.strip()
        if clean.startswith('```'):
            clean = clean.split('```')[1]
            if clean.startswith('json'):
                clean = clean[4:]
            clean = clean.strip()
        parsed = _json.loads(clean)
    except Exception:
        return jsonify({'error': 'Nepodarilo sa spracovať odpoveď AI. Skús znova.'}), 502

    # Enrich main result with TMDB data
    main = parsed.get('main', {})
    tmdb_id, poster, year_tmdb = _tmdb_search_first(main.get('title', ''))
    main['tmdb_id'] = tmdb_id
    main['poster'] = poster
    if not main.get('year') and year_tmdb:
        main['year'] = year_tmdb

    # Enrich alternatives
    alts = []
    for a in parsed.get('alternatives', []):
        tid, aposter, ayear = _tmdb_search_first(a.get('title', ''))
        alts.append({
            'title': a.get('title', ''),
            'year': a.get('year') or ayear,
            'reason': a.get('reason', ''),
            'tmdb_id': tid,
            'poster': aposter,
        })

    return jsonify({'main': main, 'alternatives': alts})


# ── Movie redirect helper (tmdb_id → internal movie page) ────
@app.route('/movie-redirect/<int:tmdb_id>')
def movie_redirect(tmdb_id):
    """Try to find the movie in the local DB; if not, go to TMDB."""
    movie = Movie.query.filter_by(movie_id=tmdb_id).first()
    if movie:
        return redirect(url_for('movie_detail', movie_id=movie.movie_id))
    return redirect(f'https://www.themoviedb.org/movie/{tmdb_id}')
