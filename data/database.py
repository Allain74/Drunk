import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()


def get_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id  BIGINT PRIMARY KEY,
                    username     TEXT NOT NULL,
                    weight_kg    REAL NOT NULL,
                    gender       TEXT NOT NULL CHECK(gender IN ('homme', 'femme'))
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id           SERIAL PRIMARY KEY,
                    telegram_id  BIGINT NOT NULL REFERENCES users(telegram_id),
                    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    active       BOOLEAN NOT NULL DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS drink_logs (
                    id           SERIAL PRIMARY KEY,
                    session_id   INTEGER NOT NULL REFERENCES sessions(id),
                    telegram_id  BIGINT NOT NULL,
                    drink_key    TEXT NOT NULL,
                    alc_grams    REAL NOT NULL,
                    logged_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetchone(cur) -> dict | None:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetchall(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Utilisateurs ──────────────────────────────────────────────────────────────

def upsert_user(telegram_id: int, username: str, weight_kg: float, gender: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (telegram_id, username, weight_kg, gender)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username=EXCLUDED.username,
                    weight_kg=EXCLUDED.weight_kg,
                    gender=EXCLUDED.gender
            """, (telegram_id, username, weight_kg, gender))
        conn.commit()


def get_user(telegram_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            return _fetchone(cur)


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users")
            return _fetchall(cur)


# ── Sessions ──────────────────────────────────────────────────────────────────

def start_session(telegram_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET active=FALSE WHERE telegram_id=%s AND active=TRUE",
                (telegram_id,)
            )
            cur.execute(
                "INSERT INTO sessions (telegram_id) VALUES (%s) RETURNING id",
                (telegram_id,)
            )
            sid = cur.fetchone()[0]
        conn.commit()
        return sid


def get_active_session(telegram_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE telegram_id=%s AND active=TRUE",
                (telegram_id,)
            )
            return _fetchone(cur)


def end_session(telegram_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET active=FALSE WHERE telegram_id=%s AND active=TRUE",
                (telegram_id,)
            )
        conn.commit()


# ── Logs de boissons ──────────────────────────────────────────────────────────

def log_drink(telegram_id: int, drink_key: str, alc_grams: float) -> bool:
    session = get_active_session(telegram_id)
    if not session:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO drink_logs (session_id, telegram_id, drink_key, alc_grams)
                VALUES (%s, %s, %s, %s)
            """, (session["id"], telegram_id, drink_key, alc_grams))
        conn.commit()
    return True


def get_session_drinks(telegram_id: int) -> list[tuple[float, datetime]]:
    session = get_active_session(telegram_id)
    if not session:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT alc_grams, logged_at FROM drink_logs WHERE session_id=%s ORDER BY logged_at",
                (session["id"],)
            )
            return [(row[0], row[1]) for row in cur.fetchall()]


def get_all_active_drinks() -> dict[int, list[tuple[float, datetime]]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dl.telegram_id, dl.alc_grams, dl.logged_at
                FROM drink_logs dl
                JOIN sessions s ON dl.session_id = s.id
                WHERE s.active = TRUE
                ORDER BY dl.logged_at
            """)
            result: dict[int, list] = {}
            for tid, alc_g, logged_at in cur.fetchall():
                result.setdefault(tid, []).append((alc_g, logged_at))
            return result
