import os
import json
import secrets
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, Application, filters
)
from core.drinks import DRINKS, list_drinks_text
from core.widmark import alcohol_grams, total_bac, bac_label, sober_in_hours
from core.recap import build_weekly_recap
from core.blackjack import new_deck, hand_value, is_blackjack, display_hand, dealer_should_hit
from data.database import (
    init_db, upsert_user, get_user, get_user_by_username, get_all_users,
    start_session, get_active_session, log_drink, get_session_drinks,
    get_session_drinks_detail, delete_last_drink, end_session, update_location,
    is_banned, ban_user, unban_user, rename_user, get_top_drinks, update_max_bac,
    get_coins, add_coins, get_transactions, get_all_balances,
    create_bet, get_pending_bet_for, accept_bet, cancel_bet, settle_bet, get_bet,
    create_blackjack_session, get_blackjack_session, get_blackjack_session_by_id,
    update_blackjack_session,
    add_blackjack_player, get_blackjack_players, update_blackjack_player,
    get_blackjack_session_by_player, _get_waiting_session_by_creator,
    is_following, follow_user, unfollow_user, get_following,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# ── Conversation states ───────────────────────────────────────────────────────
BET_TYPE, BET_OPPONENT, BET_TIME, BET_AMOUNT = range(4)
BJ_MODE, BJ_PLAYERS, BJ_BET, BJ_PLAYING = range(4, 8)

ALIAS_MAP: dict[str, str] = {}
for key, drink in DRINKS.items():
    for alias in drink.aliases:
        ALIAS_MAP[alias.lower().replace("é", "e").replace("è", "e")] = key

# Messages fun par niveau de TAC
FUN_MESSAGES = [
    [],  # 0 — sobre
    ["Petit chauffage en cours 🔥", "On commence bien la soirée 😏"],
    ["Bonne ambiance 😄", "C'est parti ! 🎉", "Tu commences à voir la vie en rose 🌹"],
    ["⚠️ Approche de la limite légale !", "T'as les yeux qui brillent là 👀", "Conduis pas hein 🚗❌"],
    ["🔴 Là t'es bien lancé(e) !", "Les jambes commencent à décorer ? 🕺", "T'es sûr(e) d'en rajouter un ? 😅"],
    ["💀 Légende vivante", "Les murs te parlent ? 🌀", "Quelqu'un appelle un taxi 🚕"],
]

import random

def fun_message(bac: float) -> str:
    if bac == 0: return ""
    if bac < 0.2: lvl = 1
    elif bac < 0.5: lvl = 2
    elif bac < 0.8: lvl = 3
    elif bac < 1.5: lvl = 4
    else: lvl = 5
    msgs = FUN_MESSAGES[lvl]
    return f"\n_{random.choice(msgs)}_" if msgs else ""

PARIS = ZoneInfo("Europe/Paris")

def sober_time_str(bac: float) -> str:
    if bac <= 0:
        return "maintenant"
    h = sober_in_hours(bac)
    target = datetime.now(PARIS) + timedelta(hours=h)
    return f"vers {target.strftime('%Hh%M')} (~{h:.1f}h)"


def ensure_session(telegram_id: int):
    if not get_active_session(telegram_id):
        start_session(telegram_id)


async def _notify_all(ctx, sender_id: int, message: str):
    """Envoie un message à tous les utilisateurs qui suivent l'expéditeur."""
    for user in get_all_users():
        if user["telegram_id"] != sender_id and is_following(user["telegram_id"], sender_id):
            try:
                await ctx.bot.send_message(chat_id=user["telegram_id"], text=message, parse_mode="Markdown")
            except Exception:
                pass


async def _notify_everyone(ctx, message: str):
    """Envoie un message à tous les utilisateurs sans exception."""
    for user in get_all_users():
        try:
            await ctx.bot.send_message(chat_id=user["telegram_id"], text=message, parse_mode="Markdown")
        except Exception:
            pass


async def _refresh_api():
    api_url = os.environ.get("API_URL", "https://drunk-l34t.onrender.com")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{api_url}/refresh", timeout=3)
    except Exception:
        pass


TOPO_MSG = (
    "🍺 *Bienvenue sur Drunk ! Par Allain*\n\n"
    "Ce bot te permet de suivre ton taux d'alcool en temps réel avec tes potes.\n\n"
    "*Comment ça marche ?*\n\n"
    "1️⃣ Configure ton profil une seule fois :\n"
    "`/p 80 h` _(poids en kg + h pour homme, f pour femme)_\n\n"
    "2️⃣ À chaque verre, tape juste le nom :\n"
    "`pinte` `demi` `vodka` `vin` `mojito`...\n\n"
    "3️⃣ Le bot calcule ton TAC en temps réel et te dit à quelle heure tu seras sobre.\n\n"
    "*Commandes utiles :*\n"
    "• `/site` — voir le dashboard en temps réel\n"
    "• `/tac` — voir ton taux d'alcool actuel\n"
    "• `/annuler` — supprimer le dernier verre\n"
    "• `/liste` — toutes les boissons disponibles\n\n"
    "_Tape `/` pour voir toutes les commandes disponibles_ 🎉"
)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TOPO_MSG, parse_mode="Markdown")


# ── /topo ─────────────────────────────────────────────────────────────────────

async def cmd_topo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TOPO_MSG, parse_mode="Markdown")


# ── /profil ───────────────────────────────────────────────────────────────────

async def cmd_profil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if len(args) != 2:
        await update.message.reply_text("❌ Usage : /p 80 h ou /p 60 f")
        return
    try:
        weight = float(args[0].replace(",", "."))
        gender = args[1].lower()
        gender = {"h": "homme", "f": "femme"}.get(gender, gender)
        assert gender in ("homme", "femme")
        assert 30 < weight < 250
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Exemple : /p 80 h ou /p 60 f")
        return
    name = user.first_name or user.username or str(user.id)
    upsert_user(user.id, name, weight, gender)
    ensure_session(user.id)
    await update.message.reply_text(
        f"✅ Profil enregistré, *{name}* ! ({weight}kg — {gender})\n"
        f"Envoie `pinte`, `demi`, `vodka`... pour commencer.",
        parse_mode="Markdown"
    )


# ── Clavier rapide ────────────────────────────────────────────────────────────

def _quick_keyboard(tid: int, last_key: str) -> InlineKeyboardMarkup:
    top = get_top_drinks(tid, 5)
    suggestions = [last_key]
    for k in top:
        if k not in suggestions:
            suggestions.append(k)
        if len(suggestions) == 3:
            break
    buttons = [
        InlineKeyboardButton(DRINKS[k].name, callback_data=f"drink:{k}")
        for k in suggestions if k in DRINKS
    ]
    return InlineKeyboardMarkup([buttons])


