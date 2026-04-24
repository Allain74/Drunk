import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from data.database import init_db, get_all_users, get_all_active_drinks
from core.widmark import total_bac, bac_label, sober_in_hours

load_dotenv()

_ws_clients: set[WebSocket] = set()
_bot_app = None
_danger_notified: dict[int, datetime] = {}

RENDER_URL = os.environ.get("RENDER_URL", "https://drunk-l34t.onrender.com")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app
    init_db()

    from bot.bot import create_application
    _bot_app = create_application()
    await _bot_app.initialize()
    await _bot_app.start()

    # Webhook : Telegram envoie les messages à notre URL
    await _bot_app.bot.set_webhook(
        url=f"{RENDER_URL}/telegram-webhook",
        drop_pending_updates=True,
    )

    from telegram import BotCommand
    await _bot_app.bot.set_my_commands([
        BotCommand("p",          "Configurer ton profil  →  /p 80 h"),
        BotCommand("tac",        "Voir ton taux d'alcool actuel"),
        BotCommand("h",          "Historique des verres de la session"),
        BotCommand("stop",       "Remettre les compteurs à zéro"),
        BotCommand("liste",      "Voir toutes les boissons disponibles"),
        BotCommand("demi",       "🍺 Demi 25cl (5%)"),
        BotCommand("pinte",      "🍺 Pinte 50cl (5%)"),
        BotCommand("demif",      "🍺 Demi forte 25cl (8.5%)"),
        BotCommand("pintef",     "🍺 Pinte forte 50cl (8.5%)"),
        BotCommand("vin",        "🍷 Verre vin rouge 12cl"),
        BotCommand("blanc",      "🥂 Verre vin blanc 12cl"),
        BotCommand("champagne",  "🥂 Coupe champagne 10cl"),
        BotCommand("vodka",      "🥃 Shot vodka 4cl"),
        BotCommand("whisky",     "🥃 Shot whisky 4cl"),
        BotCommand("tequila",    "🥃 Shot tequila 4cl"),
        BotCommand("rhum",       "🥃 Shot rhum 4cl"),
        BotCommand("shot96",     "💥 Shot alcool 96° 4cl"),
        BotCommand("mojito",     "🍹 Mojito"),
        BotCommand("gin",        "🍹 Gin tonic"),
        BotCommand("aperol",     "🍹 Aperol spritz"),
        BotCommand("longisland", "🍹 Long Island"),
        BotCommand("pastis",     "🌿 Pastis 2.5cl"),
        BotCommand("cidre",      "🍎 Cidre 25cl"),
        BotCommand("sangria",    "🍷 Sangria 20cl"),
    ])

    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_danger_loop())

    yield

    await _bot_app.bot.delete_webhook(drop_pending_updates=True)
    await _bot_app.stop()
    await _bot_app.shutdown()



app = FastAPI(title="AlcooTracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Webhook Telegram ──────────────────────────────────────────────────────────

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, _bot_app.bot)
    await _bot_app.process_update(update)
    return {"ok": True}


# ── Helpers dashboard ─────────────────────────────────────────────────────────

def build_snapshot() -> list[dict]:
    users = {u["telegram_id"]: u for u in get_all_users()}
    drinks_by_user = get_all_active_drinks()
    now = datetime.now(timezone.utc)
    result = []
    for uid, user in users.items():
        drinks = drinks_by_user.get(uid, [])
        bac = total_bac(drinks, user["weight_kg"], user["gender"], now)
        result.append({
            "username":    user["username"],
            "bac":         round(bac, 3),
            "label":       bac_label(bac),
            "sober_in_h":  round(sober_in_hours(bac), 1),
            "nb_drinks":   len(drinks),
            "has_session": uid in drinks_by_user,
            "lat":         user["latitude"],
            "lon":         user["longitude"],
        })
    result.sort(key=lambda x: x["bac"], reverse=True)
    return result


async def _broadcast(data: list[dict]):
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


async def _broadcast_loop():
    while True:
        await asyncio.sleep(60)
        await _broadcast(build_snapshot())


async def _danger_loop():
    while True:
        await asyncio.sleep(300)
        now = datetime.now(timezone.utc)
        users = {u["telegram_id"]: u for u in get_all_users()}
        drinks_by_user = get_all_active_drinks()
        for uid, user in users.items():
            drinks = drinks_by_user.get(uid, [])
            if not drinks:
                _danger_notified.pop(uid, None)
                continue
            bac = total_bac(drinks, user["weight_kg"], user["gender"], now)
            if bac <= 1.5:
                _danger_notified.pop(uid, None)
                continue
            last_drink_t = max(d[1] for d in drinks)
            if (now - last_drink_t).total_seconds() < 1800:
                continue
            last_notif = _danger_notified.get(uid)
            if last_notif and (now - last_notif).total_seconds() < 3600:
                continue
            _danger_notified[uid] = now
            try:
                await _bot_app.bot.send_message(
                    chat_id=uid,
                    text=f"👀 *{user['username']}*, t'es encore vivant ? {bac:.2f} g/L depuis un moment...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/snapshot")
def get_snapshot():
    return build_snapshot()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps(build_snapshot()))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


@app.post("/refresh")
async def trigger_refresh():
    snapshot = build_snapshot()
    await _broadcast(snapshot)
    return {"ok": True}


@app.get("/history")
def get_history():
    """Retourne l'historique des boissons par user pour le graphique."""
    from data.database import get_all_users, get_active_session, get_conn
    users = get_all_users()
    result = []
    for user in users:
        session = get_active_session(user["telegram_id"])
        if not session:
            continue
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE session_id=? ORDER BY logged_at",
                (session["id"],)
            ).fetchall()
        points = []
        for r in rows:
            t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc)
            points.append({"t": t.isoformat(), "alc_g": r["alc_grams"], "drink_key": r["drink_key"]})
        result.append({
            "username": user["username"],
            "weight_kg": user["weight_kg"],
            "gender": user["gender"],
            "points": points,
        })
    return result
