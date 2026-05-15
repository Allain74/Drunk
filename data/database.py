import os
import hashlib
import httpx
from datetime import datetime, timezone

_RAW_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_URL = _RAW_URL.replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

# Pool généreux pour absorber les bursts de calls Turso quand plusieurs users
# utilisent l'app simultanément. Sans ces limits, le default httpx (100 max
# connections, pool_timeout 5s) sature sous charge -> PoolTimeout exceptions
# -> endpoints qui fail -> downs.
_client = httpx.Client(
    timeout=httpx.Timeout(20.0, connect=5.0, pool=30.0),
    limits=httpx.Limits(
        max_connections=200,
        max_keepalive_connections=100,
        keepalive_expiry=60.0,
    ),
)


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
            ncols = len(cols)
            for row in data["rows"]:
                d = {}
                for i, cell in enumerate(row):
                    if i >= ncols:
                        break
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
                # Alias bidirectionnel user_id ↔ telegram_id (migration partielle)
                if "user_id" in d and "telegram_id" not in d:
                    d["telegram_id"] = d["user_id"]
                elif "telegram_id" in d and "user_id" not in d:
                    d["user_id"] = d["telegram_id"]
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

def set_password(user_id: int, password: str):
    _execute("UPDATE users SET password_hash=? WHERE user_id=?",
             [_hash_password(password), user_id])

def verify_password(username: str, password: str) -> dict | None:
    """Retourne le user si le pseudo+password est correct, sinon None.
    Les anciens comptes sans mot de passe peuvent se connecter avec un champ vide."""
    user = _fetchone("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", [username])
    if not user:
        return None
    stored = user.get("password_hash") or ""
    if not stored:
        # Ancien compte sans mot de passe : autorise uniquement si mdp laissé vide
        return user if password == "" else None
    if _check_password(password, stored):
        return user
    return None


