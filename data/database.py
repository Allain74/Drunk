import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "alcootracker.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id     INTEGER PRIMARY KEY,
                username        TEXT NOT NULL,
                weight_kg       REAL NOT NULL,
                gender          TEXT NOT NULL CHECK(gender IN ('homme', 'femme')),
                latitude        REAL,
                longitude       REAL,
                location_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER NOT NULL,
                started_at      TEXT NOT NULL DEFAULT (datetime('now')),
                active          INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS drink_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      INTEGER NOT NULL,
                telegram_id     INTEGER NOT NULL,
                drink_key       TEXT NOT NULL,
                alc_grams       REAL NOT NULL,
                logged_at       TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
        """)


def upsert_user(telegram_id: int, username: str, weight_kg: float, gender: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (telegram_id, username, weight_kg, gender)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username, weight_kg=excluded.weight_kg, gender=excluded.gender
        """, (telegram_id, username, weight_kg, gender))


def get_user(telegram_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()


def get_all_users() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users").fetchall()


def update_location(telegram_id: int, lat: float, lon: float):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET latitude=?, longitude=?, location_at=? WHERE telegram_id=?",
            (lat, lon, now, telegram_id)
        )


def start_session(telegram_id: int) -> int:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET active=0 WHERE telegram_id=? AND active=1", (telegram_id,))
        cur = conn.execute("INSERT INTO sessions (telegram_id) VALUES (?)", (telegram_id,))
        return cur.lastrowid


def get_active_session(telegram_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE telegram_id=? AND active=1", (telegram_id,)
        ).fetchone()


def end_session(telegram_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET active=0 WHERE telegram_id=? AND active=1", (telegram_id,))


def log_drink(telegram_id: int, drink_key: str, alc_grams: float) -> bool:
    session = get_active_session(telegram_id)
    if not session:
        return False
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO drink_logs (session_id, telegram_id, drink_key, alc_grams) VALUES (?, ?, ?, ?)",
            (session["id"], telegram_id, drink_key, alc_grams)
        )
    return True


def get_session_drinks_detail(telegram_id: int) -> list[sqlite3.Row]:
    session = get_active_session(telegram_id)
    if not session:
        return []
    with get_conn() as conn:
        return conn.execute(
            "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
            (session["id"],)
        ).fetchall()


def get_session_drinks(telegram_id: int) -> list[tuple[float, datetime]]:
    session = get_active_session(telegram_id)
    if not session:
        return []
    cutoff = (datetime.now(timezone.utc).timestamp() - 86400)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
            (session["id"],)
        ).fetchall()
    return [
        (r["alc_grams"], datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc))
        for r in rows
        if datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc).timestamp() >= cutoff
    ]


def get_all_active_drinks() -> dict[int, list[tuple[float, datetime]]]:
    cutoff = (datetime.now(timezone.utc).timestamp() - 86400)
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT dl.telegram_id, dl.alc_grams, dl.logged_at
            FROM drink_logs dl JOIN sessions s ON dl.session_id=s.id
            WHERE s.active=1 ORDER BY dl.logged_at
        """).fetchall()
    result: dict[int, list] = {}
    for r in rows:
        t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc)
        if t.timestamp() >= cutoff:
            result.setdefault(r["telegram_id"], []).append((r["alc_grams"], t))
    return result
