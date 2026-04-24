import os
import logging
from datetime import datetime
from dotenv import load_dotenv
import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, Application, filters
)
from core.drinks import DRINKS, list_drinks_text
from core.widmark import alcohol_grams, total_bac, bac_label, sober_in_hours
from data.database import (
    init_db, upsert_user, get_user,
    start_session, get_active_session, log_drink, get_session_drinks,
    get_session_drinks_detail, end_session
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

# Index alias → drink_key (pour lookup rapide)
ALIAS_MAP: dict[str, str] = {}
for key, drink in DRINKS.items():
    for alias in drink.aliases:
        ALIAS_MAP[alias.lower().replace("é", "e").replace("è", "e")] = key


def ensure_session(telegram_id: int):
    if not get_active_session(telegram_id):
        start_session(telegram_id)


async def _do_drink(update: Update, drink_key: str):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text(
            "❌ Configure ton profil d'abord : /profil 80 homme"
        )
        return

    ensure_session(tid)
    drink = DRINKS[drink_key]
    alc_g = alcohol_grams(drink.volume_ml, drink.abv)
    log_drink(tid, drink_key, alc_g)

    drinks_data = get_session_drinks(tid)
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])

    await update.message.reply_text(
        f"✅ *{drink.name}*\n\n"
        f"🧪 TAC : *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre dans ~*{sober_in_hours(bac):.1f}h*",
        parse_mode="Markdown"
    )

    api_url = os.environ.get("API_URL", "http://localhost:8000")
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
        "`/profil 80 homme` ou `/profil 60 femme`\n\n"
        "Ensuite envoie simplement le nom de ta boisson :\n"
        "`pinte`, `demi`, `vodka`, `vin`...\n\n"
        "Tape `liste` pour voir toutes les boissons.",
        parse_mode="Markdown"
    )


# ── /profil ───────────────────────────────────────────────────────────────────

async def cmd_profil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if len(args) != 2:
        await update.message.reply_text("❌ Usage : /profil 80 homme")
        return
    try:
        weight = float(args[0].replace(",", "."))
        gender = args[1].lower()
        gender = {"h": "homme", "f": "femme"}.get(gender, gender)
        assert gender in ("homme", "femme")
        assert 30 < weight < 250
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Exemple : /profil 80 homme ou /profil 60 femme")
        return

    name = user.first_name or user.username or str(user.id)
    upsert_user(user.id, name, weight, gender)
    ensure_session(user.id)
    await update.message.reply_text(
        f"✅ Profil enregistré, *{name}* ! ({weight} kg — {gender})\n\n"
        f"Envoie maintenant `pinte`, `demi`, `vodka`...",
        parse_mode="Markdown"
    )


# ── /tac ──────────────────────────────────────────────────────────────────────

async def cmd_tac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil : /profil 80 homme")
        return
    drinks_data = get_session_drinks(tid)
    if not drinks_data:
        await update.message.reply_text("🫗 Aucune boisson encore. Envoie `pinte`, `demi`...", parse_mode="Markdown")
        return
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    await update.message.reply_text(
        f"🧪 *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre dans ~*{sober_in_hours(bac):.1f}h*\n"
        f"_{len(drinks_data)} consommation(s)_",
        parse_mode="Markdown"
    )


# ── /stop ─────────────────────────────────────────────────────────────────────

async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    rows = get_session_drinks_detail(tid)
    if not rows:
        await update.message.reply_text("🫗 Aucune boisson cette session.")
        return
    lines = ["📋 *Tes boissons cette session :*\n"]
    for i, r in enumerate(rows, 1):
        heure = datetime.fromisoformat(r["logged_at"]).strftime("%H:%M")
        drink = DRINKS.get(r["drink_key"])
        nom = drink.name if drink else r["drink_key"]
        lines.append(f"{i}. {nom} — {heure}")
    lines.append(f"\n_Total : {len(rows)} verre(s)_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    end_session(update.effective_user.id)
    ensure_session(update.effective_user.id)
    await update.message.reply_text("✅ Compteurs remis à zéro !")


# ── Handler texte libre (pinte, demi, vodka...) ───────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    text = text.replace("é", "e").replace("è", "e").replace("à", "a")

    # Commandes texte spéciales
    if text in ("liste", "list", "l"):
        await update.message.reply_text(list_drinks_text(), parse_mode="Markdown")
        return
    if text in ("tac", "mon tac", "t"):
        await cmd_tac(update, ctx)
        return
    if text in ("stop", "reset", "r"):
        await cmd_stop(update, ctx)
        return
    if text in ("historique", "histo", "h"):
        await cmd_historique(update, ctx)
        return

    drink_key = ALIAS_MAP.get(text)
    if drink_key:
        await _do_drink(update, drink_key)
    # On ignore silencieusement les messages inconnus


# ── Création de l'app ─────────────────────────────────────────────────────────

def create_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).updater(None).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler(["profil", "p"],          cmd_profil))
    app.add_handler(CommandHandler(["historique", "h", "histo"], cmd_historique))
    app.add_handler(CommandHandler(["tac", "t"],    cmd_tac))
    app.add_handler(CommandHandler(["stop", "reset", "r"], cmd_stop))
    app.add_handler(CommandHandler(["liste", "l"],  lambda u, c: u.message.reply_text(list_drinks_text(), parse_mode="Markdown")))

    # Commandes slash par boisson
    registered = set()
    for key, drink in DRINKS.items():
        for alias in drink.aliases:
            cmd = alias.lower().replace("é", "e").replace("è", "e")
            if cmd not in registered and cmd.replace("3", "").isalpha():
                app.add_handler(CommandHandler(cmd, lambda u, c, k=key: _do_drink(u, k)))
                registered.add(cmd)

    # Handler texte libre (sans /)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


if __name__ == "__main__":
    init_db()
    app = create_application()
    app.run_polling()