async def _send_drink_response(reply_func, tid: int, drink_key: str, user_data: dict, ctx):
    drinks_data = get_session_drinks(tid)
    nb = len(drinks_data)
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    prev_bac = total_bac(drinks_data[:-1], user_data["weight_kg"], user_data["gender"]) if nb > 1 else 0.0
    drink = DRINKS[drink_key]

    text = (
        f"✅ *{drink.name}*\n\n"
        f"🧪 TAC : *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre {sober_time_str(bac)}"
        f"{fun_message(bac)}"
    )
    if nb > 0 and nb % 3 == 0:
        text += f"\n\n💧 *{nb} verres — pense à boire de l'eau !*"

    update_max_bac(tid, bac)
    add_coins(tid, 5, f"Verre bu ({DRINKS[drink_key].name})")
    await reply_func(text, parse_mode="Markdown", reply_markup=_quick_keyboard(tid, drink_key))

    if nb == 1:
        site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
        await _notify_all(ctx, tid, f"🍺 *{user_data['username']}* commence à boire ! Rejoins-le !\n{site}")
    if prev_bac < 0.8 <= bac:
        await _notify_all(ctx, tid, f"⚠️ *{user_data['username']}* vient de dépasser la limite légale ({bac:.2f} g/L) 🚨")

    await _refresh_api()


# ── Boisson ───────────────────────────────────────────────────────────────────

async def _do_drink(update: Update, ctx: ContextTypes.DEFAULT_TYPE, drink_key: str):
    tid = update.effective_user.id
    if await _check_banned(update): return
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil d'abord : /p 80 h")
        return
    ensure_session(tid)
    drink = DRINKS[drink_key]
    log_drink(tid, drink_key, alcohol_grams(drink.volume_ml, drink.abv))
    await _send_drink_response(update.message.reply_text, tid, drink_key, user_data, ctx)


async def handle_drink_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    drink_key = query.data.split(":")[1]
    tid = update.effective_user.id
    if is_banned(tid):
        await query.answer("🚫 Tu as été banni.", show_alert=True)
        return
    user_data = get_user(tid)
    if not user_data:
        await query.answer("❌ Configure ton profil : /p 80 h", show_alert=True)
        return
    ensure_session(tid)
    drink = DRINKS[drink_key]
    log_drink(tid, drink_key, alcohol_grams(drink.volume_ml, drink.abv))
    await _send_drink_response(query.message.reply_text, tid, drink_key, user_data, ctx)


# ── /annuler ──────────────────────────────────────────────────────────────────

async def cmd_annuler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    drink_key = delete_last_drink(tid)
    if not drink_key:
        await update.message.reply_text("Aucun verre à annuler.")
        return
    drink = DRINKS.get(drink_key)
    nom = drink.name if drink else drink_key
    user_data = get_user(tid)
    drinks_data = get_session_drinks(tid)
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"]) if user_data else 0
    await update.message.reply_text(
        f"↩️ *{nom}* annulé.\n🧪 TAC : *{bac:.2f} g/L*",
        parse_mode="Markdown"
    )
    await _refresh_api()


# ── /tac ──────────────────────────────────────────────────────────────────────

async def cmd_tac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil : /p 80 h")
        return
    drinks_data = get_session_drinks(tid)
    if not drinks_data:
        await update.message.reply_text("🫗 Aucune boisson encore.")
        return
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    await update.message.reply_text(
        f"🧪 *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre {sober_time_str(bac)}\n"
        f"_{len(drinks_data)} verre(s)_",
        parse_mode="Markdown"
    )


# ── /historique ───────────────────────────────────────────────────────────────

