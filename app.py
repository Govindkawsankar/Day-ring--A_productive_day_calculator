import csv
import io
import json
import os
import sqlite3
import statistics
from datetime import date, timedelta, datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, g, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = "productive_day.db"

# --- Database backend: local SQLite file by default (unchanged local dev
# workflow), or a free-tier Turso (libSQL) database when these two env vars
# are set — that's how the app runs on a host with no persistent disk. ---
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


class DictRow:
    """Makes a Turso/libSQL result row support row['col'], row[0], dict(row),
    and .keys() — the same interface sqlite3.Row already provides — so the
    rest of this app doesn't need to know which backend is active."""
    __slots__ = ("_columns", "_values")

    def __init__(self, columns, values):
        self._columns = columns
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._columns.index(key)]
        return self._values[key]

    def keys(self):
        return list(self._columns)

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return f"DictRow({dict(zip(self._columns, self._values))})"


class TursoCursor:
    def __init__(self, raw_cursor):
        self._cursor = raw_cursor

    def _columns(self):
        return [d[0] for d in (self._cursor.description or [])]

    def fetchone(self):
        row = self._cursor.fetchone()
        return DictRow(self._columns(), row) if row is not None else None

    def fetchall(self):
        cols = self._columns()
        return [DictRow(cols, r) for r in self._cursor.fetchall()]

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


class TursoConnection:
    """Wraps a raw libsql connection so calling code (db.execute(...), etc.)
    behaves the same as it does against a plain sqlite3.Connection."""
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=()):
        return TursoCursor(self._conn.execute(sql, params))

    def executemany(self, sql, seq_of_params):
        return TursoCursor(self._conn.executemany(sql, seq_of_params))

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def open_db_connection():
    if USE_TURSO:
        import libsql
        return TursoConnection(libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


app = Flask(__name__)
# For local use this default is fine. Before hosting this online, set a real
# SECRET_KEY environment variable so sessions can't be forged.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-this-before-hosting")


@app.context_processor
def inject_globals():
    endpoint_map = {
        "index": "today", "day_view": "today",
        "history": "history", "insights": "insights", "habits": "habits",
    }
    return {
        "today_iso": date.today().isoformat(),
        "active_page": endpoint_map.get(request.endpoint, "today"),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    return session["user_id"]


# --- Default habit set: category, name, weight (weights sum to 100) ---
DEFAULT_HABITS = [
    ("Mind", "Focused study block (2+ hrs)", 15),
    ("Mind", "Completed today's key task", 10),
    ("Body", "Exercise / movement (20+ min)", 15),
    ("Rest", "Slept 7+ hours", 10),
    ("Rest", "In bed before target time", 5),
    ("Nutrition", "All meals on time", 10),
    ("Nutrition", "Drank enough water", 5),
    ("Discipline", "Woke up on time", 10),
    ("Discipline", "Screen time under limit", 10),
    ("Social", "Meaningful social/family time", 5),
    ("Social", "Journaled / reflected", 5),
]

CATEGORY_COLORS = {
    "Mind": "#5B7FBD",
    "Body": "#6E9B6E",
    "Rest": "#8E7CC3",
    "Nutrition": "#C9A227",
    "Discipline": "#3F9C9C",
    "Social": "#C97B84",
}

# Used for any category the user adds beyond the default six
EXTRA_PALETTE = ["#B0724A", "#5C7A99", "#7A9E7E", "#A6689A", "#B08C3A", "#6A7FA6"]

# How many distinct logged days before the ML features have enough to work with
BASELINE_TARGET_DAYS = 15

TAG_KEYWORDS = {
    "Fatigue": ["tired", "exhausted", "sleepy", "fatigue", "no energy", "drained", "burnt out"],
    "Distraction": ["distracted", "phone", "social media", "scrolling", "instagram", "youtube", "reels"],
    "Procrastination": ["procrastinat", "lazy", "delayed", "put off", "avoided", "kept putting"],
    "Stress": ["stressed", "anxious", "overwhelmed", "pressure", "anxiety", "panic"],
    "Health": ["sick", "ill", "headache", "unwell", "cold", "fever", "pain"],
    "Social": ["friends", "family", "party", "hangout", "outing", "guests"],
    "Academics": ["exam", "assignment", "deadline", "study", "class", "lecture", "test", "submission"],
    "Focus": ["focused", "productive", "flow", "concentrated", "on track"],
    "Motivation": ["motivated", "unmotivated", "demotivated", "inspired", "energized"],
}

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _sentiment_analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _sentiment_analyzer = None


def analyze_reflection(text):
    """Returns (sentiment_compound_score_or_None, comma_joined_tags)."""
    text = (text or "").strip()
    if not text:
        return None, ""
    sentiment = _sentiment_analyzer.polarity_scores(text)["compound"] if _sentiment_analyzer else None
    lower = text.lower()
    tags = [tag for tag, keywords in TAG_KEYWORDS.items() if any(kw in lower for kw in keywords)]
    return sentiment, ",".join(tags)


def sentiment_label(score):
    if score is None:
        return None
    if score >= 0.3:
        return "positive"
    if score <= -0.3:
        return "negative"
    return "neutral"


def encouragement_for(pct, is_today):
    if not is_today:
        return None
    if pct == 0:
        return "Nothing logged yet — even one habit checked gets the ring moving."
    if pct < 40:
        return "Started. A couple more checks and today's already a solid day."
    if pct < 70:
        return "Good pace — you're past the halfway point."
    if pct < 100:
        return "Strong day. A few more and it's a clean sweep."
    return "Full ring. That's exactly the kind of day the model will learn from."


def color_for_category(cat, known_categories):
    if cat in CATEGORY_COLORS:
        return CATEGORY_COLORS[cat]
    others = [c for c in known_categories if c != cat and c not in CATEGORY_COLORS]
    idx = others.index(cat) if cat in others else 0
    return EXTRA_PALETTE[idx % len(EXTRA_PALETTE)]


def get_db():
    if "db" not in g:
        g.db = open_db_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_or_create_legacy_user(db):
    """Used only when migrating a pre-accounts database, so existing solo
    data isn't lost. Default login: username 'me', password 'changeme123'
    — change this immediately after your first login."""
    row = db.execute("SELECT id FROM users WHERE username = ?", ("me",)).fetchone()
    if row:
        return row["id"]
    pwd_hash = generate_password_hash("changeme123")
    cur = db.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)", ("me", pwd_hash)
    )
    return cur.lastrowid


