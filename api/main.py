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
    get_all_balances, get_transactions,
    get_blackjack_session, get_blackjack_players, update_blackjack_session,
    update_blackjack_player, create_blackjack_session, add_blackjack_player,
    follow_user, unfollow_user, is_following, get_following,
)
from core.recap import build_weekly_recap
from core.widmark import total_bac, bac_label, sober_in_hours
from core.blackjack import new_deck, hand_value, display_hand, is_blackjack

load_dotenv()

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
        BotCommand("p",          "Configurer ton profil  →  /p 80 h"),
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
        BotCommand("bucket",     "🪣 Bucket thaïlandais (375ml, 40°)"),
        BotCommand("solde",      "🪙 Voir ton solde de BeerCoins"),
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


@app.websocket("/ws/blackjack/{token}")
async def blackjack_ws(ws: WebSocket, token: str):
    session = get_blackjack_session(token)
    if not session:
        await ws.close()
        return
    await ws.accept()
    _bj_clients.setdefault(token, set()).add(ws)

    async def broadcast_state():
        sess = get_blackjack_session(token)
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

    try:
        await broadcast_state()
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

                    await broadcast_state()

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

    return {"ok": True, "token": new_token}
