import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update
from telegram.error import Conflict

from data.database import init_db, get_all_users, get_all_active_drinks
from core.widmark import total_bac, bac_label, sober_in_hours

load_dotenv()

_ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Démarre le bot Telegram dans le même event loop
    from bot.bot import create_application

    async def ignore_conflict(update, context):
        if isinstance(context.error, Conflict):
            return
        raise context.error

    bot_app = create_application()
    bot_app.add_error_handler(ignore_conflict)
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    asyncio.create_task(_broadcast_loop())

    yield

    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()


app = FastAPI(title="AlcooTracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_snapshot() -> list[dict]:
    users = {u["telegram_id"]: u for u in get_all_users()}
    drinks_by_user = get_all_active_drinks()
    now = datetime.now(timezone.utc)
    result = []
    for uid, user in users.items():
        drinks = drinks_by_user.get(uid, [])
        bac = total_bac(drinks, user["weight_kg"], user["gender"], now)
        result.append({
            "username":   user["username"],
            "bac":        round(bac, 3),
            "label":      bac_label(bac),
            "sober_in_h": round(sober_in_hours(bac), 1),
            "nb_drinks":  len(drinks),
            "has_session": uid in drinks_by_user,
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
        await asyncio.sleep(30)
        await _broadcast(build_snapshot())


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