def init_db():
    # ── Migration flags (one-shot migrations) ────────────────────────────────
    try:
        _execute("CREATE TABLE IF NOT EXISTS _migration_flags (key TEXT PRIMARY KEY)")
    except Exception:
        pass
    try:
        done = _fetchone("SELECT 1 FROM _migration_flags WHERE key='bj_reset_2026_05'")
        if not done:
            _pipeline([
                ("DELETE FROM blackjack_players", []),
                ("DELETE FROM blackjack_sessions", []),
            ])
            _execute("INSERT INTO _migration_flags (key) VALUES ('bj_reset_2026_05')")
    except Exception:
        pass

    # Migration: telegram_id → user_id
    for tbl, col in [("users", "telegram_id"), ("sessions", "telegram_id"),
                      ("drink_logs", "telegram_id"), ("banned_users", "telegram_id"),
                      ("transactions", "telegram_id"), ("blackjack_players", "telegram_id"),
                      ("push_subscriptions", "telegram_id")]:
        try:
            cols = [r["name"] for r in _fetchall(f"PRAGMA table_info({tbl})", [])]
            if "telegram_id" in cols and "user_id" not in cols:
                _execute(f"ALTER TABLE {tbl} RENAME COLUMN telegram_id TO user_id", [])
        except Exception:
            pass

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
    try:
        _execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
    except Exception:
        pass
    try:
        _execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT NOT NULL
        )""")
    except Exception:
        pass
    # Gamification : XP, badges, streaks
    try:
        _execute("ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN current_streak INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN longest_streak INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN last_drink_date TEXT")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    except Exception:
        pass
    try:
        _execute("""CREATE TABLE IF NOT EXISTS user_badges (
            user_id     INTEGER NOT NULL,
            badge_key   TEXT NOT NULL,
            unlocked_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, badge_key)
        )""")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN last_spin_at TEXT")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN discreet_mode INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        _execute("""CREATE TABLE IF NOT EXISTS challenge_claims (
            user_id       INTEGER NOT NULL,
            challenge_key TEXT NOT NULL,
            week_iso      TEXT NOT NULL,
            claimed_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, challenge_key, week_iso)
        )""")
    except Exception:
        pass
    try:
        _execute("ALTER TABLE users ADD COLUMN active_skin TEXT DEFAULT 'default'")
    except Exception:
        pass
    try:
        _execute("""CREATE TABLE IF NOT EXISTS user_skins (
            user_id  INTEGER NOT NULL,
            skin_key TEXT NOT NULL,
            owned_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, skin_key)
        )""")
    except Exception:
        pass
    # Reset les anciens skins "cadres" qui n'existent plus (gold/purple/neon/fire/rainbow)
    try:
        done = _fetchone("SELECT 1 FROM _migration_flags WHERE key='skins_v2_2026_05'")
        if not done:
            _execute(
                "UPDATE users SET active_skin='default' "
                "WHERE active_skin IN ('gold','purple','neon','fire','rainbow')"
            )
            _execute(
                "DELETE FROM user_skins "
                "WHERE skin_key IN ('gold','purple','neon','fire','rainbow')"
            )
            _execute("INSERT INTO _migration_flags (key) VALUES ('skins_v2_2026_05')")
    except Exception:
        pass
    _pipeline([
        ("""CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT NOT NULL,
            weight_kg   REAL NOT NULL,
            gender      TEXT NOT NULL,
            latitude    REAL,
            longitude   REAL,
            location_at TEXT
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            started_at  TEXT NOT NULL DEFAULT (datetime('now')),
            active      INTEGER NOT NULL DEFAULT 1
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS drink_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            drink_key   TEXT NOT NULL,
            alc_grams   REAL NOT NULL,
            logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
        ("CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)", []),
        ("""CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
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
            user_id    INTEGER NOT NULL,
            bet        INTEGER NOT NULL DEFAULT 0,
            hand       TEXT NOT NULL DEFAULT '[]',
            status     TEXT NOT NULL DEFAULT 'waiting',
            result     TEXT
        )""", []),
        # Historique de chaque main terminée (1 row par main, pas par session).
        # Permet de compter correctement les vraies stats W/L au lieu de garder
        # uniquement le résultat de la dernière main d'une session.
        ("""CREATE TABLE IF NOT EXISTS blackjack_hands_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            bet         INTEGER NOT NULL,
            result      TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""", []),
    ])
    # Migration one-shot : si la table history est vide, migre les rows
    # actuelles de blackjack_players avec un result (ne perd pas les stats
    # déjà accumulées).
    existing = _fetchone("SELECT COUNT(*) AS c FROM blackjack_hands_history")
    if existing and (existing.get("c") or 0) == 0:
        _execute("""
            INSERT INTO blackjack_hands_history (session_id, user_id, bet, result)
            SELECT session_id, user_id, bet, result
            FROM blackjack_players WHERE result IS NOT NULL
        """)

    # Follows table
    _execute("""CREATE TABLE IF NOT EXISTS follows (
        follower_id  INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        PRIMARY KEY (follower_id, following_id)
    )""")


def upsert_user(user_id: int, username: str, weight_kg: float, gender: str):
    existing = _fetchone("SELECT user_id FROM users WHERE user_id=?", [user_id])
    if existing:
        _execute(
            "UPDATE users SET username=?, weight_kg=?, gender=? WHERE user_id=?",
            [username, weight_kg, gender, user_id]
        )
    else:
        _execute(
            "INSERT INTO users (user_id, username, weight_kg, gender) VALUES (?, ?, ?, ?)",
            [user_id, username, weight_kg, gender]
        )


def get_user(user_id: int) -> dict | None:
    return _fetchone("SELECT * FROM users WHERE user_id=?", [user_id])


