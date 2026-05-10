import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton

from data.database import (
    init_db, get_all_users, get_all_active_drinks, get_active_session,
    get_drinks_by_session, get_all_time_stats, get_last_drink_time,
    set_last_inactivity_notif, get_last_inactivity_notif,
    get_active_bets, settle_bet, get_bet, get_user, add_coins, get_coins,
    create_bet, get_pending_bet_for, accept_bet, cancel_bet,
    get_user_bets, get_all_balances, get_transactions,
    get_blackjack_session, get_blackjack_session_by_player,
    get_active_blackjack_sessions, get_blackjack_players, update_blackjack_session,
    update_blackjack_player, create_blackjack_session, add_blackjack_player,
    follow_user, unfollow_user, is_following, get_following, get_followers,
    verify_password,
    log_drink as db_log_drink, start_session, end_session,
    upsert_user, set_password, get_user_by_username, rename_user,
    delete_last_drink, update_max_bac, is_username_taken,
    get_session_drinks, delete_user, update_location,
    init_push_subscriptions, save_push_subscription,
    delete_push_subscription, get_push_subscriptions,
    get_blackjack_stats, get_profile_follows,
    set_avatar, get_all_avatars,
)
from core.recap import build_weekly_recap
from core.widmark import total_bac, bac_label, sober_in_hours, alcohol_grams
from core.blackjack import new_deck, hand_value, display_hand, is_blackjack
from core.drinks import DRINKS

load_dotenv()

# ── Web Push (VAPID) ──────────────────────────────────────────────────────────
try:
    from pywebpush import webpush, WebPushException
    _PUSH_ENABLED = True
except ImportError:
    _PUSH_ENABLED = False

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": "mailto:admin@drunk.app"}


def _send_push(telegram_id: int, title: str, body: str, url: str = "/"):
    """Envoie une notification push à tous les appareils d'un utilisateur."""
    if not _PUSH_ENABLED or not VAPID_PRIVATE_KEY:
        print(f"[PUSH] désactivé — PUSH_ENABLED={_PUSH_ENABLED} KEY={'oui' if VAPID_PRIVATE_KEY else 'non'}")
        return
    subs = get_push_subscriptions(telegram_id)
    print(f"[PUSH] envoi à {telegram_id} — {len(subs)} subscription(s)")
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
                ttl=86400,                          # garde 24h si appareil hors ligne
                headers={"urgency": "high"},        # réveille iOS même app fermée
            )
            print(f"[PUSH] ✅ envoyé à {sub['endpoint'][:60]}…")
        except Exception as e:
            print(f"[PUSH] ❌ erreur : {e}")
            # Subscription expirée → on la supprime
            try:
                resp = getattr(e, "response", None)
                if resp and resp.status_code in (404, 410):
                    delete_push_subscription(sub["endpoint"])
            except Exception:
                pass


async def _notify_followers(actor_id: int, title: str, body: str, url: str = "/"):
    """Notifie en tâche de fond tous les abonnés d'un utilisateur."""
    import asyncio
    loop = asyncio.get_event_loop()
    followers = get_followers(actor_id)
    for fid in followers:
        loop.run_in_executor(None, _send_push, fid, title, body, url)

_ws_clients: set[WebSocket] = set()
_bj_clients: dict[str, set[WebSocket]] = {}
_bot_app = None
_danger_notified: dict[int, datetime] = {}
_last_weekly_recap_date: str = ""  # "YYYY-MM-DD" du dernier lundi envoyé
PARIS = ZoneInfo("Europe/Paris")