async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    rows = get_session_drinks_detail(tid)
    if not rows:
        await update.message.reply_text("🫗 Aucune boisson cette session.")
        return
    lines = ["📋 *Tes boissons :*\n"]
    for i, r in enumerate(rows, 1):
        heure = datetime.fromisoformat(r["logged_at"]).strftime("%H:%M")
        drink = DRINKS.get(r["drink_key"])
        nom = drink.name if drink else r["drink_key"]
        lines.append(f"{i}. {nom} — {heure}")
    lines.append(f"\n_Total : {len(rows)} verre(s)_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /defi ─────────────────────────────────────────────────────────────────────

async def cmd_defi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    if not users:
        await update.message.reply_text("Aucun joueur enregistré.")
        return

    scores = []
    for u in users:
        drinks = get_session_drinks(u["telegram_id"])
        bac = total_bac(drinks, u["weight_kg"], u["gender"]) if drinks else 0
        scores.append((u["username"], bac, len(drinks)))

    scores.sort(key=lambda x: x[1], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Classement de la soirée :*\n"]
    for i, (name, bac, nb) in enumerate(scores):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} *{name}* — {bac:.2f} g/L ({nb} verre(s))")

    # Plus sobre
    sobre = min(scores, key=lambda x: x[1])
    ivre = max(scores, key=lambda x: x[1])
    lines.append(f"\n😇 Plus sobre : *{sobre[0]}*")
    if ivre[0] != sobre[0]:
        lines.append(f"🤪 Plus festif : *{ivre[0]}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _check_banned(update: Update) -> bool:
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Tu as été banni de ce bot.")
        return True
    return False


# ── /invite ───────────────────────────────────────────────────────────────────

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if await _check_banned(update): return
    user = get_user(tid)
    if not user:
        await update.message.reply_text("❌ Configure ton profil d'abord : /p 80 h")
        return
    lat, lon = user.get("latitude"), user.get("longitude")
    msg = f"🎉 *{user['username']}* t'invite à venir boire avec lui/elle !"
    if lat and lon:
        msg += "\n📍 Sa position ci-dessous 👇"
    for other in get_all_users():
        if other["telegram_id"] != tid:
            try:
                await ctx.bot.send_message(chat_id=other["telegram_id"], text=msg, parse_mode="Markdown")
                if lat and lon:
                    await ctx.bot.send_location(chat_id=other["telegram_id"], latitude=lat, longitude=lon)
            except Exception:
                pass
    await update.message.reply_text("✅ Invitation envoyée à tous !")


# ── /ou ───────────────────────────────────────────────────────────────────────

async def cmd_ou(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /ou <prénom>")
        return
    username = ctx.args[0]
    target = get_user_by_username(username)
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{username}' introuvable.")
        return
    lat, lon = target.get("latitude"), target.get("longitude")
    if not lat or not lon:
        await update.message.reply_text(f"📍 *{target['username']}* n'a pas encore partagé sa position.", parse_mode="Markdown")
        return
    await update.message.reply_location(latitude=lat, longitude=lon)
    await update.message.reply_text(f"📍 Dernière position connue de *{target['username']}*", parse_mode="Markdown")


# ── /notif ────────────────────────────────────────────────────────────────────

async def cmd_notif(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if ADMIN_ID == 0:
        await update.message.reply_text(f"⚙️ Ton ID Telegram : `{tid}`\nAjoute `ADMIN_ID={tid}` dans les variables Render.", parse_mode="Markdown")
        return
    if tid != ADMIN_ID:
        await update.message.reply_text("❌ Commande réservée à l'admin.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage : /notif <message>")
        return
    message = "📢 " + " ".join(ctx.args)
    await _notify_everyone(ctx, message)
    await update.message.reply_text(f"✅ Message envoyé à tous !")


# ── /ban /unban /rename (admin) ──────────────────────────────────────────────

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage : /ban <prénom>")
        return
    target = get_user_by_username(ctx.args[0])
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{ctx.args[0]}' introuvable.")
        return
    ban_user(target["telegram_id"])
    await update.message.reply_text(f"🚫 *{target['username']}* banni.", parse_mode="Markdown")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage : /unban <prénom>")
        return
    target = get_user_by_username(ctx.args[0])
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{ctx.args[0]}' introuvable.")
        return
    unban_user(target["telegram_id"])
    await update.message.reply_text(f"✅ *{target['username']}* débanni.", parse_mode="Markdown")


async def cmd_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage : /rename <nouveau> (soi-même) ou /rename <ancien> <nouveau> (admin)"""
    tid = update.effective_user.id
    if len(ctx.args) == 1:
        # Tous les utilisateurs peuvent changer leur propre pseudo
        if not get_user(tid):
            await update.message.reply_text("❌ Configure ton profil d'abord : /p 80 h")
            return
        new_name = ctx.args[0]
        rename_user(tid, new_name)
        await update.message.reply_text(f"✅ Pseudo changé en *{new_name}* !", parse_mode="Markdown")
        await _refresh_api()
    elif len(ctx.args) == 2 and _is_admin(tid):
        # Admin peut renommer quelqu'un d'autre
        target = get_user_by_username(ctx.args[0])
        if not target:
            await update.message.reply_text(f"❌ Utilisateur '{ctx.args[0]}' introuvable.")
            return
        new_name = ctx.args[1]
        rename_user(target["telegram_id"], new_name)
        await update.message.reply_text(f"✅ Renommé : *{target['username']}* → *{new_name}*", parse_mode="Markdown")
        await _refresh_api()
    else:
        await update.message.reply_text("Usage : /rename <nouveau_pseudo>")


# ── /notifmaj ─────────────────────────────────────────────────────────────────

async def cmd_notifmaj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if ADMIN_ID and tid != ADMIN_ID:
        await update.message.reply_text("❌ Commande réservée à l'admin.")
        return
    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    msg = f"🔔 *Mise à jour disponible !*\nLe bot vient d'être amélioré — nouvelles fonctionnalités disponibles.\n{site}"
    await _notify_everyone(ctx, msg)
    await update.message.reply_text("✅ Notification envoyée !")


# ── /addverre /delverre (admin) ───────────────────────────────────────────────

def _is_admin(tid: int) -> bool:
    return ADMIN_ID != 0 and tid == ADMIN_ID


async def cmd_addverre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage : /addverre <username> <boisson>"""
    tid = update.effective_user.id
    if not _is_admin(tid):
        await update.message.reply_text("❌ Réservé à l'admin.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage : /add <prénom> <boisson>")
        return
    username, drink_alias = ctx.args[0], ctx.args[1].lower()
    target = get_user_by_username(username)
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{username}' introuvable.")
        return
    drink_key = ALIAS_MAP.get(drink_alias)
    if not drink_key:
        await update.message.reply_text(f"❌ Boisson '{drink_alias}' inconnue.")
        return
    ensure_session(target["telegram_id"])
    drink = DRINKS[drink_key]
    alc_g = alcohol_grams(drink.volume_ml, drink.abv)
    log_drink(target["telegram_id"], drink_key, alc_g)
    drinks_data = get_session_drinks(target["telegram_id"])
    bac = total_bac(drinks_data, target["weight_kg"], target["gender"])
    await update.message.reply_text(f"✅ *{drink.name}* ajouté à *{target['username']}*\n🧪 Son TAC : *{bac:.2f} g/L*", parse_mode="Markdown")
    await _refresh_api()


async def cmd_delverre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage : /delverre <username>"""
    tid = update.effective_user.id
    if not _is_admin(tid):
        await update.message.reply_text("❌ Réservé à l'admin.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage : /del <prénom>")
        return
    username = ctx.args[0]
    target = get_user_by_username(username)
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{username}' introuvable.")
        return
    drink_key = delete_last_drink(target["telegram_id"])
    if not drink_key:
        await update.message.reply_text(f"❌ Aucun verre à supprimer pour {username}.")
        return
    drink = DRINKS.get(drink_key)
    nom = drink.name if drink else drink_key
    await update.message.reply_text(f"↩️ *{nom}* supprimé pour *{target['username']}*", parse_mode="Markdown")
    await _refresh_api()


# ── /recap (admin) ────────────────────────────────────────────────────────────

async def cmd_recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not _is_admin(tid):
        await update.message.reply_text("❌ Réservé à l'admin.")
        return
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    msg = build_weekly_recap(since, now)
    # Prévisualisation pour l'admin seulement
    await update.message.reply_text(msg, parse_mode="Markdown")
    # Si arg "send" → envoie à tout le monde
    if ctx.args and ctx.args[0].lower() == "send":
        await _notify_everyone(ctx, msg)
        await update.message.reply_text("✅ Recap envoyé à tous !")


# ── /site ─────────────────────────────────────────────────────────────────────

async def cmd_site(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    await update.message.reply_text(f"🌐 Dashboard en temps réel :\n{url}")


# ── /stop ─────────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    end_session(update.effective_user.id)
    ensure_session(update.effective_user.id)
    await update.message.reply_text("✅ Compteurs remis à zéro !")
    await _refresh_api()


# ── Position ──────────────────────────────────────────────────────────────────

async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not get_user(tid):
        return
    loc = update.message.location
    update_location(tid, loc.latitude, loc.longitude)
    await update.message.reply_text("📍 Position enregistrée !", reply_markup=ReplyKeyboardRemove())
    await _refresh_api()


# ── Handler texte libre ───────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    text = text.replace("é", "e").replace("è", "e").replace("à", "a")

    if text in ("liste", "list", "l"):
        await update.message.reply_text(list_drinks_text(), parse_mode="Markdown")
        return
    if text in ("tac", "t"):
        await cmd_tac(update, ctx); return
    if text in ("stop", "reset", "r"):
        await cmd_stop(update, ctx); return
    if text in ("historique", "histo", "h"):
        await cmd_historique(update, ctx); return
    if text in ("annuler", "a"):
        await cmd_annuler(update, ctx); return
    if text in ("defi", "défi", "classement"):
        await cmd_defi(update, ctx); return

    # Blackjack multi-player : hit/stand hors ConversationHandler
    if text in ("hit", "carte", "stand", "rester"):
        session = get_blackjack_session_by_player(tid)
        if session and session["status"] == "active":
            players = get_blackjack_players(session["id"])
            player = next((p for p in players if p["telegram_id"] == tid), None)
            if player and player["status"] == "playing":
                await _bj_action(update.message.reply_text, tid, text, session, ctx.bot)
                return

    drink_key = ALIAS_MAP.get(text)
    if drink_key:
        await _do_drink(update, ctx, drink_key)


# ── /solde ────────────────────────────────────────────────────────────────────

async def cmd_solde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    coins = get_coins(tid)
    txs = get_transactions(tid, 5)
    lines = [f"🪙 *Ton solde : {coins} BeerCoins*\n", "*Dernières transactions :*"]
    for t in txs:
        sign = "+" if t["amount"] > 0 else ""
        lines.append(f"{sign}{t['amount']} 🪙 — {t['reason']}")

    all_bal = get_all_balances()
    rank = next((i + 1 for i, b in enumerate(all_bal) if b["telegram_id"] == tid), "?")
    lines.append(f"\n_Classement : #{rank} sur {len(all_bal)}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /offrir ───────────────────────────────────────────────────────────────────

async def cmd_offrir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage : /offrir <prénom> <montant>")
        return
    target = get_user_by_username(ctx.args[0])
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{ctx.args[0]}' introuvable.")
        return
    try:
        amount = int(ctx.args[1])
        assert amount > 0
    except Exception:
        await update.message.reply_text("❌ Montant invalide.")
        return
    sender = get_user(tid)
    if get_coins(tid) < amount:
        await update.message.reply_text("❌ Solde insuffisant.")
        return
    add_coins(tid, -amount, f"Offert à {target['username']}")
    add_coins(target["telegram_id"], amount, f"Reçu de {sender['username']}")
    await update.message.reply_text(
        f"✅ {amount} 🪙 envoyés à *{target['username']}* !",
        parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(
            chat_id=target["telegram_id"],
            text=(f"🎁 *{sender['username']}* t'a offert *{amount} 🪙* !\n"
                  f"💰 Nouveau solde : {get_coins(target['telegram_id'])} 🪙"),
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ── /pari conversation ────────────────────────────────────────────────────────

async def cmd_pari_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not get_user(tid):
        await update.message.reply_text("❌ Configure ton profil d'abord : /p 80 h")
        return ConversationHandler.END
    await update.message.reply_text(
        "🎰 *Quel type de pari ?*\n\n"
        "1️⃣ Plus de verres — qui boit le plus ?\n"
        "2️⃣ Plus ivre — qui a le TAC le plus haut ?\n"
        "3️⃣ Pile ou face — 50/50 immédiat\n\n"
        "_Réponds avec le numéro_",
        parse_mode="Markdown"
    )
    return BET_TYPE


async def cmd_pari_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice not in ("1", "2", "3"):
        await update.message.reply_text("❌ Réponds avec 1, 2 ou 3.")
        return BET_TYPE
    ctx.user_data["bet_type"] = {"1": "verres", "2": "ivre", "3": "coinflip"}[choice]
    await update.message.reply_text("👤 Contre qui ? (entre le prénom)")
    return BET_OPPONENT


async def cmd_pari_opponent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = get_user_by_username(update.message.text.strip())
    if not target:
        await update.message.reply_text("❌ Utilisateur introuvable. Réessaie.")
        return BET_OPPONENT
    if target["telegram_id"] == update.effective_user.id:
        await update.message.reply_text("❌ Tu peux pas parier contre toi-même.")
        return BET_OPPONENT
    ctx.user_data["bet_opponent"] = target
    bet_type = ctx.user_data["bet_type"]
    if bet_type in ("verres", "ivre"):
        await update.message.reply_text("⏰ À quelle heure on règle le pari ? (ex: 23h00 ou 01h30)")
        return BET_TIME
    else:
        coins = get_coins(update.effective_user.id)
        await update.message.reply_text(f"💰 Quelle est ta mise ? (solde : {coins} 🪙)")
        return BET_AMOUNT


async def cmd_pari_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import re
    text = update.message.text.strip().replace("h", ":").replace("H", ":")
    match = re.match(r"(\d{1,2})[:\s]?(\d{0,2})", text)
    if not match:
        await update.message.reply_text("❌ Format invalide. Exemple : 23h00 ou 01h30")
        return BET_TIME
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    ctx.user_data["bet_time"] = f"{hour:02d}:{minute:02d}"
    coins = get_coins(update.effective_user.id)
    await update.message.reply_text(f"💰 Quelle est ta mise ? (solde : {coins} 🪙)")
    return BET_AMOUNT


async def cmd_pari_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    try:
        amount = int(update.message.text.strip())
        assert amount > 0
    except Exception:
        await update.message.reply_text("❌ Montant invalide.")
        return BET_AMOUNT
    if get_coins(tid) < amount:
        await update.message.reply_text(f"❌ Solde insuffisant (tu as {get_coins(tid)} 🪙).")
        return BET_AMOUNT

    opponent = ctx.user_data["bet_opponent"]
    if get_coins(opponent["telegram_id"]) < amount:
        await update.message.reply_text(f"❌ {opponent['username']} n'a pas assez de 🪙.")
        return ConversationHandler.END

    bet_type = ctx.user_data["bet_type"]
    end_time = ctx.user_data.get("bet_time")
    sender = get_user(tid)

    bet_id = create_bet(tid, opponent["telegram_id"], bet_type, amount, end_time)

    type_labels = {"verres": "plus de verres", "ivre": "TAC le plus haut", "coinflip": "pile ou face"}
    label = type_labels[bet_type]
    end_str = f" (règlement à {end_time})" if end_time else ""

    msg = (
        f"🎰 *{sender['username']}* te propose un pari !\n\n"
        f"Type : *{label}*{end_str}\n"
        f"Mise : *{amount} 🪙*\n\n"
        f"Réponds /accepter pour accepter ou /refuser pour décliner."
    )

    ctx.user_data.clear()

    try:
        await ctx.bot.send_message(chat_id=opponent["telegram_id"], text=msg, parse_mode="Markdown")
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ Pari envoyé à *{opponent['username']}* — en attente de sa réponse !",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cmd_pari_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Pari annulé.")
    return ConversationHandler.END


# ── /accepter & /refuser ──────────────────────────────────────────────────────

async def cmd_accepter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    bet = get_pending_bet_for(tid)
    if not bet:
        await update.message.reply_text("❌ Aucun pari en attente.")
        return

    if get_coins(tid) < bet["amount"]:
        await update.message.reply_text("❌ Solde insuffisant pour accepter ce pari.")
        return

    if bet["bet_type"] == "coinflip":
        import random as _rand
        winner_id = _rand.choice([bet["challenger_id"], bet["opponent_id"]])
        loser_id = bet["opponent_id"] if winner_id == bet["challenger_id"] else bet["challenger_id"]
        settle_bet(bet["id"], winner_id)
        winner = get_user(winner_id)
        loser = get_user(loser_id)
        add_coins(winner_id, bet["amount"], f"Pari gagné contre {loser['username']}")
        add_coins(loser_id, -bet["amount"], f"Pari perdu contre {winner['username']}")
        result_msg = (
            f"🪙 *Pile ou face !*\n\n"
            f"🏆 Gagnant : *{winner['username']}* +{bet['amount']} 🪙\n"
            f"💸 Perdant : *{loser['username']}* -{bet['amount']} 🪙"
        )
        for uid in [bet["challenger_id"], bet["opponent_id"]]:
            try:
                await ctx.bot.send_message(chat_id=uid, text=result_msg, parse_mode="Markdown")
            except Exception:
                pass
    else:
        accept_bet(bet["id"])
        type_labels = {"verres": "plus de verres", "ivre": "TAC le plus haut"}
        label = type_labels.get(bet["bet_type"], bet["bet_type"])
        end_str = f" à {bet['end_time']}" if bet.get("end_time") else ""
        msg = f"✅ Pari accepté ! *{label}*{end_str} — mise : {bet['amount']} 🪙\nBonne chance ! 🍀"
        await update.message.reply_text(msg, parse_mode="Markdown")
        try:
            await ctx.bot.send_message(
                chat_id=bet["challenger_id"],
                text=f"✅ *{get_user(tid)['username']}* a accepté ton pari ! {msg}",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def cmd_refuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    bet = get_pending_bet_for(tid)
    if not bet:
        await update.message.reply_text("❌ Aucun pari en attente.")
        return
    cancel_bet(bet["id"])
    await update.message.reply_text("❌ Pari refusé.")
    try:
        await ctx.bot.send_message(
            chat_id=bet["challenger_id"],
            text=f"❌ *{get_user(tid)['username']}* a refusé ton pari.",
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ── /blackjack conversation ───────────────────────────────────────────────────

async def cmd_bj_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not get_user(tid):
        await update.message.reply_text("❌ Configure ton profil d'abord : /p 80 h")
        return ConversationHandler.END

    # Rembourser toute partie non terminée
    old = get_blackjack_session_by_player(tid)
    if old:
        players = get_blackjack_players(old["id"])
        player = next((p for p in players if p["telegram_id"] == tid), None)
        if player and player["bet"] > 0 and player["status"] not in ("bust", "done"):
            add_coins(tid, player["bet"], "Remboursement partie abandonnée")
            update_blackjack_player(old["id"], tid, status="done", result="abandoned")
            await update.message.reply_text(f"↩️ Ancienne partie annulée — *{player['bet']} 🪙* remboursés.", parse_mode="Markdown")
        update_blackjack_session(old["id"], status="finished")

    coins = get_coins(tid)
    if coins <= 0:
        await update.message.reply_text("❌ Tu n'as plus de 🪙 pour jouer.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🃏 *Blackjack !*\n\n"
        "Solo ou multi ?\n"
        "1️⃣ Solo (contre le croupier)\n"
        "2️⃣ Multi (invite des joueurs)",
        parse_mode="Markdown"
    )
    return BJ_MODE


async def cmd_bj_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice not in ("1", "2"):
        await update.message.reply_text("❌ Réponds 1 (solo) ou 2 (multi).")
        return BJ_MODE
    ctx.user_data["bj_mode"] = "solo" if choice == "1" else "multi"
    if ctx.user_data["bj_mode"] == "multi":
        await update.message.reply_text(
            "👥 Qui invites-tu ? (entre les prénoms séparés par des espaces)\nEx: Thomas Lucas Marie"
        )
        return BJ_PLAYERS
    else:
        coins = get_coins(update.effective_user.id)
        await update.message.reply_text(f"💰 Quelle est ta mise ? (solde : {coins} 🪙)")
        return BJ_BET


async def cmd_bj_players(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    names = update.message.text.strip().split()
    players = []
    not_found = []
    for name in names:
        u = get_user_by_username(name)
        if u and u["telegram_id"] != tid:
            players.append(u)
        else:
            not_found.append(name)
    if not_found:
        await update.message.reply_text(f"❌ Introuvable : {', '.join(not_found)}. Réessaie.")
        return BJ_PLAYERS
    if not players:
        await update.message.reply_text("❌ Aucun joueur valide.")
        return BJ_PLAYERS
    ctx.user_data["bj_invited"] = players
    coins = get_coins(tid)
    await update.message.reply_text(f"💰 Quelle est ta mise ? (solde : {coins} 🪙)")
    return BJ_BET


async def cmd_bj_bet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    try:
        bet = int(update.message.text.strip())
        assert 1 <= bet <= get_coins(tid)
    except Exception:
        await update.message.reply_text(f"❌ Mise invalide (solde : {get_coins(tid)} 🪙).")
        return BJ_BET

    ctx.user_data["bj_bet"] = bet
    token = secrets.token_urlsafe(8)
    session_id = create_blackjack_session(tid, token)
    ctx.user_data["bj_token"] = token
    ctx.user_data["bj_session_id"] = session_id

    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    bj_url = f"{site}/blackjack.html?session={token}"

    if ctx.user_data.get("bj_mode") == "solo":
        deck = new_deck()
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]

        update_blackjack_session(session_id,
            status="active",
            deck=json.dumps(deck),
            dealer_hand=json.dumps(dealer_hand)
        )
        add_blackjack_player(session_id, tid, bet)
        update_blackjack_player(session_id, tid, hand=json.dumps(player_hand), status="playing")

        add_coins(tid, -bet, "Mise blackjack")

        text = (
            f"🃏 *Blackjack — Partie solo*\n\n"
            f"🎴 Ta main : {display_hand(player_hand)}\n"
            f"🏠 Croupier : {display_hand(dealer_hand, hide_second=True)}\n\n"
            f"🌐 {bj_url}"
        )

        if is_blackjack(player_hand):
            winnings = int(bet * 1.5)
            add_coins(tid, bet + winnings, "Blackjack ! (×1.5)")
            update_blackjack_player(session_id, tid, status="done", result="blackjack")
            update_blackjack_session(session_id, status="finished")
            text += f"\n\n🎉 *BLACKJACK !* Tu gagnes {winnings} 🪙 !"
            await update.message.reply_text(text, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_bj_keyboard())
            return BJ_PLAYING

        ctx.user_data.clear()
        return ConversationHandler.END
    else:
        invited = ctx.user_data.get("bj_invited", [])
        host = get_user(tid)
        add_blackjack_player(session_id, tid, bet)
        add_coins(tid, -bet, "Mise blackjack")

        names = ", ".join(u["username"] for u in invited)

        # Invitation avec bouton Rejoindre pour chaque invité
        join_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🃏 Rejoindre ({bet} 🪙)", callback_data=f"bj_join:{token}:{bet}")
        ]])
        for u in invited:
            try:
                await ctx.bot.send_message(
                    chat_id=u["telegram_id"],
                    text=(
                        f"🃏 *{host['username']}* t'invite au Blackjack !\n"
                        f"Mise : *{bet} 🪙*\n\n"
                        f"🌐 Table en direct : {bj_url}"
                    ),
                    parse_mode="Markdown",
                    reply_markup=join_kb
                )
            except Exception:
                pass

        # Bouton Lancer pour l'hôte
        launch_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Lancer la partie", callback_data=f"bj_launch:{token}")
        ]])
        await update.message.reply_text(
            f"✅ Invitations envoyées à {names} !\n"
            f"Clique sur *Lancer* quand tout le monde est prêt.\n\n"
            f"🌐 Table en direct : {bj_url}",
            parse_mode="Markdown",
            reply_markup=launch_kb
        )
        ctx.user_data.clear()
        return ConversationHandler.END


def _bj_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🃏 Hit",   callback_data="bj:hit"),
        InlineKeyboardButton("✋ Stand", callback_data="bj:stand"),
    ]])


def _bj_state_text(players: list, dealer_hand: list, game_over: bool = False) -> str:
    """Génère un récap de l'état de la partie pour tous les joueurs."""
    STATUS_ICONS = {"playing": "🎮", "stand": "✋", "bust": "💥", "done": "✅", "waiting": "⏳"}
    RESULT_ICONS = {"win": "🏆", "lose": "💸", "push": "🤝", "blackjack": "🎉", "abandoned": "↩️"}

    lines = ["👥 *État de la partie :*\n"]
    for p in players:
        u = get_user(p["telegram_id"])
        name = u["username"] if u else "?"
        h = json.loads(p["hand"])
        icon = STATUS_ICONS.get(p["status"], "")
        if game_over and p.get("result"):
            res_icon = RESULT_ICONS.get(p["result"], "")
            lines.append(f"{res_icon} *{name}* : {display_hand(h)}")
        else:
            lines.append(f"{icon} *{name}* : {display_hand(h)}")

    lines.append(f"\n🏠 *Croupier* : {display_hand(dealer_hand, hide_second=not game_over)}")
    return "\n".join(lines)


async def _bj_action(send_func, tid: int, action: str, session: dict, bot=None) -> bool:
    """Logique hit/stand partagée solo+multi. Retourne True si tour terminé."""
    players = get_blackjack_players(session["id"])
    player = next((p for p in players if p["telegram_id"] == tid), None)
    if not player or player["status"] != "playing":
        return True

    hand = json.loads(player["hand"])
    deck = json.loads(session["deck"])
    dealer_hand = json.loads(session["dealer_hand"])
    is_multi = len(players) > 1

    if action == "hit":
        card = deck.pop()
        hand.append(card)
        update_blackjack_session(session["id"], deck=json.dumps(deck))
        if hand_value(hand) > 21:
            update_blackjack_player(session["id"], tid, hand=json.dumps(hand), status="bust", result="lose")
            await send_func(
                f"🃏 Ta main : {display_hand(hand)}\n💥 *Bust !* Tu perds {player['bet']} 🪙.",
                parse_mode="Markdown"
            )
            # Pas de broadcast intermédiaire — on continue pour vérifier all_done
        else:
            update_blackjack_player(session["id"], tid, hand=json.dumps(hand))
            await send_func(
                f"🃏 Ta main : {display_hand(hand)}\n"
                f"🏠 Croupier : {display_hand(dealer_hand, hide_second=True)}",
                parse_mode="Markdown",
                reply_markup=_bj_keyboard()
            )
            return False  # Pas encore terminé, pas de message aux autres

    elif action == "stand":
        update_blackjack_player(session["id"], tid, status="stand")
        await send_func("✋ Tu restes.", parse_mode="Markdown")
    else:
        return False

    # Vérifier si tous les joueurs ont terminé leur tour
    players = get_blackjack_players(session["id"])
    all_done = all(p["status"] in ("stand", "bust") for p in players)
    if not all_done:
        return True

    # Dealer joue
    while hand_value(dealer_hand) < 17:
        dealer_hand.append(deck.pop())
    dealer_val = hand_value(dealer_hand)
    update_blackjack_session(session["id"],
        dealer_hand=json.dumps(dealer_hand),
        deck=json.dumps(deck),
        status="finished"
    )

    # Calculer résultats
    for p in players:
        p_hand = json.loads(p["hand"])
        bet = p["bet"]
        if p["status"] == "bust":
            update_blackjack_player(session["id"], p["telegram_id"], result="lose")
        elif dealer_val > 21 or hand_value(p_hand) > dealer_val:
            add_coins(p["telegram_id"], bet * 2, "Blackjack gagné")
            update_blackjack_player(session["id"], p["telegram_id"], result="win")
        elif hand_value(p_hand) == dealer_val:
            add_coins(p["telegram_id"], bet, "Blackjack égalité")
            update_blackjack_player(session["id"], p["telegram_id"], result="push")
        else:
            update_blackjack_player(session["id"], p["telegram_id"], result="lose")

    # Envoyer résultats finaux à chaque joueur
    players = get_blackjack_players(session["id"])
    state_txt = _bj_state_text(players, dealer_hand, game_over=True)

    RESULT_MSGS = {
        "win":  lambda bet: f"🏆 *Tu gagnes !* +{bet} 🪙",
        "lose": lambda bet: f"💸 *Perdu !* -{bet} 🪙",
        "push": lambda bet: "🤝 *Égalité !* Mise remboursée.",
        "bust": lambda bet: f"💥 *Bust !* -{bet} 🪙",
    }

    rematch_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Rejouer", callback_data=f"bj_rematch:{session['id']}")
    ]])

    for p in players:
        result_line = RESULT_MSGS.get(p["result"], lambda b: "")(p["bet"])
        msg = f"{state_txt}\n\n{result_line}"
        is_creator = p["telegram_id"] == session["creator_id"]
        kb = rematch_kb if is_creator else None
        if p["telegram_id"] == tid:
            await send_func(msg, parse_mode="Markdown", reply_markup=kb)
        elif bot:
            try:
                await bot.send_message(chat_id=p["telegram_id"], text=msg,
                                       parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass
    return True


async def handle_bj_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gère les boutons Hit/Stand du blackjack."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    action = query.data.split(":")[1]  # "hit" ou "stand"

    session = get_blackjack_session_by_player(tid)
    if not session or session["status"] != "active":
        await query.answer("❌ Aucune partie en cours.", show_alert=True)
        return

    players = get_blackjack_players(session["id"])
    player = next((p for p in players if p["telegram_id"] == tid), None)
    if not player or player["status"] != "playing":
        await query.answer("C'est pas ton tour.", show_alert=True)
        return

    # Édite le message original pour retirer les boutons
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _bj_action(query.message.reply_text, tid, action, session, ctx.bot)


async def cmd_bj_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback texte pendant la conversation solo (normalement les boutons sont utilisés)."""
    return BJ_PLAYING


async def cmd_bj_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Blackjack annulé.")
    return ConversationHandler.END


# ── /rejoindrebj & /lancerbj ──────────────────────────────────────────────────

async def handle_bj_join_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bouton 'Rejoindre' reçu par un invité."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    _, token, bet_str = query.data.split(":")
    bet = int(bet_str)

    if not get_user(tid):
        await query.answer("❌ Configure ton profil : /p 80 h", show_alert=True)
        return
    session = get_blackjack_session(token)
    if not session or session["status"] != "waiting":
        await query.answer("❌ La partie a déjà commencé ou est introuvable.", show_alert=True)
        return
    existing = get_blackjack_players(session["id"])
    if any(p["telegram_id"] == tid for p in existing):
        await query.answer("Tu as déjà rejoint !", show_alert=True)
        return
    if get_coins(tid) < bet:
        await query.answer(f"❌ Solde insuffisant ({get_coins(tid)} 🪙).", show_alert=True)
        return

    add_blackjack_player(session["id"], tid, bet)
    add_coins(tid, -bet, "Mise blackjack")
    user = get_user(tid)

    # Retirer le bouton pour cet invité
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(f"✅ Tu as rejoint la partie ! Mise : {bet} 🪙")

    # Notifier l'hôte
    try:
        await ctx.bot.send_message(
            chat_id=session["creator_id"],
            text=f"✅ *{user['username']}* a rejoint la partie !",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def handle_bj_launch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bouton 'Lancer' cliqué par l'hôte."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    token = query.data.split(":")[1]

    session = get_blackjack_session(token)
    if not session or session["creator_id"] != tid:
        await query.answer("❌ Réservé à l'hôte.", show_alert=True)
        return
    if session["status"] != "waiting":
        await query.answer("La partie a déjà commencé.", show_alert=True)
        return

    players = get_blackjack_players(session["id"])
    if len(players) < 2:
        await query.answer("❌ Il faut au moins 2 joueurs.", show_alert=True)
        return

    # Retirer le bouton Lancer
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Distribuer les mains
    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    bj_url = f"{site}/blackjack.html?session={token}"

    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]
    hands: dict[int, list] = {}
    for p in players:
        hand = [deck.pop(), deck.pop()]
        hands[p["telegram_id"]] = hand
        update_blackjack_player(session["id"], p["telegram_id"], hand=json.dumps(hand), status="playing")

    update_blackjack_session(session["id"],
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand)
    )

    # Construire l'état global visible par tous
    all_players_up = get_blackjack_players(session["id"])
    state_lines = ["👥 *Mains de tout le monde :*\n"]
    for p in all_players_up:
        u = get_user(p["telegram_id"])
        name = u["username"] if u else "?"
        h = json.loads(p["hand"])
        state_lines.append(f"🎮 *{name}* : {display_hand(h)}")
    state_lines.append(f"\n🏠 *Croupier* : {display_hand(dealer_hand, hide_second=True)}")
    state_txt = "\n".join(state_lines)

    for p in players:
        hand = hands[p["telegram_id"]]
        msg = (
            f"🃏 *La partie commence !*\n\n"
            f"🎴 Ta main : {display_hand(hand)}\n\n"
            f"{state_txt}\n\n"
            f"🌐 Table en direct : {bj_url}"
        )
        if is_blackjack(hand):
            bet = p["bet"]
            winnings = int(bet * 1.5)
            add_coins(p["telegram_id"], bet + winnings, "Blackjack ! (×1.5)")
            update_blackjack_player(session["id"], p["telegram_id"], status="done", result="blackjack")
            msg += f"\n\n🎉 *BLACKJACK !* Tu gagnes {winnings} 🪙 !"
            try:
                await ctx.bot.send_message(chat_id=p["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception:
                pass
        else:
            try:
                await ctx.bot.send_message(
                    chat_id=p["telegram_id"],
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=_bj_keyboard()
                )
            except Exception:
                pass


async def handle_bj_rematch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🔄 Rejouer avec les mêmes joueurs et mises."""
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id

    old_session_id = int(query.data.split(":")[1])
    old_session = get_blackjack_session_by_id(old_session_id)
    if not old_session:
        await query.answer("❌ Partie introuvable.", show_alert=True)
        return
    if old_session["creator_id"] != tid:
        await query.answer("❌ Seul le créateur peut relancer la partie.", show_alert=True)
        return

    old_players = get_blackjack_players(old_session_id)
    if not old_players:
        await query.answer("❌ Aucun joueur trouvé.", show_alert=True)
        return

    # Vérifier les soldes
    broke = []
    for p in old_players:
        if get_coins(p["telegram_id"]) < p["bet"]:
            u = get_user(p["telegram_id"])
            broke.append(u["username"] if u else str(p["telegram_id"]))
    if broke:
        await query.message.reply_text(
            f"❌ Solde insuffisant pour : *{', '.join(broke)}*\n"
            "Certains joueurs ne peuvent pas rejouer avec la même mise.",
            parse_mode="Markdown"
        )
        return

    # Créer une nouvelle session
    token = secrets.token_urlsafe(8)
    session_id = create_blackjack_session(tid, token)

    # Distribuer les mains
    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]

    for p in old_players:
        hand = [deck.pop(), deck.pop()]
        add_blackjack_player(session_id, p["telegram_id"], p["bet"])
        add_coins(p["telegram_id"], -p["bet"], "Mise blackjack (revanche)")
        update_blackjack_player(session_id, p["telegram_id"], hand=json.dumps(hand), status="playing")

    update_blackjack_session(session_id,
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand)
    )

    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    bj_url = f"{site}/blackjack.html?session={token}"

    # Envoyer mains à tous les joueurs
    new_players = get_blackjack_players(session_id)
    for p in new_players:
        hand = json.loads(p["hand"])
        msg = (
            f"🔄 *Revanche !*\n\n"
            f"🎴 Ta main : {display_hand(hand)}\n"
            f"🏠 Croupier : {display_hand(dealer_hand, hide_second=True)}"
        )
        if is_blackjack(hand):
            winnings = int(p["bet"] * 1.5)
            add_coins(p["telegram_id"], p["bet"] + winnings, "Blackjack ! (×1.5)")
            update_blackjack_player(session_id, p["telegram_id"], status="done", result="blackjack")
            msg += f"\n\n🎉 *BLACKJACK !* Tu gagnes {winnings} 🪙 !"
            try:
                await ctx.bot.send_message(chat_id=p["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception:
                pass
        else:
            try:
                await ctx.bot.send_message(
                    chat_id=p["telegram_id"],
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=_bj_keyboard()
                )
            except Exception:
                pass

    await query.message.reply_text(
        f"🔄 *Revanche lancée !* Tout le monde a reçu sa main.\n🌐 {bj_url}",
        parse_mode="Markdown"
    )


async def cmd_rejoindre_bj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not get_user(tid):
        await update.message.reply_text("❌ Configure ton profil : /p 80 h")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage : /rejoindrebj <token> <mise>")
        return
    token, bet_str = ctx.args[0], ctx.args[1]
    session = get_blackjack_session(token)
    if not session or session["status"] != "waiting":
        await update.message.reply_text("❌ Session introuvable ou déjà commencée.")
        return
    try:
        bet = int(bet_str)
        assert 1 <= bet <= get_coins(tid)
    except Exception:
        await update.message.reply_text(f"❌ Mise invalide (solde : {get_coins(tid)} 🪙).")
        return

    existing = get_blackjack_players(session["id"])
    if any(p["telegram_id"] == tid for p in existing):
        await update.message.reply_text("❌ Tu as déjà rejoint cette partie.")
        return

    add_blackjack_player(session["id"], tid, bet)
    add_coins(tid, -bet, "Mise blackjack")
    user = get_user(tid)
    await update.message.reply_text(f"✅ Tu as rejoint la partie ! Mise : {bet} 🪙")
    try:
        await ctx.bot.send_message(
            chat_id=session["creator_id"],
            text=f"✅ *{user['username']}* a rejoint la partie ! (mise : {bet} 🪙)",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def cmd_lancer_bj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id

    session_row = _get_waiting_session_by_creator(tid)
    if not session_row:
        await update.message.reply_text("❌ Aucune partie en attente.")
        return

    players = get_blackjack_players(session_row["id"])
    if len(players) < 2:
        await update.message.reply_text("❌ Il faut au moins 2 joueurs.")
        return

    site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
    bj_url = f"{site}/blackjack.html?session={session_row['token']}"

    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]
    hands: dict[int, list] = {}
    for p in players:
        hand = [deck.pop(), deck.pop()]
        hands[p["telegram_id"]] = hand
        update_blackjack_player(session_row["id"], p["telegram_id"],
            hand=json.dumps(hand), status="playing")

    update_blackjack_session(session_row["id"],
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand)
    )

    # État global
    all_players_up = get_blackjack_players(session_row["id"])
    state_lines = ["👥 *Mains de tout le monde :*\n"]
    for p in all_players_up:
        u = get_user(p["telegram_id"])
        name = u["username"] if u else "?"
        h = json.loads(p["hand"])
        state_lines.append(f"🎮 *{name}* : {display_hand(h)}")
    state_lines.append(f"\n🏠 *Croupier* : {display_hand(dealer_hand, hide_second=True)}")
    state_txt = "\n".join(state_lines)

    for p in players:
        hand = hands[p["telegram_id"]]
        msg = (
            f"🃏 *La partie commence !*\n\n"
            f"🎴 Ta main : {display_hand(hand)}\n\n"
            f"{state_txt}\n\n"
            f"🌐 Table en direct : {bj_url}"
        )
        try:
            await ctx.bot.send_message(
                chat_id=p["telegram_id"],
                text=msg,
                parse_mode="Markdown",
                reply_markup=_bj_keyboard()
            )
        except Exception:
            pass

    await update.message.reply_text("✅ Partie lancée ! Tout le monde a reçu sa main.")


# ── App ───────────────────────────────────────────────────────────────────────

def create_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).updater(None).build()

    app.add_handler(CommandHandler("start",                       cmd_start))
    app.add_handler(CommandHandler("topo",                        cmd_topo))
    app.add_handler(CommandHandler(["profil", "p"],               cmd_profil))
    app.add_handler(CommandHandler(["tac", "t"],                  cmd_tac))
    app.add_handler(CommandHandler(["historique", "h", "histo"],  cmd_historique))
    app.add_handler(CommandHandler(["annuler", "a"],              cmd_annuler))
    app.add_handler(CommandHandler(["defi", "classement"],        cmd_defi))
    app.add_handler(CommandHandler(["stop", "reset", "r"],        cmd_stop))
    app.add_handler(CommandHandler("site",                        cmd_site))
    app.add_handler(CommandHandler(["ou", "where"],               cmd_ou))
    app.add_handler(CommandHandler("invite",                      cmd_invite))
    app.add_handler(CommandHandler("ban",                         cmd_ban))
    app.add_handler(CommandHandler("unban",                       cmd_unban))
    app.add_handler(CommandHandler("rename",                      cmd_rename))
    app.add_handler(CommandHandler("notif",                       cmd_notif))
    app.add_handler(CommandHandler("notifmaj",                    cmd_notifmaj))
    app.add_handler(CommandHandler("add",                         cmd_addverre))
    app.add_handler(CommandHandler("del",                         cmd_delverre))
    app.add_handler(CommandHandler("recap",                       cmd_recap))
    app.add_handler(CommandHandler(["liste", "l"],                lambda u, c: u.message.reply_text(list_drinks_text(), parse_mode="Markdown")))

    registered = set()
    for key, drink in DRINKS.items():
        for alias in drink.aliases:
            cmd = alias.lower().replace("é", "e").replace("è", "e")
            if cmd not in registered and cmd.replace("9", "").replace("6", "").isalpha():
                app.add_handler(CommandHandler(cmd, lambda u, c, k=key: _do_drink(u, c, k)))
                registered.add(cmd)

    # ── ConversationHandlers (must come BEFORE generic text handler) ──────────
    pari_conv = ConversationHandler(
        entry_points=[
            CommandHandler("pari", cmd_pari_start),
            MessageHandler(filters.Regex(r'(?i)^pari$'), cmd_pari_start),
        ],
        states={
            BET_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_pari_type)],
            BET_OPPONENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_pari_opponent)],
            BET_TIME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_pari_time)],
            BET_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_pari_amount)],
        },
        fallbacks=[CommandHandler("annuler", cmd_pari_cancel)],
        per_user=True,
    )

    bj_conv = ConversationHandler(
        entry_points=[CommandHandler("blackjack", cmd_bj_start)],
        states={
            BJ_MODE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_bj_mode)],
            BJ_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_bj_players)],
            BJ_BET:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_bj_bet)],
            BJ_PLAYING: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_bj_play)],
        },
        fallbacks=[
            CommandHandler("annuler", cmd_bj_cancel),
            CommandHandler("blackjack", cmd_bj_start),
        ],
        allow_reentry=True,
        per_user=True,
    )

    app.add_handler(pari_conv)
    app.add_handler(bj_conv)

    # ── New commands ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("solde",       cmd_solde))
    app.add_handler(CommandHandler("offrir",      cmd_offrir))
    app.add_handler(CommandHandler("accepter",    cmd_accepter))
    app.add_handler(CommandHandler("refuser",     cmd_refuser))
    app.add_handler(CommandHandler("rejoindrebj", cmd_rejoindre_bj))
    app.add_handler(CommandHandler("lancerbj",    cmd_lancer_bj))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(handle_drink_callback,      pattern="^drink:"))
    app.add_handler(CallbackQueryHandler(handle_bj_callback,         pattern="^bj:"))
    app.add_handler(CallbackQueryHandler(handle_bj_join_callback,    pattern="^bj_join:"))
    app.add_handler(CallbackQueryHandler(handle_bj_launch_callback,  pattern="^bj_launch:"))
    app.add_handler(CallbackQueryHandler(handle_bj_rematch_callback, pattern="^bj_rematch:"))
    return app


if __name__ == "__main__":
    init_db()
    app = create_application()
    app.run_polling()
