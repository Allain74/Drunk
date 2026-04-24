import os
import logging
from dotenv import load_dotenv
import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, Application
)
from core.drinks import find_drink, list_drinks_text
from core.widmark import alcohol_grams, total_bac, bac_label, sober_in_hours
from data.database import (
    init_db, upsert_user, get_user,
    start_session, end_session, log_drink, get_session_drinks
)

load_dotenv()
logging.basicConfig(level=logging.INFO)


# ── Commandes ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍺 *AlcooTracker*\n\n"
        "Commandes disponibles :\n"
        "• `/profil 80 homme` — configurer ton profil\n"
        "• `/session` — démarrer une session de boisson\n"
        "• `/boire demi` — loguer une boisson\n"
        "• `/tac` — voir ton taux actuel\n"
        "• `/stop` — terminer ta session\n"
        "• `/liste` — voir toutes les boissons\n",
        parse_mode="Markdown"
    )


async def cmd_profil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if len(args) != 2:
        await update.message.reply_text("❌ Usage : `/profil 75 homme`", parse_mode="Markdown")
        return
    try:
        weight = float(args[0])
        gender = args[1].lower()
        assert gender in ("homme", "femme")
        assert 30 < weight < 250
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Exemple : `/profil 75 homme` ou `/profil 60 femme`", parse_mode="Markdown")
        return
    upsert_user(user.id, user.first_name or user.username or str(user.id), weight, gender)
    await update.message.reply_text(f"✅ Profil enregistré : {weight} kg, {gender}.")


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_data = get_user(update.effective_user.id)
    if not user_data:
        await update.message.reply_text("❌ Configure d'abord ton profil avec `/profil`.", parse_mode="Markdown")
        return
    start_session(update.effective_user.id)
    await update.message.reply_text("🍻 Session démarrée ! Envoie `/boire <boisson>` pour loguer.")


async def cmd_boire(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil avec `/profil` d'abord.", parse_mode="Markdown")
        return
    if not ctx.args:
        await update.message.reply_text("❌ Usage : `/boire demi`\nVoir `/liste` pour les options.", parse_mode="Markdown")
        return

    drink = find_drink(" ".join(ctx.args))
    if not drink:
        await update.message.reply_text(
            f"❓ Boisson inconnue. Tape `/liste` pour voir les options.", parse_mode="Markdown"
        )
        return

    alc_g = alcohol_grams(drink.volume_ml, drink.abv)
    if not log_drink(tid, drink.aliases[0], alc_g):
        await update.message.reply_text("❌ Pas de session active. Lance `/session` d'abord.", parse_mode="Markdown")
        return

    drinks_data = get_session_drinks(tid)
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    label = bac_label(bac)
    sober = sober_in_hours(bac)

    await update.message.reply_text(
        f"✅ *{drink.name}* enregistrée ({alc_g:.1f}g d'alcool)\n\n"
        f"🧪 TAC actuel : *{bac:.2f} g/L*\n"
        f"{label}\n"
        f"⏱ Sobre dans environ *{sober:.1f}h*",
        parse_mode="Markdown"
    )

    api_url = os.environ.get("API_URL", "http://localhost:8000")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{api_url}/refresh", timeout=3)
    except Exception:
        pass


async def cmd_tac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_data = get_user(tid)
    if not user_data:
        await update.message.reply_text("❌ Configure ton profil avec `/profil`.", parse_mode="Markdown")
        return
    drinks_data = get_session_drinks(tid)
    if not drinks_data:
        await update.message.reply_text("🫗 Aucune boisson dans ta session.")
        return
    bac = total_bac(drinks_data, user_data["weight_kg"], user_data["gender"])
    await update.message.reply_text(
        f"🧪 Ton TAC : *{bac:.2f} g/L*\n"
        f"{bac_label(bac)}\n"
        f"⏱ Sobre dans environ *{sober_in_hours(bac):.1f}h*\n"
        f"_(basé sur {len(drinks_data)} consommation(s))_",
        parse_mode="Markdown"
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    end_session(update.effective_user.id)
    await update.message.reply_text("✅ Session terminée. Rentre bien !")


async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(list_drinks_text(), parse_mode="Markdown")


# ── Exposition pour FastAPI lifespan ──────────────────────────────────────────

def create_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("profil",  cmd_profil))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("boire",   cmd_boire))
    app.add_handler(CommandHandler("tac",     cmd_tac))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("liste",   cmd_liste))
    return app


if __name__ == "__main__":
    init_db()
    app = create_application()
    app.run_polling()