def seed_default_habits(db, user_id):
    db.executemany(
        "INSERT INTO habits (user_id, category, name, weight) VALUES (?, ?, ?, ?)",
        [(user_id, cat, name, weight) for cat, name, weight in DEFAULT_HABITS],
    )


def init_db():
    db = open_db_connection()

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            security_question TEXT,
            security_answer_hash TEXT,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    for col, coltype in [
        ("security_question", "TEXT"), ("security_answer_hash", "TEXT"),
        ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"), ("locked_until", "TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # column already exists — sqlite3 raises OperationalError, libSQL raises ValueError
    db.execute("""
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            weight INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            habit_id INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            UNIQUE(log_date, habit_id),
            FOREIGN KEY (habit_id) REFERENCES habits(id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS reflections (
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            text TEXT,
            sentiment REAL,
            tags TEXT,
            PRIMARY KEY (user_id, log_date)
        )
    """)

    # --- Migrate a pre-accounts database (single shared user) in place ---
    habit_cols = [r[1] for r in db.execute("PRAGMA table_info(habits)").fetchall()]
    if "user_id" not in habit_cols:
        legacy_id = get_or_create_legacy_user(db)
        db.execute("ALTER TABLE habits ADD COLUMN user_id INTEGER")
        db.execute("UPDATE habits SET user_id = ? WHERE user_id IS NULL", (legacy_id,))
        db.execute("ALTER TABLE logs ADD COLUMN user_id INTEGER")
        db.execute("UPDATE logs SET user_id = ? WHERE user_id IS NULL", (legacy_id,))

    reflection_cols = [r[1] for r in db.execute("PRAGMA table_info(reflections)").fetchall()]
    if "user_id" not in reflection_cols:
        legacy_id = get_or_create_legacy_user(db)
        old_rows = db.execute("SELECT log_date, text, sentiment, tags FROM reflections").fetchall()
        db.execute("ALTER TABLE reflections RENAME TO reflections_old")
        db.execute("""
            CREATE TABLE reflections (
                user_id INTEGER NOT NULL,
                log_date TEXT NOT NULL,
                text TEXT,
                sentiment REAL,
                tags TEXT,
                PRIMARY KEY (user_id, log_date)
            )
        """)
        for r in old_rows:
            db.execute(
                "INSERT INTO reflections (user_id, log_date, text, sentiment, tags) VALUES (?, ?, ?, ?, ?)",
                (legacy_id, r["log_date"], r["text"], r["sentiment"], r["tags"]),
            )
        db.execute("DROP TABLE reflections_old")
    else:
        for col, coltype in [("sentiment", "REAL"), ("tags", "TEXT")]:
            try:
                db.execute(f"ALTER TABLE reflections ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # column already exists

    db.commit()
    db.close()


def get_active_habits(db, user_id):
    return db.execute(
        "SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY category, id", (user_id,)
    ).fetchall()


def get_day_data(db, user_id, log_date):
    habits = get_active_habits(db, user_id)
    completed_rows = db.execute(
        "SELECT habit_id, completed FROM logs WHERE user_id = ? AND log_date = ?", (user_id, log_date)
    ).fetchall()
    completed_map = {r["habit_id"]: bool(r["completed"]) for r in completed_rows}

    by_category = {}
    total_weight = 0
    earned_weight = 0
    for h in habits:
        done = completed_map.get(h["id"], False)
        total_weight += h["weight"]
        if done:
            earned_weight += h["weight"]
        by_category.setdefault(h["category"], {"total": 0, "earned": 0, "habits": []})
        by_category[h["category"]]["total"] += h["weight"]
        if done:
            by_category[h["category"]]["earned"] += h["weight"]
        by_category[h["category"]]["habits"].append({
            "id": h["id"], "name": h["name"], "weight": h["weight"], "done": done
        })

    overall_pct = round((earned_weight / total_weight) * 100) if total_weight else 0

    RADII = [90, 78, 66, 54, 42, 30]
    cat_names = list(by_category.keys())
    category_rings = []
    for idx, (cat, data) in enumerate(by_category.items()):
        pct = round((data["earned"] / data["total"]) * 100) if data["total"] else 0
        radius = RADII[idx] if idx < len(RADII) else 20
        circumference = round(2 * 3.14159265 * radius, 2)
        category_rings.append({
            "name": cat,
            "pct": pct,
            "color": color_for_category(cat, cat_names),
            "radius": radius,
            "circumference": circumference,
            "offset": round(circumference * (1 - pct / 100), 2),
        })

    reflection_row = db.execute(
        "SELECT text, sentiment, tags FROM reflections WHERE user_id = ? AND log_date = ?",
        (user_id, log_date),
    ).fetchone()
    reflection_text = reflection_row["text"] if reflection_row else ""
    reflection_sentiment = reflection_row["sentiment"] if reflection_row else None
    reflection_tags = (
        [t for t in reflection_row["tags"].split(",") if t] if reflection_row and reflection_row["tags"] else []
    )

    return {
        "overall_pct": overall_pct,
        "by_category": by_category,
        "category_rings": category_rings,
        "reflection_text": reflection_text,
        "reflection_sentiment": reflection_sentiment,
        "reflection_sentiment_label": sentiment_label(reflection_sentiment),
        "reflection_tags": reflection_tags,
        "earned_weight": earned_weight,
        "total_weight": total_weight,
    }


def compute_streak(db, user_id, threshold=50):
    """Consecutive days up to and including today with overall score >= threshold."""
    streak = 0
    d = date.today()
    while True:
        data = get_day_data(db, user_id, d.isoformat())
        if data["overall_pct"] >= threshold:
            streak += 1
            d -= timedelta(days=1)
        else:
            break
        if streak > 3650:  # safety valve
            break
    return streak


def total_logged_days(db, user_id):
    row = db.execute(
        "SELECT COUNT(DISTINCT log_date) AS n FROM logs WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["n"] if row else 0


# --- Auth routes ---

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", error=None)
    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    security_question = request.form.get("security_question", "").strip()
    security_answer = request.form.get("security_answer", "").strip()
    if not username or not password or not security_question or not security_answer:
        return render_template("register.html", error="All fields are required, including the security question — it's how you'll reset your password if you forget it.")
    if len(password) < 6:
        return render_template("register.html", error="Password must be at least 6 characters.")
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return render_template("register.html", error="That username is already taken.")
    cur = db.execute(
        "INSERT INTO users (username, password_hash, security_question, security_answer_hash) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), security_question, generate_password_hash(security_answer.lower())),
    )
    user_id = cur.lastrowid
    seed_default_habits(db, user_id)
    db.commit()
    session["user_id"] = user_id
    session["username"] = username
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)
    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    row = db.execute(
        "SELECT id, username, password_hash, failed_attempts, locked_until FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if row and row["locked_until"]:
        locked_until = datetime.fromisoformat(row["locked_until"])
        if datetime.utcnow() < locked_until:
            minutes_left = max(1, round((locked_until - datetime.utcnow()).total_seconds() / 60))
            return render_template("login.html", error=f"Too many failed attempts. Try again in about {minutes_left} minute{'s' if minutes_left != 1 else ''}.")

    if not row or not check_password_hash(row["password_hash"], password):
        if row:
            attempts = row["failed_attempts"] + 1
            locked_until = None
            if attempts >= MAX_LOGIN_ATTEMPTS:
                locked_until = (datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                attempts = 0
            db.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (attempts, locked_until, row["id"]),
            )
            db.commit()
            if locked_until:
                return render_template("login.html", error=f"Too many failed attempts. Locked for {LOCKOUT_MINUTES} minutes.")
        return render_template("login.html", error="Incorrect username or password.")

    db.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (row["id"],))
    db.commit()
    session["user_id"] = row["id"]
    session["username"] = row["username"]
    next_path = request.args.get("next")
    return redirect(next_path or url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    db = get_db()
    step = request.form.get("step", "username")

    if request.method == "GET":
        return render_template("forgot_password.html", step="username", username=None, error=None)

    if step == "username":
        username = request.form.get("username", "").strip()
        row = db.execute("SELECT security_question FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return render_template("forgot_password.html", step="username", username=None, error="No account with that username.")
        return render_template("forgot_password.html", step="answer", username=username, question=row["security_question"], error=None)

    if step == "answer":
        username = request.form.get("username", "").strip()
        answer = request.form.get("security_answer", "").strip().lower()
        new_password = request.form.get("new_password", "")
        row = db.execute(
            "SELECT id, security_answer_hash, security_question FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row or not row["security_answer_hash"] or not check_password_hash(row["security_answer_hash"], answer):
            return render_template("forgot_password.html", step="answer", username=username, question=row["security_question"] if row else "", error="That answer doesn't match.")
        if len(new_password) < 6:
            return render_template("forgot_password.html", step="answer", username=username, question=row["security_question"], error="New password must be at least 6 characters.")
        db.execute(
            "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (generate_password_hash(new_password), row["id"]),
        )
        db.commit()
        return render_template("login.html", error=None, reset_success=True)

    return redirect(url_for("forgot_password"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    db = get_db()
    user_id = current_user_id()
    if request.method == "GET":
        return render_template("account.html", error=None, success=None)
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not check_password_hash(row["password_hash"], current_password):
        return render_template("account.html", error="Current password is incorrect.", success=None)
    if len(new_password) < 6:
        return render_template("account.html", error="New password must be at least 6 characters.", success=None)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
    db.commit()
    return render_template("account.html", error=None, success="Password updated.")


# --- Day view / toggling / reflection ---

@app.route("/")
@login_required
def index():
    return redirect(url_for("day_view", log_date=date.today().isoformat()))


@app.route("/day/<log_date>")
@login_required
def day_view(log_date):
    db = get_db()
    user_id = current_user_id()
    try:
        current = date.fromisoformat(log_date)
    except ValueError:
        return redirect(url_for("day_view", log_date=date.today().isoformat()))
    day_data = get_day_data(db, user_id, log_date)
    streak = compute_streak(db, user_id)
    is_today = (current == date.today())
    logged_days = total_logged_days(db, user_id)

    predicted_tone = None
    anomaly = None
    if is_today:
        model = compute_predictive_model(db, user_id)
        cat_pcts = {r["name"]: r["pct"] for r in day_data["category_rings"]}
        predicted_tone = predict_today_tone(model, cat_pcts)
        anomaly = compute_anomaly(db, user_id, day_data["overall_pct"])

    return render_template(
        "index.html",
        today=log_date,
        is_today=is_today,
        prev_date=(current - timedelta(days=1)).isoformat(),
        next_date=(current + timedelta(days=1)).isoformat(),
        streak=streak,
        message=encouragement_for(day_data["overall_pct"], is_today),
        logged_days=logged_days,
        baseline_target=BASELINE_TARGET_DAYS,
        baseline_pct=min(100, round(logged_days / BASELINE_TARGET_DAYS * 100)),
        predicted_tone=predicted_tone,
        predicted_tone_label=sentiment_label(predicted_tone),
        anomaly=anomaly,
        **day_data,
    )


@app.route("/toggle", methods=["POST"])
@login_required
def toggle():
    db = get_db()
    user_id = current_user_id()
    log_date = request.form["log_date"]
    habit_id = int(request.form["habit_id"])
    completed = request.form.get("completed") == "1"

    owns_habit = db.execute(
        "SELECT id FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
    ).fetchone()
    if not owns_habit:
        return redirect(url_for("day_view", log_date=log_date))

    db.execute(
        """
        INSERT INTO logs (user_id, log_date, habit_id, completed) VALUES (?, ?, ?, ?)
        ON CONFLICT(log_date, habit_id) DO UPDATE SET completed = excluded.completed
        """,
        (user_id, log_date, habit_id, int(completed)),
    )
    db.commit()
    return redirect(url_for("day_view", log_date=log_date))


@app.route("/reflect", methods=["POST"])
@login_required
def reflect():
    db = get_db()
    user_id = current_user_id()
    log_date = request.form["log_date"]
    text = request.form.get("text", "")
    sentiment, tags = analyze_reflection(text)
    db.execute(
        """
        INSERT INTO reflections (user_id, log_date, text, sentiment, tags) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, log_date) DO UPDATE SET text = excluded.text, sentiment = excluded.sentiment, tags = excluded.tags
        """,
        (user_id, log_date, text, sentiment, tags),
    )
    db.commit()
    return redirect(url_for("day_view", log_date=log_date))


# --- Habit management ---

@app.route("/habits")
@login_required
def habits():
    db = get_db()
    user_id = current_user_id()
    all_habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? ORDER BY active DESC, category, id", (user_id,)
    ).fetchall()
    active_total = sum(h["weight"] for h in all_habits if h["active"])
    categories = sorted({h["category"] for h in all_habits}) or list(CATEGORY_COLORS.keys())
    return render_template(
        "habits.html", habits=all_habits, active_total=active_total, categories=categories
    )


@app.route("/habits/add", methods=["POST"])
@login_required
def add_habit():
    db = get_db()
    user_id = current_user_id()
    category = request.form["category"].strip()
    name = request.form["name"].strip()
    weight = max(1, int(request.form.get("weight", 5)))
    if category and name:
        db.execute(
            "INSERT INTO habits (user_id, category, name, weight) VALUES (?, ?, ?, ?)",
            (user_id, category, name, weight),
        )
        db.commit()
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/update", methods=["POST"])
@login_required
def update_habit(habit_id):
    db = get_db()
    user_id = current_user_id()
    owns = db.execute("SELECT id FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)).fetchone()
    if not owns:
        return redirect(url_for("habits"))
    category = request.form["category"].strip()
    name = request.form["name"].strip()
    weight = max(1, int(request.form.get("weight", 5)))
    db.execute(
        "UPDATE habits SET category = ?, name = ?, weight = ? WHERE id = ? AND user_id = ?",
        (category, name, weight, habit_id, user_id),
    )
    db.commit()
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/toggle-active", methods=["POST"])
@login_required
def toggle_habit_active(habit_id):
    db = get_db()
    user_id = current_user_id()
    row = db.execute(
        "SELECT active FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
    ).fetchone()
    if row is not None:
        db.execute(
            "UPDATE habits SET active = ? WHERE id = ? AND user_id = ?",
            (0 if row["active"] else 1, habit_id, user_id),
        )
        db.commit()
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
@login_required
def delete_habit(habit_id):
    db = get_db()
    user_id = current_user_id()
    owns = db.execute("SELECT id FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)).fetchone()
    if not owns:
        return redirect(url_for("habits"))
    db.execute("DELETE FROM logs WHERE habit_id = ? AND user_id = ?", (habit_id, user_id))
    db.execute("DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id))
    db.commit()
    return redirect(url_for("habits"))


# --- Insights ---

def compute_insights(db, user_id):
    """Correlate each category's daily completion % with the overall daily score.
    Needs several distinct logged days to say anything meaningful."""
    log_dates = [
        r["log_date"] for r in db.execute(
            "SELECT DISTINCT log_date FROM logs WHERE user_id = ? ORDER BY log_date", (user_id,)
        ).fetchall()
    ]
    daily = [get_day_data(db, user_id, d) for d in log_dates]

    all_categories = sorted({
        cat for d in daily for cat in d["by_category"].keys()
    })

    series_overall = [d["overall_pct"] for d in daily]
    category_series = {}
    for cat in all_categories:
        series = []
        for d in daily:
            cat_data = d["by_category"].get(cat)
            pct = round((cat_data["earned"] / cat_data["total"]) * 100) if cat_data and cat_data["total"] else None
            series.append(pct)
        category_series[cat] = series

    def pearson(xs, ys):
        pairs = [(x, y) for x, y in zip(xs, ys) if x is not None]
        n = len(pairs)
        if n < 3:
            return None
        mean_x = sum(p[0] for p in pairs) / n
        mean_y = sum(p[1] for p in pairs) / n
        cov = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs)
        var_x = sum((p[0] - mean_x) ** 2 for p in pairs)
        var_y = sum((p[1] - mean_y) ** 2 for p in pairs)
        if var_x == 0 or var_y == 0:
            return None
        return cov / (var_x ** 0.5 * var_y ** 0.5)

    correlations = []
    for cat, series in category_series.items():
        r = pearson(series, series_overall)
        avg = round(sum(v for v in series if v is not None) / max(1, len([v for v in series if v is not None])))
        correlations.append({"category": cat, "r": round(r, 2) if r is not None else None, "avg": avg})

    correlations.sort(key=lambda c: (c["r"] is None, -(c["r"] or 0)))

    return {
        "logged_days": len(log_dates),
        "correlations": correlations,
        "enough_data": len(log_dates) >= 5,
    }


def compute_reflection_insights(db, user_id):
    rows = db.execute(
        "SELECT log_date, sentiment, tags FROM reflections WHERE user_id = ? AND text IS NOT NULL AND text != ''",
        (user_id,),
    ).fetchall()
    if not rows:
        return {"reflection_count": 0, "tag_insights": [], "avg_sentiment": None}

    date_pct = {r["log_date"]: get_day_data(db, user_id, r["log_date"])["overall_pct"] for r in rows}
    tag_to_dates = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            if t:
                tag_to_dates.setdefault(t, set()).add(r["log_date"])

    all_dates = set(date_pct.keys())
    tag_insights = []
    for tag, dates in tag_to_dates.items():
        if len(dates) < 2:
            continue
        with_pcts = [date_pct[d] for d in dates]
        without_dates = all_dates - dates
        without_pcts = [date_pct[d] for d in without_dates]
        avg_with = round(sum(with_pcts) / len(with_pcts))
        avg_without = round(sum(without_pcts) / len(without_pcts)) if without_pcts else None
        tag_insights.append({
            "tag": tag,
            "count": len(dates),
            "avg_with": avg_with,
            "avg_without": avg_without,
            "diff": (avg_with - avg_without) if avg_without is not None else None,
        })
    tag_insights.sort(key=lambda t: (t["diff"] is None, -(abs(t["diff"]) if t["diff"] is not None else 0)))

    sentiments = [r["sentiment"] for r in rows if r["sentiment"] is not None]
    avg_sentiment = round(sum(sentiments) / len(sentiments), 2) if sentiments else None

    return {
        "reflection_count": len(rows),
        "tag_insights": tag_insights,
        "avg_sentiment": avg_sentiment,
    }


def compute_suggested_weights(db, user_id):
    """Propose new habit weights: categories that move with the overall
    score more get more weight, ones that don't get less. Directional
    guidance based on correlation, not a guarantee — a category's score is
    part of the overall score by construction, so this is a nudge, not proof."""
    insights_data = compute_insights(db, user_id)
    if not insights_data["enough_data"]:
        return None

    rs = {c["category"]: (c["r"] if c["r"] is not None else 0) for c in insights_data["correlations"]}
    if not rs:
        return None

    adjusted = {cat: max(r, 0) + 0.05 for cat, r in rs.items()}
    total_adjusted = sum(adjusted.values())
    shares = {cat: adjusted[cat] / total_adjusted for cat in adjusted}

    active_habits = get_active_habits(db, user_id)
    by_cat = {}
    for h in active_habits:
        by_cat.setdefault(h["category"], []).append(h)
    if not by_cat:
        return None

    suggestions = []
    for cat, habits_list in by_cat.items():
        current_total = sum(h["weight"] for h in habits_list)
        target_total = round(shares.get(cat, 1 / len(by_cat)) * 100)
        for h in habits_list:
            ratio = (h["weight"] / current_total) if current_total else (1 / len(habits_list))
            new_weight = max(1, round(target_total * ratio))
            if new_weight != h["weight"]:
                suggestions.append({
                    "id": h["id"], "name": h["name"], "category": cat,
                    "old_weight": h["weight"], "new_weight": new_weight,
                })

    return {"suggestions": suggestions, "has_changes": len(suggestions) > 0}


MIN_PREDICTIVE_ROWS = 7


def compute_predictive_model(db, user_id):
    """Learns, per user, which categories actually predict how positive their
    daily reflection reads — using reflection sentiment as the target rather
    than the overall score, since the overall score is defined BY the
    category weights and would make this circular. Sentiment comes from the
    user's own words, so it's an independent signal worth learning from."""
    rows = db.execute(
        "SELECT log_date, sentiment FROM reflections WHERE user_id = ? AND sentiment IS NOT NULL ORDER BY log_date",
        (user_id,),
    ).fetchall()
    if len(rows) < MIN_PREDICTIVE_ROWS:
        return {"ready": False, "rows": len(rows), "needed": MIN_PREDICTIVE_ROWS}

    categories = sorted({h["category"] for h in get_active_habits(db, user_id)})
    if not categories:
        return {"ready": False, "rows": len(rows), "needed": MIN_PREDICTIVE_ROWS}

    import numpy as np

    X, y = [], []
    for r in rows:
        day = get_day_data(db, user_id, r["log_date"])
        feat = []
        for cat in categories:
            cd = day["by_category"].get(cat)
            pct = (cd["earned"] / cd["total"]) if cd and cd["total"] else 0.0
            feat.append(pct)
        X.append(feat)
        y.append(r["sentiment"])

    X_arr = np.array(X)
    X_with_intercept = np.hstack([np.ones((X_arr.shape[0], 1)), X_arr])
    y_arr = np.array(y)
    coeffs, *_ = np.linalg.lstsq(X_with_intercept, y_arr, rcond=None)
    intercept = float(coeffs[0])
    weights = {cat: round(float(w), 2) for cat, w in zip(categories, coeffs[1:])}

    return {
        "ready": True, "rows": len(rows), "needed": MIN_PREDICTIVE_ROWS,
        "intercept": round(intercept, 2), "weights": weights, "categories": categories,
    }


def predict_today_tone(model, today_category_pcts):
    if not model or not model.get("ready"):
        return None
    score = model["intercept"]
    for cat, w in model["weights"].items():
        score += w * (today_category_pcts.get(cat, 0) / 100.0)
    return round(max(-1.0, min(1.0, score)), 2)


MIN_ANOMALY_DAYS = 7
ANOMALY_Z_THRESHOLD = 1.25


def compute_anomaly(db, user_id, today_pct):
    """Flags when today looks statistically unusual next to the user's own
    history — gentle, descriptive framing only, never alarmist."""
    log_dates = db.execute(
        "SELECT DISTINCT log_date FROM logs WHERE user_id = ? AND log_date != ?",
        (user_id, date.today().isoformat()),
    ).fetchall()
    if len(log_dates) < MIN_ANOMALY_DAYS:
        return None
    history_pcts = [get_day_data(db, user_id, r["log_date"])["overall_pct"] for r in log_dates]
    mean = statistics.mean(history_pcts)
    stdev = statistics.pstdev(history_pcts)
    if stdev == 0:
        return None
    z = (today_pct - mean) / stdev
    if z <= -ANOMALY_Z_THRESHOLD:
        return {
            "type": "low", "z": round(z, 2), "mean": round(mean),
            "message": f"A bit below your usual pattern (you typically land around {round(mean)}%). No big deal — tomorrow's a clean reset.",
        }
    if z >= ANOMALY_Z_THRESHOLD:
        return {
            "type": "high", "z": round(z, 2), "mean": round(mean),
            "message": f"Well above your usual pattern (you typically land around {round(mean)}%). Nice day.",
        }
    return None


def build_trend_svg(trend_points, width=600, height=160, pad_x=16, pad_y=18):
    n = len(trend_points)
    if n < 2:
        return None
    xs = [pad_x + (width - 2 * pad_x) * i / (n - 1) for i in range(n)]
    ys = [pad_y + (height - 2 * pad_y) * (1 - p["pct"] / 100) for p in trend_points]
    line_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area_d = line_d + f" L {xs[-1]:.1f},{height - pad_y:.1f} L {xs[0]:.1f},{height - pad_y:.1f} Z"
    points = [
        {"x": round(x, 1), "y": round(y, 1), "date": p["date"], "pct": p["pct"]}
        for x, y, p in zip(xs, ys, trend_points)
    ]
    return {"line_d": line_d, "area_d": area_d, "points": points, "width": width, "height": height}


def compute_trend(db, user_id, days=30):
    today = date.today()
    start = today - timedelta(days=days - 1)
    log_dates = db.execute(
        "SELECT DISTINCT log_date FROM logs WHERE user_id = ? AND log_date >= ? ORDER BY log_date",
        (user_id, start.isoformat()),
    ).fetchall()
    trend_points = [
        {"date": r["log_date"], "pct": get_day_data(db, user_id, r["log_date"])["overall_pct"]}
        for r in log_dates
    ]
    return {"trend_points": trend_points, "trend_svg": build_trend_svg(trend_points)}


def compute_weekly_summary(db, user_id):
    today = date.today()
    this_week_start = today - timedelta(days=6)
    last_week_start = today - timedelta(days=13)
    last_week_end = today - timedelta(days=7)

    def avg_pct_for_range(start, end):
        dates = [
            r["log_date"] for r in db.execute(
                "SELECT DISTINCT log_date FROM logs WHERE user_id = ? AND log_date >= ? AND log_date <= ?",
                (user_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        ]
        if not dates:
            return None, 0
        pcts = [get_day_data(db, user_id, d)["overall_pct"] for d in dates]
        return round(sum(pcts) / len(pcts)), len(dates)

    def category_averages(start, end):
        dates = [
            r["log_date"] for r in db.execute(
                "SELECT DISTINCT log_date FROM logs WHERE user_id = ? AND log_date >= ? AND log_date <= ?",
                (user_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        ]
        totals = {}
        for d in dates:
            day = get_day_data(db, user_id, d)
            for cat, cd in day["by_category"].items():
                if cd["total"]:
                    totals.setdefault(cat, []).append(round((cd["earned"] / cd["total"]) * 100))
        return {cat: round(sum(v) / len(v)) for cat, v in totals.items()}

    this_avg, this_days = avg_pct_for_range(this_week_start, today)
    last_avg, last_days = avg_pct_for_range(last_week_start, last_week_end)
    cat_avgs = category_averages(this_week_start, today)

    if this_days == 0:
        return {"has_data": False}

    strongest = max(cat_avgs, key=cat_avgs.get) if cat_avgs else None
    weakest = min(cat_avgs, key=cat_avgs.get) if cat_avgs else None

    trend = None
    if last_avg is not None:
        diff = this_avg - last_avg
        if diff > 3:
            trend = "up"
        elif diff < -3:
            trend = "down"
        else:
            trend = "flat"

    return {
        "has_data": True,
        "this_avg": this_avg, "this_days": this_days,
        "last_avg": last_avg, "last_days": last_days,
        "trend": trend,
        "strongest": strongest, "strongest_pct": cat_avgs.get(strongest) if strongest else None,
        "weakest": weakest, "weakest_pct": cat_avgs.get(weakest) if weakest else None,
    }


@app.route("/insights")
@login_required
def insights():
    db = get_db()
    user_id = current_user_id()
    data = compute_insights(db, user_id)
    data.update(compute_reflection_insights(db, user_id))
    data["weight_suggestions"] = compute_suggested_weights(db, user_id)
    data.update(compute_trend(db, user_id))
    data["predictive_model"] = compute_predictive_model(db, user_id)
    data["weekly_summary"] = compute_weekly_summary(db, user_id)
    return render_template("insights.html", **data)


@app.route("/insights/apply-weights", methods=["POST"])
@login_required
def apply_weights():
    db = get_db()
    user_id = current_user_id()
    suggestion = compute_suggested_weights(db, user_id)
    if suggestion:
        for s in suggestion["suggestions"]:
            db.execute(
                "UPDATE habits SET weight = ? WHERE id = ? AND user_id = ?",
                (s["new_weight"], s["id"], user_id),
            )
        db.commit()
    return redirect(url_for("insights"))


@app.route("/history")
@login_required
def history():
    db = get_db()
    user_id = current_user_id()
    weeks = 10
    days_count = weeks * 7
    today = date.today()
    start = today - timedelta(days=days_count - 1)

    logged_dates = {
        r["log_date"] for r in db.execute(
            "SELECT DISTINCT log_date FROM logs WHERE user_id = ? AND log_date >= ?",
            (user_id, start.isoformat()),
        ).fetchall()
    }

    pad = start.weekday()  # Monday = 0, so the grid's first column starts aligned
    cells = [None] * pad
    for i in range(days_count):
        d = start + timedelta(days=i)
        d_iso = d.isoformat()
        if d_iso in logged_dates:
            pct = get_day_data(db, user_id, d_iso)["overall_pct"]
        else:
            pct = None
        cells.append({"date": d_iso, "pct": pct, "is_today": d == today})

    logged_days = total_logged_days(db, user_id)
    return render_template(
        "history.html",
        cells=cells,
        logged_days=logged_days,
        baseline_target=BASELINE_TARGET_DAYS,
    )


@app.route("/export/json")
@login_required
def export_json():
    db = get_db()
    user_id = current_user_id()
    habits = [dict(h) for h in db.execute(
        "SELECT id, category, name, weight, active FROM habits WHERE user_id = ? ORDER BY category, id", (user_id,)
    ).fetchall()]
    logs = [dict(l) for l in db.execute(
        """SELECT logs.log_date, logs.completed, habits.name AS habit_name, habits.category
           FROM logs JOIN habits ON logs.habit_id = habits.id
           WHERE logs.user_id = ? ORDER BY logs.log_date""", (user_id,)
    ).fetchall()]
    reflections = [dict(r) for r in db.execute(
        "SELECT log_date, text, sentiment, tags FROM reflections WHERE user_id = ? ORDER BY log_date", (user_id,)
    ).fetchall()]
    payload = {"habits": habits, "logs": logs, "reflections": reflections}
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=dayring_export.json"},
    )


@app.route("/export/csv")
@login_required
def export_csv():
    db = get_db()
    user_id = current_user_id()
    log_dates = [r["log_date"] for r in db.execute(
        "SELECT DISTINCT log_date FROM logs WHERE user_id = ? ORDER BY log_date", (user_id,)
    ).fetchall()]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "overall_pct", "reflection_text", "reflection_sentiment", "reflection_tags"])
    for d in log_dates:
        day = get_day_data(db, user_id, d)
        writer.writerow([
            d, day["overall_pct"], day["reflection_text"] or "",
            day["reflection_sentiment"] if day["reflection_sentiment"] is not None else "",
            ",".join(day["reflection_tags"]),
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=dayring_export.csv"},
    )


@app.route("/service-worker.js")
def service_worker():
    return app.send_static_file("service-worker.js")


# Runs on import — so tables get created/migrated whether this is started
# with `python app.py` locally or by a production server like gunicorn,
# which imports this module and never executes the block below.
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