def _banned_col() -> str:
    """Détecte si la colonne s'appelle user_id (migration faite) ou
    telegram_id (héritée). Cache le résultat pour éviter les PRAGMA répétés."""
    global _BANNED_COL_CACHE
    try:
        col = _BANNED_COL_CACHE
        if col:
            return col
    except NameError:
        pass
    try:
        cols = [r["name"] for r in _fetchall("PRAGMA table_info(banned_users)", [])]
        if "user_id" in cols:
            _BANNED_COL_CACHE = "user_id"
        elif "telegram_id" in cols:
            _BANNED_COL_CACHE = "telegram_id"
        else:
            _BANNED_COL_CACHE = "user_id"  # fallback (table sera créée avec user_id)
    except Exception:
        _BANNED_COL_CACHE = "user_id"
    return _BANNED_COL_CACHE


def is_banned(user_id: int) -> bool:
    col = _banned_col()
    return _fetchone(f"SELECT 1 FROM banned_users WHERE {col}=?", [user_id]) is not None

def ban_user(user_id: int):
    col = _banned_col()
    _execute(f"INSERT OR IGNORE INTO banned_users ({col}) VALUES (?)", [user_id])

def unban_user(user_id: int):
    col = _banned_col()
    _execute(f"DELETE FROM banned_users WHERE {col}=?", [user_id])

def rename_user(user_id: int, new_name: str):
    _execute("UPDATE users SET username=? WHERE user_id=?", [new_name, user_id])

def clear_password(user_id: int):
    """Remet le mot de passe à vide (aucun mot de passe requis)."""
    _execute("UPDATE users SET password_hash='' WHERE user_id=?", [user_id])

def delete_n_drinks(user_id: int, n: int) -> int:
    """Supprime les n derniers verres de la session active. Retourne le nb supprimé."""
    session = get_active_session(user_id)
    if not session or n <= 0:
        return 0
    rows = _fetchall(
        "SELECT id FROM drink_logs WHERE session_id=? ORDER BY logged_at DESC LIMIT ?",
        [session["id"], n]
    )
    for row in rows:
        _execute("DELETE FROM drink_logs WHERE id=?", [row["id"]])
    return len(rows)

def get_user_by_username(username: str) -> dict | None:
    return _fetchone("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", [username])


def is_username_taken(username: str, exclude_user_id: int) -> bool:
    row = _fetchone(
        "SELECT user_id FROM users WHERE LOWER(username)=LOWER(?) AND user_id != ?",
        [username, exclude_user_id]
    )
    return row is not None


def get_all_users() -> list[dict]:
    return _fetchall("SELECT * FROM users")


def update_max_bac(user_id: int, bac: float):
    _execute(
        "UPDATE users SET max_bac=? WHERE user_id=? AND (max_bac IS NULL OR max_bac < ?)",
        [bac, user_id, bac]
    )


def update_location(user_id: int, lat: float, lon: float):
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE users SET latitude=?, longitude=?, location_at=? WHERE user_id=?",
        [lat, lon, now, user_id]
    )


def start_session(user_id: int) -> int:
    results = _pipeline([
        ("UPDATE sessions SET active=0 WHERE user_id=? AND active=1", [user_id]),
        ("INSERT INTO sessions (user_id) VALUES (?)", [user_id]),
    ])
    return int(results[-1]["last_insert_rowid"])


def get_active_session(user_id: int) -> dict | None:
    return _fetchone(
        "SELECT * FROM sessions WHERE user_id=? AND active=1", [user_id]
    )


def end_session(user_id: int):
    _execute("UPDATE sessions SET active=0 WHERE user_id=? AND active=1", [user_id])


def log_drink(user_id: int, drink_key: str, alc_grams: float) -> bool:
    session = get_active_session(user_id)
    if not session:
        return False
    _execute(
        "INSERT INTO drink_logs (session_id, user_id, drink_key, alc_grams) VALUES (?, ?, ?, ?)",
        [session["id"], user_id, drink_key, alc_grams]
    )
    return True


