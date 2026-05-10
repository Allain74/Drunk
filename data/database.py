import os
import hashlib
import httpx
from datetime import datetime, timezone

_RAW_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_URL = _RAW_URL.replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

_client = httpx.Client(timeout=10)


def _args(values: list) -> list:
    result = []
    for v in (values or []):
        if v is None:
            result.append({"type": "null", "value": None})
        elif isinstance(v, bool):
            result.append({"type": "integer", "value": "1" if v else "0"})
        elif isinstance(v, int):
            result.append({"type": "integer", "value": str(v)})
        elif isinstance(v, float):
            result.append({"type": "float", "value": v})
        else:
            result.append({"type": "text", "value": str(v)})
    return result


def _pipeline(statements: list[tuple[str, list]]) -> list[dict]:
    requests = [
        {"type": "execute", "stmt": {"sql": sql, "args": _args(args)}}
        for sql, args in statements
    ]
    requests.append({"type": "close"})
    r = _client.post(
        f"{TURSO_URL}/v2/pipeline",
        json={"requests": requests},
        headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
    )
    if not r.is_success:
        raise Exception(f"Turso {r.status_code}: {r.text}")
    parsed = []
    for res in r.json()["results"]:
        if res["type"] == "error":
            raise Exception(res["error"]["message"])
        if res["type"] == "ok" and res["response"]["type"] == "execute":
            data = res["response"]["result"]
            cols = [c["name"] for c in data["cols"]]
            rows = []
            for row in data["rows"]:
                d = {}
                for i, cell in enumerate(row):
                    t = cell["type"]
                    v = cell.get("value")
                    if t == "null" or v is None:
                        d[cols[i]] = None
                    elif t == "integer":
                        d[cols[i]] = int(v)
                    elif t == "float":
                        d[cols[i]] = float(v)
                    else:
                        d[cols[i]] = v
                rows.append(d)
            parsed.append({
                "rows": rows,
                "last_insert_rowid": data.get("last_insert_rowid"),
                "affected_row_count": data.get("affected_row_count", 0),
            })
    return parsed


def _execute(sql: str, args=None) -> dict:
    return _pipeline([(sql, args or [])])[0]


def _fetchall(sql: str, args=None) -> list[dict]:
    return _execute(sql, args)["rows"]


def _fetchone(sql: str, args=None) -> dict | None:
    rows = _fetchall(sql, args)
    return rows[0] if rows else None


def _hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode(), 100_000)
    return f"{salt}:{dk.hex()}"

def _check_password(password: str, stored: str) -> bool:
    try:
        salt, dk_hex = stored.split(":", 1)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode(), 100_000)
        return dk.hex() == dk_hex
    except Exception:
        return False

def set_password(telegram_id: int, password: str):
    _execute("UPDATE users SET password_hash=? WHERE telegram_id=?",
             [_hash_password(password), telegram_id])

def verify_password(username: str, password: str) -> dict | None:
    """Retourne le user si le pseudo+password est correct, sinon None."""
    user = _fetchone("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", [username])
    if not user:
        return None
    stored = user.get("password_hash") or ""
    if not stored:
        return None  # password non configuré
    if _check_password(password, stored):
        return user
    return None


