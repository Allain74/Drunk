import os
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, Application, filters
)
from core.drinks import DRINKS, list_drinks_text
from core.widmark import alcohol_grams, total_bac, bac_label, sober_in_hours
from data.database import (
    init_db, upsert_user, get_user, get_user_by_username, get_all_users,
    start_session, get_active_session, log_drink, get_session_drinks,
    get_session_drinks_detail, delete_last_drink, end_session, update_location,
    is_banned, ban_user, unban_user, rename_user
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

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
    """Envoie un message à tous les utilisateurs sauf l'expéditeur."""
    for user in get_all_users():
        if user["telegram_id"] != sender_id:
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


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍺 *AlcooTracker*\n\n"
        "Configure ton profil une seule fois :\n"
        "`/p 80 h` ou `/p 60 f`\n\n"
        "Puis envoie le nom de ta boisson directement :\n"
        "`pinte`, `demi`, `vodka`, `vin`...\n\n"
        "Tape `/` pour voir toutes les commandes.",
        parse_mode="Markdown"
    )


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
    alc_g = alcohol_grams(drink.volume_ml, drink.abv)
    log_drink(tid, drink_key, alc_g)

    drinks_data = get_session_drinks(tid)
    nb = len(drinks_data)
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    prev_bac = total_bac(drinks_data[:-1], user_data["weight_kg"], user_data["gender"]) if nb > 1 else 0.0

    text = (
        f"✅ *{drink.name}*\n\n"
        f"🧪 TAC : *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre {sober_time_str(bac)}"
        f"{fun_message(bac)}"
    )

    # Rappel eau toutes les 3 boissons
    if nb > 0 and nb % 3 == 0:
        text += f"\n\n💧 *{nb} verres — pense à boire de l'eau !*"

    # Bouton position
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Partager ma position", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    # Notif premier verre de la session
    if nb == 1:
        site = os.environ.get("SITE_URL", "https://drunk-weld.vercel.app")
        await _notify_all(ctx, tid, f"🍺 *{user_data['username']}* commence à boire ! Rejoins-le !\n{site}")

    # Notif si quelqu'un passe 0.8 g/L pour la première fois (franchissement)
    if prev_bac < 0.8 <= bac:
        name = user_data["username"]
        await _notify_all(ctx, tid, f"⚠️ *{name}* vient de dépasser la limite légale ({bac:.2f} g/L) 🚨")

    await _refresh_api()


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
    """Usage : /rename <ancien> <nouveau>"""
    if not _is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage : /rename <ancien_prénom> <nouveau_prénom>")
        return
    target = get_user_by_username(ctx.args[0])
    if not target:
        await update.message.reply_text(f"❌ Utilisateur '{ctx.args[0]}' introuvable.")
        return
    new_name = ctx.args[1]
    rename_user(target["telegram_id"], new_name)
    await update.message.reply_text(f"✅ Renommé : *{target['username']}* → *{new_name}*", parse_mode="Markdown")
    await _refresh_api()


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

    drink_key = ALIAS_MAP.get(text)
    if drink_key:
        await _do_drink(update, ctx, drink_key)


# ── App ───────────────────────────────────────────────────────────────────────

def create_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).updater(None).build()

    app.add_handler(CommandHandler("start",                       cmd_start))
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
    app.add_handler(CommandHandler(["liste", "l"],                lambda u, c: u.message.reply_text(list_drinks_text(), parse_mode="Markdown")))

    registered = set()
    for key, drink in DRINKS.items():
        for alias in drink.aliases:
            cmd = alias.lower().replace("é", "e").replace("è", "e")
            if cmd not in registered and cmd.replace("9", "").replace("6", "").isalpha():
                app.add_handler(CommandHandler(cmd, lambda u, c, k=key: _do_drink(u, c, k)))
                registered.add(cmd)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    return app


if __name__ == "__main__":
    init_db()
    app = create_application()
    app.run_polling()
