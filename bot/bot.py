import os
import logging
from dotenv import load_dotenv
import httpx
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application
from core.drinks import DRINKS, find_drink, list_drinks_text
from core.widmark import alcohol_grams, total_bac, bac_label, sober_in_hours
from data.database import (
    init_db, upsert_user, get_user,
    start_session, get_active_session, log_drink, get_session_drinks,
    end_session
)

load_dotenv()
logging.basicConfig(level=logging.INFO)


def ensure_session(telegram_id: int):
    """Crée une session si l'utilisateur n'en a pas."""
    if not get_active_session(telegram_id):
        start_session(telegram_id)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍺 *AlcooTracker*\n\n"
        "Pour commencer, configure ton profil une seule fois :\n"
        "`/profil 80 homme` ou `/profil 60 femme`\n\n"
        "Ensuite envoie directement `/pinte`, `/demi`, `/vodka`...\n"
        "Tape `/liste` pour voir toutes les boissons.",
        parse_mode="Markdown"
    )


# ── /profil ───────────────────────────────────────────────────────────────────

async def cmd_profil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args

    if len(args) != 2:
        await update.message.reply_text(
            "❌ Usage : `/profil 80 homme` ou `/profil 60 femme`",
            parse_mode="Markdown"
        )
        return

    try:
        weight = float(args[0].replace(",", "."))
        gender = args[1].lower()
        assert gender in ("homme", "femme")
        assert 30 < weight < 250
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ Exemple valide : `/profil 80 homme` ou `/profil 60 femme`",
            parse_mode="Markdown"
        )
        return

    name = user.first_name or user.username or str(user.id)
    upsert_user(user.id, name, weight, gender)
    ensure_session(user.id)

    await update.message.reply_text(
        f"✅ Profil enregistré, *{name}* !\n"
        f"{weight} kg — {gender}\n\n"
        f"Tu peux maintenant envoyer `/pinte`, `/demi`, `/vodka`...",
        parse_mode="Markdown"
    )


# ── Commandes boissons dynamiques ─────────────────────────────────────────────

def make_drink_handler(drink_key: str):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        tid = update.effective_user.id
        user_data = get_user(tid)

        if not user_data:
            await update.message.reply_text(
                "❌ Configure ton profil d'abord : `/profil 80 homme`",
                parse_mode="Markdown"
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

    return handler


# ── /tac ──────────────────────────────────────────────────────────────────────

async def cmd_tac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil : `/profil 80 homme`", parse_mode="Markdown")
        return
    drinks_data = get_session_drinks(tid)
    if not drinks_data:
        await update.message.reply_text("🫗 Aucune boisson encore. Envoie `/pinte`, `/demi`...", parse_mode="Markdown")
        return
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    await update.message.reply_text(
        f"🧪 *{bac:.2f} g/L* — {bac_label(bac)}\n"
        f"⏱ Sobre dans ~*{sober_in_hours(bac):.1f}h*\n"
        f"_{len(drinks_data)} consommation(s)_",
        parse_mode="Markdown"
    )


# ── /stop ─────────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    end_session(update.effective_user.id)
    ensure_session(update.effective_user.id)  # recrée une session vide immédiatement
    await update.message.reply_text("✅ Compteurs remis à zéro !")


# ── /liste ────────────────────────────────────────────────────────────────────

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(list_drinks_text(), parse_mode="Markdown")


# ── Création de l'app ─────────────────────────────────────────────────────────

def create_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("profil", cmd_profil))
    app.add_handler(CommandHandler("tac",    cmd_tac))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("liste",  cmd_liste))

    # Une commande par boisson (premier alias de chaque)
    registered = set()
    for key, drink in DRINKS.items():
        for alias in drink.aliases:
            cmd = alias.lower().replace("é", "e").replace("è", "e")
            if cmd not in registered and cmd.isalnum():
                app.add_handler(CommandHandler(cmd, make_drink_handler(key)))
                registered.add(cmd)

    return app


if __name__ == "__main__":
    init_db()
    app = create_application()
    app.run_polling()