def init_db():
    try:
        _execute("ALTER TABLE users ADD COLUMN max_bac REAL DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN last_inactivity_notif TEXT")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 100")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")
    except Exception:
        pass
    _pipeline([
        ("""CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username    TEXT NOT NULL,
            weight_kg   REAL NOT NULL,
            gender      TEXT NOT NULL,
            latitude    REAL,
            longitude   REAL,
            location_at TEXT
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            started_at  TEXT NOT NULL DEFAULT (datetime('now')),
            active      INTEGER NOT NULL DEFAULT 1
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS drink_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            drink_key   TEXT NOT NULL,
            alc_grams   REAL NOT NULL,
            logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
        ("CREATE TABLE IF NOT EXISTS banned_users (telegram_id INTEGER PRIMARY KEY)", []),
        ("""CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            reason      TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS bets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER NOT NULL,
            opponent_id   INTEGER NOT NULL,
            bet_type      TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            end_time      TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            winner_id     INTEGER,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS blackjack_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT NOT NULL UNIQUE,
            creator_id INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'waiting',
            deck       TEXT NOT NULL DEFAULT '[]',
            dealer_hand TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS blackjack_players (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            bet        INTEGER NOT NULL DEFAULT 0,
            hand       TEXT NOT NULL DEFAULT '[]',
            status     TEXT NOT NULL DEFAULT 'waiting',
            result     TEXT
        )""", []),
    ])
    # Follows table
    _execute("""CREATE TABLE IF NOT EXISTS follows (
        follower_id  INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        PRIMARY KEY (follower_id, following_id)
    )""")
    # Populate: everyone follows everyone by default (idempotent)
    _all = get_all_users()
    _pairs = [
        ("INSERT OR IGNORE INTO follows (follower_id, following_id) VALUES (?, ?)",
         [u1["telegram_id"], u2["telegram_id"]])
        for u1 in _all for u2 in _all
        if u1["telegram_id"] != u2["telegram_id"]
    ]
    if _pairs:
        _pipeline(_pairs)


def upsert_user(telegram_id: int, username: str, weight_kg: float, gender: str):
    existing = _fetchone("SELECT telegram_id FROM users WHERE telegram_id=?", [telegram_id])
    if existing:
        _execute(
            "UPDATE users SET username=?, weight_kg=?, gender=? WHERE telegram_id=?",
            [username, weight_kg, gender, telegram_id]
        )
    else:
        _execute(
            "INSERT INTO users (telegram_id, username, weight_kg, gender) VALUES (?, ?, ?, ?)",
            [telegram_id, username, weight_kg, gender]
        )


def get_user(telegram_id: int) -> dict | None:
    return _fetchone("SELECT * FROM users WHERE telegram_id=?", [telegram_id])


def is_banned(telegram_id: int) -> bool:
    return _fetchone("SELECT 1 FROM banned_users WHERE telegram_id=?", [telegram_id]) is not None

def ban_user(telegram_id: int):
    _execute("INSERT OR IGNORE INTO banned_users (telegram_id) VALUES (?)", [telegram_id])

def unban_user(telegram_id: int):
    _execute("DELETE FROM banned_users WHERE telegram_id=?", [telegram_id])

def rename_user(telegram_id: int, new_name: str):
    _execute("UPDATE users SET username=? WHERE telegram_id=?", [new_name, telegram_id])

def get_user_by_username(username: str) -> dict | None:
    return _fetchone("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", [username])


def is_username_taken(username: str, exclude_telegram_id: int) -> bool:
    row = _fetchone(
        "SELECT telegram_id FROM users WHERE LOWER(username)=LOWER(?) AND telegram_id != ?",
        [username, exclude_telegram_id]
    )
    return row is not None


def get_all_users() -> list[dict]:
    return _fetchall("SELECT * FROM users")


def update_max_bac(telegram_id: int, bac: float):
    _execute(
        "UPDATE users SET max_bac=? WHERE telegram_id=? AND (max_bac IS NULL OR max_bac < ?)",
        [bac, telegram_id, bac]
    )


def update_location(telegram_id: int, lat: float, lon: float):
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE users SET latitude=?, longitude=?, location_at=? WHERE telegram_id=?",
        [lat, lon, now, telegram_id]
    )


def start_session(telegram_id: int) -> int:
    results = _pipeline([
        ("UPDATE sessions SET active=0 WHERE telegram_id=? AND active=1", [telegram_id]),
        ("INSERT INTO sessions (telegram_id) VALUES (?)", [telegram_id]),
    ])
    return int(results[-1]["last_insert_rowid"])


def get_active_session(telegram_id: int) -> dict | None:
    return _fetchone(
        "SELECT * FROM sessions WHERE telegram_id=? AND active=1", [telegram_id]
    )


def end_session(telegram_id: int):
    _execute("UPDATE sessions SET active=0 WHERE telegram_id=? AND active=1", [telegram_id])


def log_drink(telegram_id: int, drink_key: str, alc_grams: float) -> bool:
    session = get_active_session(telegram_id)
    if not session:
        return False
    _execute(
        "INSERT INTO drink_logs (session_id, telegram_id, drink_key, alc_grams) VALUES (?, ?, ?, ?)",
        [session["id"], telegram_id, drink_key, alc_grams]
    )
    return True


def delete_last_drink(telegram_id: int) -> str | None:
    session = get_active_session(telegram_id)
    if not session:
        return None
    row = _fetchone(
        "SELECT id, drink_key FROM drink_logs WHERE session_id=? ORDER BY logged_at DESC LIMIT 1",
        [session["id"]]
    )
    if not row:
        return None
    _execute("DELETE FROM drink_logs WHERE id=?", [row["id"]])
    return row["drink_key"]


def get_session_drinks_detail(telegram_id: int) -> list[dict]:
    session = get_active_session(telegram_id)
    if not session:
        return []
    return _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session["id"]]
    )


def get_session_drinks(telegram_id: int) -> list[tuple[float, datetime]]:
    session = get_active_session(telegram_id)
    if not session:
        # Cherche la session la plus récente même fermée (< 48h) pour afficher le bon TAC
        session = _fetchone(
            "SELECT * FROM sessions WHERE telegram_id=? AND started_at >= datetime('now', '-48 hours') ORDER BY id DESC LIMIT 1",
            [telegram_id]
        )
    if not session:
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - 172800  # 48h
    rows = _fetchall(
        "SELECT alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session["id"]]
    )
    return [
        (r["alc_grams"], datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc))
        for r in rows
        if datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc).timestamp() >= cutoff
    ]


def get_all_active_drinks() -> dict[int, list[tuple[float, datetime]]]:
    # Cutoff 48h — évite qu'une longue soirée ou une session fermée trop tôt fasse tomber le TAC à 0
    cutoff = datetime.now(timezone.utc).timestamp() - 172800
    rows = _fetchall("""
        SELECT dl.telegram_id, dl.alc_grams, dl.logged_at
        FROM drink_logs dl JOIN sessions s ON dl.session_id=s.id
        WHERE s.active=1
           OR (s.active=0 AND s.started_at >= datetime('now', '-48 hours'))
        ORDER BY dl.logged_at
    """)
    result: dict[int, list] = {}
    for r in rows:
        t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc)
        if t.timestamp() >= cutoff:
            result.setdefault(r["telegram_id"], []).append((r["alc_grams"], t))
    return result


def get_all_time_stats() -> list[dict]:
    rows = _fetchall("""
        SELECT u.username, dl.drink_key, COUNT(*) as count, SUM(dl.alc_grams) as total_alc
        FROM drink_logs dl
        JOIN users u ON dl.telegram_id = u.telegram_id
        GROUP BY u.telegram_id, dl.drink_key
        ORDER BY u.username, count DESC
    """)
    days_rows = _fetchall("""
        SELECT u.username, COUNT(DISTINCT DATE(dl.logged_at)) as nb_days, MAX(u.max_bac) as max_bac
        FROM drink_logs dl
        JOIN users u ON dl.telegram_id = u.telegram_id
        GROUP BY u.telegram_id
    """)
    nb_days_map = {r["username"]: (r["nb_days"], r.get("max_bac") or 0) for r in days_rows}

    users: dict[str, dict] = {}
    for r in rows:
        name = r["username"]
        if name not in users:
            users[name] = {"username": name, "total_drinks": 0, "total_alc_g": 0.0, "breakdown": []}
        users[name]["total_drinks"] += r["count"]
        users[name]["total_alc_g"] = round(users[name]["total_alc_g"] + r["total_alc"], 1)
        users[name]["breakdown"].append({"drink_key": r["drink_key"], "count": r["count"]})

    for u in users.values():
        nb_days, max_bac = nb_days_map.get(u["username"], (1, 0))
        u["nb_days"] = nb_days
        u["avg_doses_per_day"] = round((u["total_alc_g"] / 10) / nb_days, 1)
        u["max_bac"] = round(max_bac, 2)

    return sorted(users.values(), key=lambda x: x["total_alc_g"], reverse=True)


def set_last_inactivity_notif(telegram_id: int, dt: datetime):
    _execute(
        "UPDATE users SET last_inactivity_notif=? WHERE telegram_id=?",
        [dt.isoformat(), telegram_id]
    )


def get_last_inactivity_notif(telegram_id: int) -> datetime | None:
    row = _fetchone(
        "SELECT last_inactivity_notif FROM users WHERE telegram_id=?",
        [telegram_id]
    )
    if not row or not row["last_inactivity_notif"]:
        return None
    return datetime.fromisoformat(row["last_inactivity_notif"]).replace(tzinfo=timezone.utc)


def get_last_drink_time(telegram_id: int) -> datetime | None:
    row = _fetchone(
        "SELECT logged_at FROM drink_logs WHERE telegram_id=? ORDER BY logged_at DESC LIMIT 1",
        [telegram_id]
    )
    if not row:
        return None
    return datetime.fromisoformat(row["logged_at"]).replace(tzinfo=timezone.utc)


def get_weekly_drink_logs(since: datetime) -> list[dict]:
    return _fetchall("""
        SELECT dl.telegram_id, u.username, dl.drink_key, dl.alc_grams, dl.logged_at
        FROM drink_logs dl
        JOIN users u ON dl.telegram_id = u.telegram_id
        WHERE dl.logged_at >= ?
        ORDER BY dl.telegram_id, dl.logged_at
    """, [since.isoformat()])


def get_top_drinks(telegram_id: int, n: int = 5) -> list[str]:
    rows = _fetchall("""
        SELECT drink_key, COUNT(*) as cnt
        FROM drink_logs WHERE telegram_id=?
        GROUP BY drink_key ORDER BY cnt DESC LIMIT ?
    """, [telegram_id, n])
    return [r["drink_key"] for r in rows]


def get_drinks_by_session(session_id: int) -> list[dict]:
    return _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session_id]
    )


# ── Coins & Transactions ──────────────────────────────────────────────────────

def get_coins(telegram_id: int) -> int:
    row = _fetchone("SELECT coins FROM users WHERE telegram_id=?", [telegram_id])
    return int(row["coins"] or 0) if row else 0


def add_coins(telegram_id: int, amount: int, reason: str) -> int:
    _execute("UPDATE users SET coins = coins + ? WHERE telegram_id=?", [amount, telegram_id])
    _execute(
        "INSERT INTO transactions (telegram_id, amount, reason) VALUES (?, ?, ?)",
        [telegram_id, amount, reason]
    )
    return get_coins(telegram_id)


def get_transactions(telegram_id: int, limit: int = 20) -> list[dict]:
    return _fetchall(
        "SELECT amount, reason, created_at FROM transactions WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?",
        [telegram_id, limit]
    )


def get_all_balances() -> list[dict]:
    return _fetchall("SELECT telegram_id, username, coins FROM users ORDER BY coins DESC")


# ── Bets ──────────────────────────────────────────────────────────────────────

def create_bet(challenger_id: int, opponent_id: int, bet_type: str, amount: int, end_time: str | None) -> int:
    result = _execute(
        "INSERT INTO bets (challenger_id, opponent_id, bet_type, amount, end_time) VALUES (?, ?, ?, ?, ?)",
        [challenger_id, opponent_id, bet_type, amount, end_time]
    )
    return int(result["last_insert_rowid"])


def get_pending_bet_for(opponent_id: int) -> dict | None:
    return _fetchone(
        "SELECT * FROM bets WHERE opponent_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
        [opponent_id]
    )


def accept_bet(bet_id: int):
    _execute("UPDATE bets SET status='active' WHERE id=?", [bet_id])


def cancel_bet(bet_id: int):
    _execute("UPDATE bets SET status='cancelled' WHERE id=?", [bet_id])


def get_active_bets() -> list[dict]:
    return _fetchall("SELECT * FROM bets WHERE status='active'")


def settle_bet(bet_id: int, winner_id: int):
    _execute("UPDATE bets SET status='settled', winner_id=? WHERE id=?", [winner_id, bet_id])


def get_bet(bet_id: int) -> dict | None:
    return _fetchone("SELECT * FROM bets WHERE id=?", [bet_id])


# ── Blackjack ─────────────────────────────────────────────────────────────────

def create_blackjack_session(creator_id: int, token: str) -> int:
    result = _execute(
        "INSERT INTO blackjack_sessions (creator_id, token) VALUES (?, ?)",
        [creator_id, token]
    )
    return int(result["last_insert_rowid"])


def get_blackjack_session(token: str) -> dict | None:
    return _fetchone("SELECT * FROM blackjack_sessions WHERE token=?", [token])


def get_blackjack_session_by_id(session_id: int) -> dict | None:
    return _fetchone("SELECT * FROM blackjack_sessions WHERE id=?", [session_id])


def update_blackjack_session(session_id: int, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    _execute(f"UPDATE blackjack_sessions SET {sets} WHERE id=?", vals)


def add_blackjack_player(session_id: int, telegram_id: int, bet: int):
    _execute(
        "INSERT OR IGNORE INTO blackjack_players (session_id, telegram_id, bet) VALUES (?, ?, ?)",
        [session_id, telegram_id, bet]
    )


def get_blackjack_players(session_id: int) -> list[dict]:
    return _fetchall("SELECT * FROM blackjack_players WHERE session_id=?", [session_id])


def update_blackjack_player(session_id: int, telegram_id: int, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id, telegram_id]
    _execute(f"UPDATE blackjack_players SET {sets} WHERE session_id=? AND telegram_id=?", vals)


def get_blackjack_session_by_player(telegram_id: int) -> dict | None:
    return _fetchone("""
        SELECT bs.* FROM blackjack_sessions bs
        JOIN blackjack_players bp ON bs.id = bp.session_id
        WHERE bp.telegram_id=? AND bs.status IN ('waiting', 'active')
        ORDER BY bs.created_at DESC LIMIT 1
    """, [telegram_id])


def _get_waiting_session_by_creator(telegram_id: int) -> dict | None:
    return _fetchone(
        "SELECT * FROM blackjack_sessions WHERE creator_id=? AND status='waiting' ORDER BY created_at DESC LIMIT 1",
        [telegram_id]
    )


# ── Follows ───────────────────────────────────────────────────────────────────

def follow_user(follower_id: int, following_id: int):
    _execute("INSERT OR IGNORE INTO follows (follower_id, following_id) VALUES (?, ?)",
             [follower_id, following_id])

def unfollow_user(follower_id: int, following_id: int):
    _execute("DELETE FROM follows WHERE follower_id=? AND following_id=?",
             [follower_id, following_id])

def is_following(follower_id: int, following_id: int) -> bool:
    return _fetchone("SELECT 1 FROM follows WHERE follower_id=? AND following_id=?",
                     [follower_id, following_id]) is not None

def get_following(follower_id: int) -> list[int]:
    rows = _fetchall("SELECT following_id FROM follows WHERE follower_id=?", [follower_id])
    return [r["following_id"] for r in rows]


def delete_user(telegram_id: int):
    """Supprime un compte et toutes ses données associées."""
    _pipeline([
        ("DELETE FROM drink_logs   WHERE telegram_id=?", [telegram_id]),
        ("DELETE FROM sessions     WHERE telegram_id=?", [telegram_id]),
        ("DELETE FROM transactions WHERE telegram_id=?", [telegram_id]),
        ("DELETE FROM follows      WHERE follower_id=? OR following_id=?", [telegram_id, telegram_id]),
        ("DELETE FROM bets         WHERE challenger_id=? OR opponent_id=?", [telegram_id, telegram_id]),
        ("DELETE FROM blackjack_players WHERE telegram_id=?", [telegram_id]),
        ("DELETE FROM banned_users WHERE telegram_id=?", [telegram_id]),
        ("DELETE FROM users        WHERE telegram_id=?", [telegram_id]),
    ])