RENDER_URL = os.environ.get("RENDER_URL", "https://drunk-l34t.onrender.com")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app
    init_db()
    init_push_subscriptions()

    from bot.bot import create_application
    _bot_app = create_application()
    await _bot_app.initialize()
    await _bot_app.start()

    # Webhook : Telegram envoie les messages à notre URL
    await _bot_app.bot.set_webhook(
        url=f"{RENDER_URL}/telegram-webhook",
        drop_pending_updates=True,
    )

    from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

    user_commands = [
        BotCommand("topo",       "ℹ️ Comment utiliser le bot"),
        BotCommand("profil",     "🧾 Configurer ton profil (sexe, poids, pseudo, mdp)"),
        BotCommand("password",   "🔑 Changer ton mot de passe site web"),
        BotCommand("tac",        "Voir ton taux d'alcool actuel"),
        BotCommand("h",          "Historique des verres de la session"),
        BotCommand("annuler",    "↩️ Annuler le dernier verre"),
        BotCommand("stop",       "Remettre les compteurs à zéro"),
        BotCommand("defi",       "🏆 Classement de la soirée"),
        BotCommand("ou",         "📍 Position de quelqu'un  →  /ou Prénom"),
        BotCommand("invite",     "🎉 Inviter tout le monde à boire avec toi"),
        BotCommand("site",       "🌐 Lien du dashboard"),
        BotCommand("liste",      "Voir toutes les boissons disponibles"),
        BotCommand("demi",       "🍺 Demi 25cl (5%)"),
        BotCommand("pinte",      "🍺 Pinte 50cl (5%)"),
        BotCommand("demif",      "🍺 Demi forte 25cl (8.5%)"),
        BotCommand("pintef",     "🍺 Pinte forte 50cl (8.5%)"),
        BotCommand("vin",        "🍷 Verre de vin 12cl (rouge/blanc)"),
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
        BotCommand("ricard",     "🌿 Ricard / Pastis 2.5cl"),
        BotCommand("perroquet",  "🦜 Perroquet Sauvage (pastis + Get 27)"),
        BotCommand("get27",      "🍃 Get 27 4cl"),
        BotCommand("cidre",      "🍎 Cidre 25cl"),
        BotCommand("sangria",    "🍷 Sangria 20cl"),
        BotCommand("bucket",     "🪣 Bucket thaïlandais (125ml, 40°)"),
        BotCommand("solde",      "🪙 Voir ton solde de pièces"),
        BotCommand("offrir",     "🎁 Offrir des coins  →  /offrir Prénom 50"),
        BotCommand("pari",       "🎰 Lancer un pari"),
        BotCommand("accepter",   "✅ Accepter un pari"),
        BotCommand("refuser",    "❌ Refuser un pari"),
        BotCommand("blackjack",  "🃏 Jouer au blackjack"),
        BotCommand("rejoindrebj","🃏 Rejoindre une partie  →  /rejoindrebj token mise"),
        BotCommand("lancerbj",   "🚀 Lancer une partie multi"),
        BotCommand("rename",     "✏️ Changer ton pseudo  →  /rename NouveauNom"),
    ]

    admin_commands = user_commands + [
        BotCommand("notif",      "📢 Envoyer un message à tous"),
        BotCommand("notifmaj",   "🔔 Notifier une mise à jour"),
        BotCommand("add",        "➕ Ajouter un verre  →  /add Prénom boisson"),
        BotCommand("del",        "➖ Supprimer un verre  →  /del Prénom"),
        BotCommand("ban",        "🚫 Bannir un utilisateur  →  /ban Prénom"),
        BotCommand("unban",      "✅ Débannir  →  /unban Prénom"),
        BotCommand("recap",      "📊 Recap de la semaine  →  /recap [send]"),
    ]

    await _bot_app.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if admin_id:
        try:
            await _bot_app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass

    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_danger_loop())
    asyncio.create_task(_weekly_recap_loop())
    asyncio.create_task(_bet_settlement_loop())

    yield

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
        # Peak BAC during current session (last 24h)
        peak_24h = 0.0
        for i in range(len(drinks)):
            b = total_bac(drinks[:i+1], user["weight_kg"], user["gender"], drinks[i][1])
            if b > peak_24h:
                peak_24h = b
        result.append({
            "username":    user["username"],
            "bac":         round(bac, 3),
            "label":       bac_label(bac),
            "sober_in_h":  round(sober_in_hours(bac), 1),
            "nb_drinks":   len(drinks),
            "has_session": uid in drinks_by_user,
            "lat":         user["latitude"],
            "lon":         user["longitude"],
            "max_bac":     round(user.get("max_bac") or 0, 2),
            "peak_24h":    round(peak_24h, 2),
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
    _ws_clients.difference_update(dead)


async def _broadcast_loop():
    while True:
        await asyncio.sleep(300)
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

        # Notif inactivité — une fois par semaine d'absence
        _INACTIVITY_MSGS = [
            "😤 *{name}*, t'es devenu gay pour pas picoler depuis une semaine ? Allez, bois un verre ! 🍺",
            "😶 *{name}*, deux semaines sans boire… t'as rejoint les alcooliques anonymes ou quoi ? 🤨",
            "💀 *{name}*, trois semaines. T'es sobre. C'est honteux. Tes potes ont honte de toi. 🫵",
            "🚨 *{name}*, un mois sans picoler. Appelle le 15, c'est une urgence médicale. 🏥",
        ]
        for user in get_all_users():
            uid = user["telegram_id"]
            last_t = get_last_drink_time(uid)
            if last_t is None:
                continue
            days_inactive = (now - last_t).total_seconds() / 86400
            if days_inactive >= 7:
                last_notif = get_last_inactivity_notif(uid)
                if last_notif is None or (now - last_notif).total_seconds() >= 7 * 86400:
                    set_last_inactivity_notif(uid, now)
                    weeks = int(days_inactive // 7)
                    msg_template = _INACTIVITY_MSGS[min(weeks - 1, len(_INACTIVITY_MSGS) - 1)]
                    try:
                        await _bot_app.bot.send_message(
                            chat_id=uid,
                            text=msg_template.format(name=user["username"]),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass


async def _weekly_recap_loop():
    global _last_weekly_recap_date
    while True:
        await asyncio.sleep(60)
        now_paris = datetime.now(PARIS)
        # Lundi à 9h00
        if now_paris.weekday() == 0 and now_paris.hour == 9:
            today_str = now_paris.strftime("%Y-%m-%d")
            if _last_weekly_recap_date != today_str:
                _last_weekly_recap_date = today_str
                until = datetime.now(timezone.utc)
                since = until - timedelta(days=7)
                msg = build_weekly_recap(since, until)
                for user in get_all_users():
                    try:
                        await _bot_app.bot.send_message(
                            chat_id=user["telegram_id"],
                            text=msg,
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return {"ok": True}


@app.post("/admin/reset-bj-stats")
async def admin_reset_bj_stats(request: Request):
    """Endpoint admin : remet à zéro toutes les stats et sessions blackjack."""
    body = await request.json()
    secret = body.get("secret", "")
    if secret != os.environ.get("ADMIN_SECRET", ""):
        return {"ok": False, "error": "Non autorisé"}
    from data.database import _pipeline
    _pipeline([
        ("DELETE FROM blackjack_players", []),
        ("DELETE FROM blackjack_sessions", []),
    ])
    return {"ok": True, "message": "Stats BJ réinitialisées"}


@app.post("/admin/set-coins")
async def admin_set_coins(request: Request):
    """Endpoint admin : définit le solde exact d'un utilisateur."""
    body = await request.json()
    secret = body.get("secret", "")
    if secret != os.environ.get("ADMIN_SECRET", ""):
        return {"ok": False, "error": "Non autorisé"}
    username = body.get("username", "").strip()
    amount   = body.get("amount")
    if not username or amount is None:
        return {"ok": False, "error": "username et amount requis"}
    user = get_user_by_username(username)
    if not user:
        return {"ok": False, "error": f"Utilisateur '{username}' introuvable"}
    current = get_coins(user["telegram_id"])
    delta = int(amount) - current
    if delta != 0:
        add_coins(user["telegram_id"], delta, f"Admin set-coins → {amount}")
    return {"ok": True, "username": user["username"], "before": current, "after": int(amount)}


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


@app.get("/alltime")
def get_alltime():
    return get_all_time_stats()


@app.get("/lookup")
def lookup_user(name: str):
    from data.database import get_user_by_username
    user = get_user_by_username(name)
    if not user:
        return {"found": False}
    return {"found": True, "telegram_id": user["telegram_id"], "username": user["username"]}


async def _bet_settlement_loop():
    while True:
        await asyncio.sleep(60)
        now_paris = datetime.now(PARIS)
        current_time = now_paris.strftime("%H:%M")

        for bet in get_active_bets():
            if not bet.get("end_time"):
                continue
            if current_time < bet["end_time"]:
                continue

            from data.database import get_session_drinks
            uid1, uid2 = bet["challenger_id"], bet["opponent_id"]
            users = {u["telegram_id"]: u for u in get_all_users()}

            if bet["bet_type"] == "verres":
                drinks1 = get_session_drinks(uid1)
                drinks2 = get_session_drinks(uid2)
                count1, count2 = len(drinks1), len(drinks2)
                winner_id = uid1 if count1 >= count2 else uid2
                loser_id = uid2 if winner_id == uid1 else uid1
                detail = f"({count1} vs {count2} verres)"

            elif bet["bet_type"] == "ivre":
                from core.widmark import total_bac as _total_bac
                from data.database import get_session_drinks
                u1 = users.get(uid1, {})
                u2 = users.get(uid2, {})
                drinks1 = get_session_drinks(uid1)
                drinks2 = get_session_drinks(uid2)
                now_utc = datetime.now(timezone.utc)
                bac1 = _total_bac(drinks1, u1.get("weight_kg", 70), u1.get("gender", "homme"), now_utc)
                bac2 = _total_bac(drinks2, u2.get("weight_kg", 70), u2.get("gender", "homme"), now_utc)
                winner_id = uid1 if bac1 >= bac2 else uid2
                loser_id = uid2 if winner_id == uid1 else uid1
                detail = f"({bac1:.2f} vs {bac2:.2f} g/L)"

            else:
                continue

            settle_bet(bet["id"], winner_id)
            winner = get_user(winner_id)
            loser = get_user(loser_id)
            amount = bet["amount"]
            add_coins(winner_id, amount, f"Pari gagné contre {loser['username']}")
            add_coins(loser_id, -amount, f"Pari perdu contre {winner['username']}")

            msg = (
                f"🏁 *Résultat du pari !* {detail}\n\n"
                f"🏆 Gagnant : *{winner['username']}* +{amount} 🪙\n"
                f"💸 Perdant : *{loser['username']}* -{amount} 🪙"
            )

            for uid in [uid1, uid2]:
                try:
                    await _bot_app.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
                except Exception:
                    pass


@app.get("/history")
def get_history():
    users = get_all_users()
    result = []
    for user in users:
        session = get_active_session(user["telegram_id"])
        if not session:
            continue
        rows = get_drinks_by_session(session["id"])
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


@app.get("/coins")
def get_coins_endpoint():
    balances = get_all_balances()
    result = []
    for b in balances:
        txs = get_transactions(b["telegram_id"], 10)
        result.append({
            "username": b["username"],
            "coins": b["coins"] or 0,
            "transactions": [
                {"amount": t["amount"], "reason": t["reason"], "at": t["created_at"]}
                for t in txs
            ],
        })
    return result


@app.get("/users")
def get_all_users_endpoint():
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    users = get_all_users()
    return [
        {
            "telegram_id": u["telegram_id"],
            "username": u["username"],
            "is_admin": u["telegram_id"] == admin_id,
        }
        for u in users
    ]

@app.get("/following/{telegram_id}")
def get_following_endpoint(telegram_id: int):
    return {"following": get_following(telegram_id)}

@app.post("/follow")
async def follow_endpoint(request: Request):
    body = await request.json()
    follower_id  = body.get("follower_id")
    following_id = body.get("following_id")
    if not follower_id or not following_id:
        return {"ok": False, "error": "Missing IDs"}
    follow_user(follower_id, following_id)
    return {"ok": True}

@app.post("/unfollow")
async def unfollow_endpoint(request: Request):
    body = await request.json()
    follower_id  = body.get("follower_id")
    following_id = body.get("following_id")
    if not follower_id or not following_id:
        return {"ok": False, "error": "Missing IDs"}
    unfollow_user(follower_id, following_id)
    return {"ok": True}


@app.post("/login")
async def login_endpoint(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()  # peut être vide pour anciens comptes
    if not username:
        return {"ok": False, "error": "Pseudo requis"}
    user = verify_password(username, password)
    if not user:
        return {"ok": False, "error": "Pseudo ou mot de passe incorrect"}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    return {
        "ok": True,
        "telegram_id": user["telegram_id"],
        "username": user["username"],
        "is_admin": user["telegram_id"] == admin_id,
    }


def _ensure_session(telegram_id: int):
    if not get_active_session(telegram_id):
        start_session(telegram_id)


# ── Inscription web ───────────────────────────────────────────────────────────

@app.post("/register")
async def register_endpoint(request: Request):
    import random as _rand
    body     = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    gender   = body.get("gender", "").strip()
    weight   = body.get("weight")

    if not all([username, password, gender, weight]):
        return {"ok": False, "error": "Tous les champs sont requis"}
    if len(username) < 2 or len(username) > 30:
        return {"ok": False, "error": "Pseudo invalide (2–30 caractères)"}
    if len(password) < 4:
        return {"ok": False, "error": "Mot de passe trop court (min. 4 caractères)"}
    if gender not in ("homme", "femme"):
        return {"ok": False, "error": "Genre invalide"}
    try:
        weight = float(weight)
        assert 30 < weight < 250
    except Exception:
        return {"ok": False, "error": "Poids invalide (30–250 kg)"}

    if get_user_by_username(username):
        return {"ok": False, "error": "Ce pseudo est déjà utilisé"}

    # ID web : grand entier pour éviter tout conflit avec les Telegram IDs
    web_id = _rand.randint(10**12, 9 * 10**12)

    upsert_user(web_id, username, weight, gender)
    set_password(web_id, password)
    _ensure_session(web_id)

    # Nouvel utilisateur → suit automatiquement l'admin
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if admin_id and admin_id != web_id:
        follow_user(web_id, admin_id)

    return {
        "ok": True,
        "telegram_id": web_id,
        "username": username,
        "is_admin": web_id == admin_id,
    }


# ── Modification de profil ────────────────────────────────────────────────────

@app.post("/update-profile")
async def update_profile_endpoint(request: Request):
    body          = await request.json()
    telegram_id   = body.get("telegram_id")
    current_pwd   = body.get("current_password", "").strip()
    new_gender    = body.get("gender")
    new_weight    = body.get("weight")
    new_password  = body.get("new_password", "").strip()
    new_username  = body.get("new_username", "").strip()

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}

    if not verify_password(user["username"], current_pwd):
        return {"ok": False, "error": "Mot de passe incorrect"}

    response_username = None

    # Changement de pseudo
    if new_username and new_username != user["username"]:
        if len(new_username) < 2 or len(new_username) > 30:
            return {"ok": False, "error": "Pseudo invalide (2–30 caractères)"}
        if is_username_taken(new_username, telegram_id):
            return {"ok": False, "error": "Ce pseudo est déjà pris"}
        rename_user(telegram_id, new_username)
        response_username = new_username

    # Mise à jour genre / poids
    current_username = response_username or user["username"]
    if new_gender or new_weight:
        gender = new_gender if new_gender in ("homme", "femme") else user["gender"]
        try:
            w = float(new_weight) if new_weight else user["weight_kg"]
            assert 30 < w < 250
        except Exception:
            return {"ok": False, "error": "Poids invalide (30–250 kg)"}
        upsert_user(telegram_id, current_username, w, gender)

    # Nouveau mot de passe
    if new_password:
        if len(new_password) < 4:
            return {"ok": False, "error": "Nouveau mot de passe trop court (min. 4 caractères)"}
        set_password(telegram_id, new_password)

    await _broadcast(build_snapshot())
    return {"ok": True, **({"new_username": response_username} if response_username else {})}


# ── Suppression de compte ─────────────────────────────────────────────────────

@app.post("/delete-account")
async def delete_account_endpoint(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")
    password    = body.get("password", "").strip()

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}

    if not verify_password(user["username"], password):
        return {"ok": False, "error": "Mot de passe incorrect"}

    delete_user(telegram_id)
    await _broadcast(build_snapshot())
    return {"ok": True}


# ── Push notifications endpoints ──────────────────────────────────────────────

@app.get("/push/vapid-public-key")
def push_vapid_key():
    return {"key": VAPID_PUBLIC_KEY}

@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")
    # Le frontend envoie endpoint/p256dh/auth à plat
    endpoint = body.get("endpoint")
    p256dh   = body.get("p256dh")
    auth     = body.get("auth")
    if not all([telegram_id, endpoint, p256dh, auth]):
        print(f"[PUSH/subscribe] données incomplètes : {list(body.keys())}")
        return {"ok": False, "error": "Données incomplètes"}
    save_push_subscription(telegram_id, endpoint, p256dh, auth)
    print(f"[PUSH/subscribe] ✅ tid={telegram_id} endpoint={endpoint[:60]}…")
    return {"ok": True}

@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    body     = await request.json()
    endpoint = body.get("endpoint")
    if endpoint:
        delete_push_subscription(endpoint)
    return {"ok": True}

@app.post("/push/test")
async def push_test(request: Request):
    """Envoie une notif de test à soi-même."""
    body        = await request.json()
    telegram_id = body.get("telegram_id")
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_push, telegram_id, "🍺 Test Drunk", "Les notifications fonctionnent !", "/")
    return {"ok": True}

# ── Logger un verre depuis le web ─────────────────────────────────────────────

@app.post("/log-drink")
async def log_drink_web(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")
    drink_key   = body.get("drink_key")
    lat         = body.get("lat")
    lon         = body.get("lon")

    if not telegram_id or not drink_key:
        return {"ok": False, "error": "Paramètres manquants"}

    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    if drink_key not in DRINKS:
        return {"ok": False, "error": "Boisson inconnue"}

    drink = DRINKS[drink_key]
    _ensure_session(telegram_id)
    db_log_drink(telegram_id, drink_key, alcohol_grams(drink.volume_ml, drink.abv))
    add_coins(telegram_id, 5, f"Verre bu ({drink.name})")

    # Mise à jour de la position si fournie
    if lat is not None and lon is not None:
        try:
            update_location(telegram_id, float(lat), float(lon))
        except Exception:
            pass

    drinks_data = get_session_drinks(telegram_id)
    bac = total_bac(drinks_data, user["weight_kg"], user["gender"])
    update_max_bac(telegram_id, bac)

    await _broadcast(build_snapshot())

    # Notifier les abonnés
    bac_label_str = bac_label(bac)
    notif_body = f"vient de boire {drink.name} · {bac:.2f} g/L ({bac_label_str})"
    await _notify_followers(telegram_id, f"🍺 {user['username']}", notif_body, "/?tab=live")

    return {
        "ok": True,
        "bac": round(bac, 3),
        "nb_drinks": len(drinks_data),
        "label": bac_label(bac),
    }


# ── Annuler le dernier verre ──────────────────────────────────────────────────

@app.post("/undo-drink")
async def undo_drink_web(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}

    if not delete_last_drink(telegram_id):
        return {"ok": False, "error": "Aucun verre à annuler"}

    drinks_data = get_session_drinks(telegram_id)
    bac = total_bac(drinks_data, user["weight_kg"], user["gender"])

    await _broadcast(build_snapshot())
    return {
        "ok": True,
        "bac": round(bac, 3),
        "nb_drinks": len(drinks_data),
        "label": bac_label(bac),
    }


# ── Remettre les compteurs à zéro ────────────────────────────────────────────

@app.post("/reset-session")
async def reset_session_web(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    if not get_user(telegram_id):
        return {"ok": False, "error": "Utilisateur introuvable"}

    end_session(telegram_id)
    start_session(telegram_id)

    await _broadcast(build_snapshot())
    return {"ok": True}


# ── Blackjack web endpoints ────────────────────────────────────────────────────

@app.get("/blackjack/{token}/state")
def get_bj_state(token: str):
    """Retourne l'état courant d'une session blackjack (fallback HTTP pour les clients WS)."""
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    players = get_blackjack_players(sess["id"])
    dealer_hand = json.loads(sess["dealer_hand"])
    hide_dealer = sess["status"] == "active"
    player_data = []
    for p in players:
        u = get_user(p["telegram_id"])
        player_data.append({
            "telegram_id": p["telegram_id"],
            "username": u["username"] if u else str(p["telegram_id"]),
            "hand": json.loads(p["hand"]),
            "status": p["status"],
            "result": p["result"],
            "bet": p["bet"],
        })
    return {
        "ok": True,
        "status": sess["status"],
        "creator_id": sess["creator_id"],
        "dealer_hand": ([dealer_hand[0], "?"] if dealer_hand else []) if hide_dealer else dealer_hand,
        "dealer_value": hand_value(dealer_hand) if not hide_dealer else None,
        "players": player_data,
    }


@app.get("/blackjack/sessions")
async def list_bj_sessions():
    """Liste toutes les sessions blackjack en attente ou actives."""
    sessions = get_active_blackjack_sessions()
    result = []
    for s in sessions:
        players = get_blackjack_players(s["id"])
        creator = get_user(s["creator_id"])
        result.append({
            "token":   s["token"],
            "status":  s["status"],
            "creator": creator["username"] if creator else "?",
            "creator_id": s["creator_id"],
            "players": [
                {
                    "telegram_id": p["telegram_id"],
                    "username": (get_user(p["telegram_id"]) or {}).get("username", "?"),
                    "bet": p["bet"],
                    "status": p["status"],
                }
                for p in players
            ],
        })
    return result


@app.post("/blackjack/create-web")
async def bj_create_web(request: Request):
    """Crée une nouvelle session blackjack depuis le web."""
    body = await request.json()
    telegram_id = body.get("telegram_id")
    bet = int(body.get("bet", 50))
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    if bet < 10:
        return {"ok": False, "error": "Mise minimum : 10 🪙"}
    coins = get_coins(telegram_id)
    if coins < bet:
        return {"ok": False, "error": f"Solde insuffisant ({coins} 🪙)"}
    # Une session en attente à la fois par créateur
    existing = get_blackjack_session_by_player(telegram_id)
    if existing:
        return {"ok": True, "token": existing["token"]}
    token = secrets.token_urlsafe(8)
    session_id = create_blackjack_session(telegram_id, token)
    add_blackjack_player(session_id, telegram_id, bet)
    add_coins(telegram_id, -bet, "Blackjack - mise")
    return {"ok": True, "token": token}


@app.post("/blackjack/{token}/join-web")
async def bj_join_web(token: str, request: Request):
    """Rejoint une session blackjack depuis le web."""
    body = await request.json()
    telegram_id = body.get("telegram_id")
    bet = int(body.get("bet", 50))
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    if sess["status"] == "finished":
        return {"ok": False, "error": "Partie terminée"}
    players = get_blackjack_players(sess["id"])
    # Déjà dedans ?
    if any(p["telegram_id"] == telegram_id for p in players):
        return {"ok": True, "token": token}
    if sess["status"] == "active":
        return {"ok": False, "error": "Partie déjà en cours"}
    if len(players) >= 6:
        return {"ok": False, "error": "Table complète (6 joueurs max)"}
    if bet < 10:
        return {"ok": False, "error": "Mise minimum : 10 🪙"}
    coins = get_coins(telegram_id)
    if coins < bet:
        return {"ok": False, "error": f"Solde insuffisant ({coins} 🪙)"}
    add_blackjack_player(sess["id"], telegram_id, bet)
    add_coins(telegram_id, -bet, "Blackjack - mise")
    # Notifier via WS
    if token in _bj_clients:
        for client in list(_bj_clients[token]):
            try:
                await client.send_text(json.dumps({"event": "player_joined"}))
            except Exception:
                pass
    return {"ok": True, "token": token}


@app.post("/blackjack/{token}/leave")
async def bj_leave(token: str, request: Request):
    """Quitte une table blackjack. Rembourse la mise si la partie n'a pas commencé."""
    body = await request.json()
    telegram_id = body.get("telegram_id")
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": True}  # Session inexistante, rien à faire
    players = get_blackjack_players(sess["id"])
    me = next((p for p in players if p["telegram_id"] == telegram_id), None)
    if not me:
        return {"ok": True}  # Pas dans cette session

    # Cartes pas encore distribuées → remboursement possible
    if sess["status"] == "waiting":
        add_coins(telegram_id, me["bet"], "Blackjack - remboursement mise")
        from data.database import _execute
        _execute("DELETE FROM blackjack_players WHERE session_id=? AND telegram_id=?",
                 [sess["id"], telegram_id])
        remaining = [p for p in players if p["telegram_id"] != telegram_id]
        if not remaining or sess["creator_id"] == telegram_id:
            update_blackjack_session(sess["id"], status="finished")
        await _bj_broadcast(token)
        return {"ok": True, "refunded": me["bet"]}

    # Cartes visibles (active ou finished) → aucun remboursement
    return {"ok": True, "refunded": 0}


@app.post("/blackjack/{token}/start-web")
async def bj_start_web(token: str, request: Request):
    """Lance la partie (deal les cartes) depuis le web."""
    body = await request.json()
    caller_id = body.get("telegram_id")
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    if sess["creator_id"] != caller_id:
        return {"ok": False, "error": "Seul le créateur peut lancer"}
    if sess["status"] != "waiting":
        return {"ok": False, "error": "Partie déjà lancée"}
    players = get_blackjack_players(sess["id"])
    if len(players) < 1:
        return {"ok": False, "error": "Aucun joueur"}

    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]

    for p in players:
        hand = [deck.pop(), deck.pop()]
        update_blackjack_player(sess["id"], p["telegram_id"],
            hand=json.dumps(hand), status="playing")

    update_blackjack_session(sess["id"],
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand),
    )

    # Gérer les blackjacks immédiats
    players = get_blackjack_players(sess["id"])
    for p in players:
        hand = json.loads(p["hand"])
        if is_blackjack(hand):
            winnings = int(p["bet"] * 1.5)
            add_coins(p["telegram_id"], p["bet"] + winnings, "Blackjack naturel !")
            update_blackjack_player(sess["id"], p["telegram_id"],
                status="done", result="blackjack")

    # Si tous done → finished
    players = get_blackjack_players(sess["id"])
    if all(p["status"] in ("stand", "bust", "done") for p in players):
        update_blackjack_session(sess["id"], status="finished",
            dealer_hand=json.dumps(dealer_hand))

    # Notifier tous les clients WS connectés
    await _bj_broadcast(token)
    return {"ok": True}


# ── Paris web endpoints ────────────────────────────────────────────────────────

@app.get("/bets/user/{telegram_id}")
async def get_bets_user(telegram_id: int):
    """Retourne les paris d'un utilisateur."""
    bets = get_user_bets(telegram_id)
    result = []
    for b in bets:
        challenger = get_user(b["challenger_id"])
        opponent   = get_user(b["opponent_id"])
        winner     = get_user(b["winner_id"]) if b.get("winner_id") else None
        result.append({
            **b,
            "challenger_name": challenger["username"] if challenger else "?",
            "opponent_name":   opponent["username"]   if opponent   else "?",
            "winner_name":     winner["username"]     if winner     else None,
        })
    return result


@app.post("/bets/create")
async def create_bet_web(request: Request):
    """Crée un pari depuis le web."""
    body          = await request.json()
    challenger_id = body.get("challenger_id")
    opponent_name = body.get("opponent_name", "").strip()
    bet_type      = body.get("bet_type")      # verres | ivre | coinflip
    amount        = int(body.get("amount", 0))
    end_time      = body.get("end_time")      # "HH:MM" ou None

    if not challenger_id:
        return {"ok": False, "error": "Non connecté"}
    if bet_type not in ("verres", "ivre", "coinflip"):
        return {"ok": False, "error": "Type de pari invalide"}
    if amount < 10:
        return {"ok": False, "error": "Mise minimum : 10 🪙"}

    challenger = get_user(challenger_id)
    if not challenger:
        return {"ok": False, "error": "Utilisateur introuvable"}

    opponent = get_user_by_username(opponent_name)
    if not opponent:
        return {"ok": False, "error": f"Joueur « {opponent_name} » introuvable"}
    if opponent["telegram_id"] == challenger_id:
        return {"ok": False, "error": "Tu ne peux pas parier contre toi-même"}

    if get_coins(challenger_id) < amount:
        return {"ok": False, "error": f"Solde insuffisant ({get_coins(challenger_id)} 🪙)"}
    if get_coins(opponent["telegram_id"]) < amount:
        return {"ok": False, "error": f"{opponent['username']} n'a pas assez de 🪙"}

    bet_id = create_bet(challenger_id, opponent["telegram_id"], bet_type, amount, end_time or None)

    # Notif push à l'adversaire
    type_labels = {"verres": "plus de verres", "ivre": "TAC le plus haut", "coinflip": "pile ou face"}
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _send_push,
        opponent["telegram_id"],
        f"🎰 Pari de {challenger['username']}",
        f"{amount} 🪙 sur {type_labels[bet_type]} — accepte ou refuse !",
        "/?tab=menu"
    )

    return {"ok": True, "bet_id": bet_id}


@app.post("/bets/accept")
async def accept_bet_web(request: Request):
    body       = await request.json()
    telegram_id = body.get("telegram_id")
    bet_id     = body.get("bet_id")
    if not telegram_id or not bet_id:
        return {"ok": False, "error": "Données manquantes"}
    bet = get_bet(bet_id)
    if not bet:
        return {"ok": False, "error": "Pari introuvable"}
    if bet["opponent_id"] != telegram_id:
        return {"ok": False, "error": "Ce pari ne te concerne pas"}
    if bet["status"] != "pending":
        return {"ok": False, "error": "Pari déjà traité"}

    # Coinflip : résoudre immédiatement
    if bet["bet_type"] == "coinflip":
        import random
        winner_id = random.choice([bet["challenger_id"], bet["opponent_id"]])
        loser_id  = bet["opponent_id"] if winner_id == bet["challenger_id"] else bet["challenger_id"]
        accept_bet(bet_id)
        settle_bet(bet_id, winner_id)
        add_coins(winner_id,  bet["amount"],  f"Coinflip gagné")
        add_coins(loser_id,  -bet["amount"],  f"Coinflip perdu")
        winner = get_user(winner_id)
        return {"ok": True, "coinflip": True, "winner": winner["username"] if winner else "?"}

    accept_bet(bet_id)
    return {"ok": True, "coinflip": False}


@app.post("/bets/refuse")
async def refuse_bet_web(request: Request):
    body        = await request.json()
    telegram_id = body.get("telegram_id")
    bet_id      = body.get("bet_id")
    if not telegram_id or not bet_id:
        return {"ok": False, "error": "Données manquantes"}
    bet = get_bet(bet_id)
    if not bet:
        return {"ok": False, "error": "Pari introuvable"}
    if bet["opponent_id"] != telegram_id:
        return {"ok": False, "error": "Ce pari ne te concerne pas"}
    if bet["status"] != "pending":
        return {"ok": False, "error": "Pari déjà traité"}
    cancel_bet(bet_id)
    return {"ok": True}


@app.get("/profile/{telegram_id}")
def get_profile(telegram_id: int):
    """Retourne le profil public d'un utilisateur : abonnés/abonnements + stats BJ."""
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    follows = get_profile_follows(telegram_id)
    bj      = get_blackjack_stats(telegram_id)
    return {
        "ok": True,
        "telegram_id": telegram_id,
        "username": user["username"],
        **follows,
        "bj": bj,
    }


@app.get("/avatars")
def get_avatars():
    """Retourne tous les avatars : {telegram_id: data_url}."""
    rows = get_all_avatars()
    return {str(r["telegram_id"]): r["avatar"] for r in rows}


@app.post("/profile/avatar")
async def upload_avatar(request: Request):
    """Sauvegarde l'avatar (data URL base64) d'un utilisateur."""
    body = await request.json()
    tid    = body.get("telegram_id")
    avatar = body.get("avatar", "")
    if not tid or not avatar:
        return {"ok": False, "error": "Données manquantes"}
    user = get_user(int(tid))
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    # Limite à ~80 Ko en base64 (≈ 60 Ko image réelle)
    if len(avatar) > 100_000:
        return {"ok": False, "error": "Image trop grande (max 60 Ko)"}
    if not avatar.startswith("data:image/"):
        return {"ok": False, "error": "Format invalide"}
    set_avatar(int(tid), avatar)
    return {"ok": True}


@app.get("/blackjack/stats/all")
def get_all_bj_stats():
    """Retourne les stats blackjack de tous les utilisateurs."""
    users = get_all_users()
    result = []
    for u in users:
        bj = get_blackjack_stats(u["telegram_id"])
        result.append({
            "telegram_id": u["telegram_id"],
            "username":    u["username"],
            **bj,
        })
    return result


async def _bj_broadcast(token: str):
    """Diffuse l'état actuel d'une session blackjack à tous ses clients WS."""
    sess = get_blackjack_session(token)
    if not sess:
        return
    players = get_blackjack_players(sess["id"])
    dealer_hand = json.loads(sess["dealer_hand"])
    hide_dealer = sess["status"] == "active"

    player_data = []
    for p in players:
        u = get_user(p["telegram_id"])
        player_data.append({
            "telegram_id": p["telegram_id"],
            "username": u["username"] if u else str(p["telegram_id"]),
            "hand": json.loads(p["hand"]),
            "status": p["status"],
            "result": p["result"],
            "bet": p["bet"],
        })

    state = {
        "status": sess["status"],
        "creator_id": sess["creator_id"],
        "dealer_hand": (
            ([dealer_hand[0], "?"] if dealer_hand else []) if hide_dealer else dealer_hand
        ),
        "dealer_value": hand_value(dealer_hand) if not hide_dealer else None,
        "players": player_data,
    }

    dead = set()
    for client in list(_bj_clients.get(token, set())):
        try:
            await client.send_text(json.dumps(state))
        except Exception:
            dead.add(client)
    _bj_clients.get(token, set()).difference_update(dead)


@app.websocket("/ws/blackjack/{token}")
async def blackjack_ws(ws: WebSocket, token: str):
    session = get_blackjack_session(token)
    if not session:
        await ws.close()
        return
    await ws.accept()
    _bj_clients.setdefault(token, set()).add(ws)

    try:
        await _bj_broadcast(token)
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")
            player_id = msg.get("telegram_id")

            if action in ("hit", "stand") and player_id:
                sess = get_blackjack_session(token)
                players = get_blackjack_players(sess["id"])
                player = next((p for p in players if p["telegram_id"] == player_id), None)

                if player and player["status"] == "playing":
                    hand = json.loads(player["hand"])
                    deck = json.loads(sess["deck"])
                    dealer_hand = json.loads(sess["dealer_hand"])

                    if action == "hit":
                        card = deck.pop()
                        hand.append(card)
                        update_blackjack_session(sess["id"], deck=json.dumps(deck))
                        val = hand_value(hand)
                        if val > 21:
                            update_blackjack_player(sess["id"], player_id,
                                hand=json.dumps(hand), status="bust", result="lose")
                        else:
                            update_blackjack_player(sess["id"], player_id, hand=json.dumps(hand))

                    elif action == "stand":
                        update_blackjack_player(sess["id"], player_id,
                            hand=json.dumps(hand), status="stand")

                    # Check if all players done
                    players = get_blackjack_players(sess["id"])
                    all_done = all(p["status"] in ("stand", "bust", "done") for p in players)

                    if all_done:
                        # Dealer plays
                        while hand_value(dealer_hand) < 17:
                            dealer_hand.append(deck.pop())
                        dealer_val = hand_value(dealer_hand)
                        update_blackjack_session(sess["id"],
                            dealer_hand=json.dumps(dealer_hand),
                            deck=json.dumps(deck),
                            status="finished"
                        )

                        for p in players:
                            if p["status"] == "bust":
                                continue
                            p_val = hand_value(json.loads(p["hand"]))
                            bet = p["bet"]
                            if dealer_val > 21 or p_val > dealer_val:
                                add_coins(p["telegram_id"], bet * 2, "Blackjack gagné")
                                update_blackjack_player(sess["id"], p["telegram_id"], result="win")
                            elif p_val == dealer_val:
                                add_coins(p["telegram_id"], bet, "Blackjack égalité")
                                update_blackjack_player(sess["id"], p["telegram_id"], result="push")
                            else:
                                update_blackjack_player(sess["id"], p["telegram_id"], result="lose")

                    await _bj_broadcast(token)

    except Exception:
        pass
    finally:
        _bj_clients.get(token, set()).discard(ws)


@app.post("/blackjack/{token}/rematch")
async def blackjack_rematch(token: str, request: Request):
    body = await request.json()
    caller_id = body.get("telegram_id")

    session = get_blackjack_session(token)
    if not session:
        return {"ok": False, "error": "Session introuvable"}
    if session["creator_id"] != caller_id:
        return {"ok": False, "error": "Seul le créateur peut relancer la partie"}
    if session["status"] != "finished":
        return {"ok": False, "error": "La partie n'est pas encore terminée"}

    old_players = get_blackjack_players(session["id"])
    if not old_players:
        return {"ok": False, "error": "Aucun joueur trouvé"}

    # Vérifier les soldes
    for p in old_players:
        if get_coins(p["telegram_id"]) < p["bet"]:
            u = get_user(p["telegram_id"])
            name = u["username"] if u else str(p["telegram_id"])
            return {"ok": False, "error": f"Solde insuffisant pour {name}"}

    # Nouvelle session
    new_token = secrets.token_urlsafe(8)
    new_sid = create_blackjack_session(caller_id, new_token)

    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]
    hands: dict[int, list] = {}

    for p in old_players:
        hand = [deck.pop(), deck.pop()]
        hands[p["telegram_id"]] = hand
        add_blackjack_player(new_sid, p["telegram_id"], p["bet"])
        add_coins(p["telegram_id"], -p["bet"], "Mise blackjack (revanche)")
        update_blackjack_player(new_sid, p["telegram_id"], hand=json.dumps(hand), status="playing")

    update_blackjack_session(new_sid,
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand)
    )

    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    bj_url = f"{site}/blackjack.html?session={new_token}"

    # Texte état global
    all_players = get_blackjack_players(new_sid)
    state_lines = ["👥 *Mains de tout le monde :*\n"]
    for p in all_players:
        u = get_user(p["telegram_id"])
        name = u["username"] if u else "?"
        h = json.loads(p["hand"])
        state_lines.append(f"🎮 *{name}* : {display_hand(h)}")
    state_lines.append(f"\n🏠 *Croupier* : {display_hand(dealer_hand, hide_second=True)}")
    state_txt = "\n".join(state_lines)

    bj_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🃏 Hit",   callback_data="bj:hit"),
        InlineKeyboardButton("✋ Stand", callback_data="bj:stand"),
    ]])

    for p in all_players:
        hand = json.loads(p["hand"])
        msg = (
            f"🔄 *Revanche !*\n\n"
            f"🎴 Ta main : {display_hand(hand)}\n\n"
            f"{state_txt}\n\n"
            f"🌐 {bj_url}"
        )
        if is_blackjack(hand):
            bet = p["bet"]
            winnings = int(bet * 1.5)
            add_coins(p["telegram_id"], bet + winnings, "Blackjack ! (×1.5)")
            update_blackjack_player(new_sid, p["telegram_id"], status="done", result="blackjack")
            msg += f"\n\n🎉 *BLACKJACK !* Tu gagnes {winnings} 🪙 !"
            try:
                await _bot_app.bot.send_message(chat_id=p["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception:
                pass
        else:
            try:
                await _bot_app.bot.send_message(
                    chat_id=p["telegram_id"], text=msg,
                    parse_mode="Markdown", reply_markup=bj_kb
                )
            except Exception:
                pass

    # Notifier tous les clients sur la nouvelle session
    await _bj_broadcast(new_token)
    return {"ok": True, "token": new_token}
