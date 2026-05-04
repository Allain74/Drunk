import os
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


def init_db():
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
    ])


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


def get_all_users() -> list[dict]:
    return _fetchall("SELECT * FROM users")


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
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
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
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    rows = _fetchall("""
        SELECT dl.telegram_id, dl.alc_grams, dl.logged_at
        FROM drink_logs dl JOIN sessions s ON dl.session_id=s.id
        WHERE s.active=1 ORDER BY dl.logged_at
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
        SELECT u.username, COUNT(DISTINCT DATE(dl.logged_at)) as nb_days
        FROM drink_logs dl
        JOIN users u ON dl.telegram_id = u.telegram_id
        GROUP BY u.telegram_id
    """)
    nb_days_map = {r["username"]: r["nb_days"] for r in days_rows}

    users: dict[str, dict] = {}
    for r in rows:
        name = r["username"]
        if name not in users:
            users[name] = {"username": name, "total_drinks": 0, "total_alc_g": 0.0, "breakdown": []}
        users[name]["total_drinks"] += r["count"]
        users[name]["total_alc_g"] = round(users[name]["total_alc_g"] + r["total_alc"], 1)
        users[name]["breakdown"].append({"drink_key": r["drink_key"], "count": r["count"]})

    for u in users.values():
        nb_days = nb_days_map.get(u["username"], 1)
        u["nb_days"] = nb_days
        u["avg_doses_per_day"] = round((u["total_alc_g"] / 10) / nb_days, 1)

    return sorted(users.values(), key=lambda x: x["total_alc_g"], reverse=True)


def get_drinks_by_session(session_id: int) -> list[dict]:
    return _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
        [session_id]
    )