def delete_last_drink(user_id: int) -> str | None:
    session = get_active_session(user_id)
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


def get_session_drinks_detail(user_id: int) -> list[dict]:
    session = get_active_session(user_id)
    if not session:
        return []
    return _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session["id"]]
    )


def get_session_drinks(user_id: int) -> list[tuple[float, datetime]]:
    session = get_active_session(user_id)
    if not session:
        # Cherche la session la plus récente même fermée (< 48h) pour afficher le bon TAC
        session = _fetchone(
            "SELECT * FROM sessions WHERE user_id=? AND started_at >= datetime('now', '-48 hours') ORDER BY id DESC LIMIT 1",
            [user_id]
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


SESSION_TIMEOUT_SEC = 6 * 3600  # 6h sans verre → soirée terminée


def get_all_active_drinks() -> dict[int, list[tuple[float, datetime]]]:
    """Retourne les verres des soirées EN COURS uniquement.
    Une soirée est considérée terminée si le dernier verre date de plus de 6h
    (cohérent avec _ensure_session côté API qui ferme la session au prochain
    log si > 6h). On garde une fenêtre de 48h pour le calcul du BAC, qui
    s'élimine naturellement dans total_bac()."""
    cutoff_bac = datetime.now(timezone.utc).timestamp() - 172800  # 48h
    rows = _fetchall("""
        SELECT dl.user_id, dl.alc_grams, dl.logged_at
        FROM drink_logs dl JOIN sessions s ON dl.session_id=s.id
        WHERE s.active=1
           OR (s.active=0 AND s.started_at >= datetime('now', '-48 hours'))
        ORDER BY dl.user_id, dl.logged_at
    """)
    by_user: dict[int, list] = {}
    for r in rows:
        t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc)
        if t.timestamp() >= cutoff_bac:
            by_user.setdefault(r["user_id"], []).append((r["alc_grams"], t))
    # Filtre : ne garde que les users dont le dernier verre est < 6h (soirée en cours)
    now = datetime.now(timezone.utc)
    result: dict[int, list] = {}
    for uid, drinks in by_user.items():
        if not drinks:
            continue
        last_t = drinks[-1][1]
        if (now - last_t).total_seconds() > SESSION_TIMEOUT_SEC:
            continue
        result[uid] = drinks
    return result


def get_all_time_stats() -> list[dict]:
    rows = _fetchall("""
        SELECT u.username, dl.drink_key, COUNT(*) as count, SUM(dl.alc_grams) as total_alc
        FROM drink_logs dl
        JOIN users u ON dl.user_id = u.user_id
        GROUP BY u.user_id, dl.drink_key
        ORDER BY u.username, count DESC
    """)
    days_rows = _fetchall("""
        SELECT u.username, COUNT(DISTINCT DATE(dl.logged_at)) as nb_days, MAX(u.max_bac) as max_bac
        FROM drink_logs dl
        JOIN users u ON dl.user_id = u.user_id
        GROUP BY u.user_id
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


def set_last_inactivity_notif(user_id: int, dt: datetime):
    _execute(
        "UPDATE users SET last_inactivity_notif=? WHERE user_id=?",
        [dt.isoformat(), user_id]
    )


def get_last_inactivity_notif(user_id: int) -> datetime | None:
    row = _fetchone(
        "SELECT last_inactivity_notif FROM users WHERE user_id=?",
        [user_id]
    )
    if not row or not row["last_inactivity_notif"]:
        return None
    return datetime.fromisoformat(row["last_inactivity_notif"]).replace(tzinfo=timezone.utc)


def get_last_drink_time(user_id: int) -> datetime | None:
    row = _fetchone(
        "SELECT logged_at FROM drink_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 1",
        [user_id]
    )
    if not row:
        return None
    return datetime.fromisoformat(row["logged_at"]).replace(tzinfo=timezone.utc)


def get_weekly_drink_logs(since: datetime) -> list[dict]:
    return _fetchall("""
        SELECT dl.user_id, u.username, dl.drink_key, dl.alc_grams, dl.logged_at
        FROM drink_logs dl
        JOIN users u ON dl.user_id = u.user_id
        WHERE dl.logged_at >= ?
        ORDER BY dl.user_id, dl.logged_at
    """, [since.isoformat()])


def get_top_drinks(user_id: int, n: int = 5) -> list[str]:
    rows = _fetchall("""
        SELECT drink_key, COUNT(*) as cnt
        FROM drink_logs WHERE user_id=?
        GROUP BY drink_key ORDER BY cnt DESC LIMIT ?
    """, [user_id, n])
    return [r["drink_key"] for r in rows]


def get_drinks_by_session(session_id: int) -> list[dict]:
    return _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session_id]
    )


# ── Coins & Transactions ──────────────────────────────────────────────────────

def get_coins(user_id: int) -> int:
    row = _fetchone("SELECT coins FROM users WHERE user_id=?", [user_id])
    return int(row["coins"] or 0) if row else 0


def add_coins(user_id: int, amount: int, reason: str) -> int:
    _execute("UPDATE users SET coins = coins + ? WHERE user_id=?", [amount, user_id])
    _execute(
        "INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)",
        [user_id, amount, reason]
    )
    return get_coins(user_id)


def try_debit_coins(user_id: int, amount: int, reason: str) -> bool:
    """Tente de débiter `amount` coins de manière atomique.
    Retourne True si le débit a eu lieu, False si le solde était insuffisant.
    Empêche les double-spend (deux requêtes parallèles ne peuvent pas
    descendre le solde sous 0 toutes les deux)."""
    if amount <= 0:
        return False
    res = _execute(
        "UPDATE users SET coins = coins - ? WHERE user_id=? AND coins >= ?",
        [amount, user_id, amount]
    )
    if not res.get("affected_row_count"):
        return False
    _execute(
        "INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)",
        [user_id, -amount, reason]
    )
    return True


def get_transactions(user_id: int, limit: int = 20) -> list[dict]:
    return _fetchall(
        "SELECT amount, reason, created_at FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        [user_id, limit]
    )


def get_all_balances() -> list[dict]:
    return _fetchall("SELECT user_id, username, coins FROM users ORDER BY coins DESC")


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


def get_user_bets(user_id: int) -> list[dict]:
    """Retourne tous les paris impliquant cet utilisateur (hors annulés), du plus récent."""
    return _fetchall(
        "SELECT * FROM bets WHERE (challenger_id=? OR opponent_id=?) AND status != 'cancelled' ORDER BY created_at DESC LIMIT 30",
        [user_id, user_id],
    )


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


def add_blackjack_player(session_id: int, user_id: int, bet: int):
    _execute(
        "INSERT OR IGNORE INTO blackjack_players (session_id, user_id, bet) VALUES (?, ?, ?)",
        [session_id, user_id, bet]
    )


def get_blackjack_players(session_id: int) -> list[dict]:
    return _fetchall("SELECT * FROM blackjack_players WHERE session_id=?", [session_id])


def update_blackjack_player(session_id: int, user_id: int, **kwargs):
    # Si on définit un result (= fin de main), archive d'abord dans l'historique
    # pour pouvoir compter les vraies stats W/L par main jouée (pas seulement la
    # dernière main de la session, qui était écrasée à chaque rematch).
    new_result = kwargs.get("result")
    if new_result:
        existing = _fetchone(
            "SELECT bet, result FROM blackjack_players WHERE session_id=? AND user_id=?",
            [session_id, user_id]
        )
        # On archive seulement si on passe de "pas de résultat" à un résultat
        # (évite de double-compter si on appelle update_player(result=...) deux
        # fois de suite sans rematch entre les deux).
        if existing and not existing.get("result"):
            bet = int(existing.get("bet") or 0)
            _execute(
                "INSERT INTO blackjack_hands_history (session_id, user_id, bet, result) VALUES (?, ?, ?, ?)",
                [session_id, user_id, bet, new_result]
            )

    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id, user_id]
    _execute(f"UPDATE blackjack_players SET {sets} WHERE session_id=? AND user_id=?", vals)


def get_blackjack_session_by_player(user_id: int) -> dict | None:
    return _fetchone("""
        SELECT bs.* FROM blackjack_sessions bs
        JOIN blackjack_players bp ON bs.id = bp.session_id
        WHERE bp.user_id=? AND bs.status IN ('waiting', 'active')
        ORDER BY bs.created_at DESC LIMIT 1
    """, [user_id])


def _get_waiting_session_by_creator(user_id: int) -> dict | None:
    return _fetchone(
        "SELECT * FROM blackjack_sessions WHERE creator_id=? AND status='waiting' ORDER BY created_at DESC LIMIT 1",
        [user_id]
    )


def get_active_blackjack_sessions() -> list[dict]:
    """Retourne toutes les sessions en attente ou actives."""
    return _fetchall(
        "SELECT * FROM blackjack_sessions WHERE status IN ('waiting', 'active') ORDER BY created_at DESC"
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


def get_followers(following_id: int) -> list[int]:
    """Retourne les IDs de tous ceux qui suivent cet utilisateur."""
    rows = _fetchall("SELECT follower_id FROM follows WHERE following_id=?", [following_id])
    return [r["follower_id"] for r in rows]


def get_blackjack_stats(user_id: int) -> dict:
    """Stats BJ : parties jouées/gagnées/perdues/égalités + coins net cumulé.
    Compte CHAQUE main jouée (via blackjack_hands_history) — pas seulement la
    dernière main d'une session comme avant le 14/05."""
    rows = _fetchall(
        "SELECT result, bet FROM blackjack_hands_history WHERE user_id=?",
        [user_id]
    )
    played = len(rows)
    won    = sum(1 for r in rows if r["result"] in ("win", "blackjack"))
    lost   = sum(1 for r in rows if r["result"] in ("lose", "bust"))
    push   = sum(1 for r in rows if r["result"] == "push")
    coins_net = 0
    for r in rows:
        bet = int(r.get("bet") or 0)
        res = r.get("result")
        if res == "win":
            coins_net += bet
        elif res == "blackjack":
            coins_net += int(bet * 1.5)
        elif res in ("lose", "bust"):
            coins_net -= bet
        # push = 0
    return {"played": played, "won": won, "lost": lost, "push": push, "coins_net": coins_net}


def get_profile_follows(user_id: int) -> dict:
    """Retourne abonnés/abonnements (counts + listes username+id)."""
    following_ids = get_following(user_id)
    follower_ids  = get_followers(user_id)
    following_users = [get_user(fid) for fid in following_ids]
    following_users = [u for u in following_users if u]
    follower_users  = [get_user(fid) for fid in follower_ids]
    follower_users  = [u for u in follower_users if u]
    return {
        "followers_count": len(follower_ids),
        "following_count": len(following_ids),
        "following": [{"user_id": u["user_id"], "username": u["username"]} for u in following_users],
        "followers": [{"user_id": u["user_id"], "username": u["username"]} for u in follower_users],
    }


# ── Push subscriptions ────────────────────────────────────────────────────────

def init_push_subscriptions():
    _execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        endpoint    TEXT NOT NULL UNIQUE,
        p256dh      TEXT NOT NULL,
        auth        TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""")


def save_push_subscription(user_id: int, endpoint: str, p256dh: str, auth: str):
    _execute(
        """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,
               p256dh=excluded.p256dh, auth=excluded.auth""",
        [user_id, endpoint, p256dh, auth]
    )


def delete_push_subscription(endpoint: str):
    _execute("DELETE FROM push_subscriptions WHERE endpoint=?", [endpoint])


def get_push_subscriptions(user_id: int) -> list[dict]:
    return _fetchall(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
        [user_id]
    )


def get_all_follows() -> list[dict]:
    """Retourne toutes les relations de suivi : [{follower_id, following_id}]."""
    return _fetchall("SELECT follower_id, following_id FROM follows")


# ── Avatars ───────────────────────────────────────────────────────────────────

def set_avatar(user_id: int, avatar: str):
    """Stocke un avatar (data URL base64) pour un utilisateur."""
    _execute("UPDATE users SET avatar=? WHERE user_id=?", [avatar, user_id])


def get_all_avatars() -> list[dict]:
    """Retourne tous les avatars non-null sous forme [{user_id, avatar}]."""
    return _fetchall("SELECT user_id, avatar FROM users WHERE avatar IS NOT NULL")


def delete_user(user_id: int):
    """Supprime un compte et toutes ses données associées."""
    _pipeline([
        ("DELETE FROM drink_logs          WHERE user_id=?", [user_id]),
        ("DELETE FROM sessions            WHERE user_id=?", [user_id]),
        ("DELETE FROM transactions        WHERE user_id=?", [user_id]),
        ("DELETE FROM follows             WHERE follower_id=? OR following_id=?", [user_id, user_id]),
        ("DELETE FROM bets                WHERE challenger_id=? OR opponent_id=?", [user_id, user_id]),
        ("DELETE FROM blackjack_players   WHERE user_id=?", [user_id]),
        ("DELETE FROM banned_users        WHERE user_id=?", [user_id]),
        ("DELETE FROM push_subscriptions  WHERE user_id=?", [user_id]),
        ("DELETE FROM users               WHERE user_id=?", [user_id]),
    ])


# ── Gamification : XP, badges, streaks ────────────────────────────────────────

def get_xp(user_id: int) -> int:
    row = _fetchone("SELECT xp FROM users WHERE user_id=?", [user_id])
    return int(row["xp"] or 0) if row else 0


def add_xp(user_id: int, amount: int) -> dict:
    """Ajoute de l'XP. Renvoie {xp_before, xp_after, leveled_up: bool}."""
    if amount <= 0 or not user_id:
        return {"xp_before": 0, "xp_after": 0, "leveled_up": False}
    from core.gamification import calc_level
    before = get_xp(user_id)
    _execute("UPDATE users SET xp = COALESCE(xp, 0) + ? WHERE user_id=?", [amount, user_id])
    after = before + amount
    leveled = calc_level(before)["level"] != calc_level(after)["level"]
    return {"xp_before": before, "xp_after": after, "leveled_up": leveled}


def unlock_badge(user_id: int, badge_key: str) -> bool:
    """Renvoie True si nouvellement débloqué, False si déjà présent."""
    if not user_id or not badge_key:
        return False
    existing = _fetchone(
        "SELECT 1 FROM user_badges WHERE user_id=? AND badge_key=?",
        [user_id, badge_key]
    )
    if existing:
        return False
    _execute(
        "INSERT INTO user_badges (user_id, badge_key) VALUES (?, ?)",
        [user_id, badge_key]
    )
    return True


def get_user_badges(user_id: int) -> list[str]:
    """Renvoie la liste des badge_key débloqués pour un user."""
    rows = _fetchall(
        "SELECT badge_key FROM user_badges WHERE user_id=? ORDER BY unlocked_at DESC",
        [user_id]
    )
    return [r["badge_key"] for r in rows]


def get_streak(user_id: int) -> dict:
    row = _fetchone(
        "SELECT current_streak, longest_streak, last_drink_date FROM users WHERE user_id=?",
        [user_id]
    )
    if not row:
        return {"current": 0, "longest": 0, "last": None}
    return {
        "current": int(row.get("current_streak") or 0),
        "longest": int(row.get("longest_streak") or 0),
        "last":    row.get("last_drink_date"),
    }


def bump_streak(user_id: int) -> dict:
    """Met à jour le streak d'un user en fonction de l'ajout d'un verre.
    Si dernier verre = aujourd'hui → ne change rien.
    Si dernier verre = hier → +1.
    Sinon → reset à 1.
    Renvoie le nouveau state."""
    from datetime import date as _date
    s = get_streak(user_id)
    today = _date.today().isoformat()
    new_current = s["current"]
    if s["last"] == today:
        # Déjà compté aujourd'hui : pas de changement
        return s
    if s["last"]:
        try:
            last = _date.fromisoformat(s["last"])
            delta = (_date.today() - last).days
            if delta == 1:
                new_current = s["current"] + 1
            else:
                new_current = 1  # reset
        except Exception:
            new_current = 1
    else:
        new_current = 1
    new_longest = max(s["longest"], new_current)
    _execute(
        "UPDATE users SET current_streak=?, longest_streak=?, last_drink_date=? WHERE user_id=?",
        [new_current, new_longest, today, user_id]
    )
    return {"current": new_current, "longest": new_longest, "last": today}


def set_referrer(user_id: int, referrer_id: int):
    """Stocke le parrain d'un nouveau user (uniquement si pas déjà défini)."""
    _execute(
        "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
        [referrer_id, user_id]
    )


def count_referrals(user_id: int) -> int:
    row = _fetchone(
        "SELECT COUNT(*) as c FROM users WHERE referred_by=?",
        [user_id]
    )
    return int(row["c"] or 0) if row else 0


# ── Auth sessions (token bearer) ──────────────────────────────────────────────

import secrets as _secrets
from datetime import timedelta as _timedelta

AUTH_TOKEN_TTL_DAYS = 90


def create_auth_session(user_id: int) -> str:
    """Génère un token bearer aléatoire et l'enregistre. Retourne le token."""
    token = _secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + _timedelta(days=AUTH_TOKEN_TTL_DAYS)
    _execute(
        "INSERT INTO auth_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        [token, user_id, now.isoformat(), expires.isoformat()]
    )
    return token


def get_uid_from_token(token: str) -> int | None:
    """Renvoie le user_id associé à un token bearer, ou None si invalide/expiré."""
    if not token or len(token) > 256:
        return None
    row = _fetchone(
        "SELECT user_id, expires_at FROM auth_sessions WHERE token=?",
        [token]
    )
    if not row:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            _execute("DELETE FROM auth_sessions WHERE token=?", [token])
            return None
    except Exception:
        return None
    return int(row["user_id"])


def revoke_auth_token(token: str):
    _execute("DELETE FROM auth_sessions WHERE token=?", [token])


def revoke_all_user_sessions(user_id: int):
    """Révoque tous les tokens d'un utilisateur (logout total / suppression)."""
    _execute("DELETE FROM auth_sessions WHERE user_id=?", [user_id])


# ── Abonnement Premium ────────────────────────────────────────────────────────

def set_premium(user_id: int, customer_id: str, subscription_id: str, premium_until: str):
    """Active l'abonnement premium d'un utilisateur."""
    _execute(
        """UPDATE users SET is_premium=1, stripe_customer_id=?,
                            stripe_subscription_id=?, premium_until=?
           WHERE user_id=?""",
        [customer_id, subscription_id, premium_until, user_id]
    )


def clear_premium(user_id: int):
    """Désactive l'abonnement premium (garde stripe_customer_id pour réutiliser)."""
    _execute(
        "UPDATE users SET is_premium=0, stripe_subscription_id=NULL, premium_until=NULL WHERE user_id=?",
        [user_id]
    )


def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    return _fetchone("SELECT * FROM users WHERE stripe_customer_id=?", [customer_id])


def set_stripe_customer_id(user_id: int, customer_id: str):
    _execute("UPDATE users SET stripe_customer_id=? WHERE user_id=?", [customer_id, user_id])
