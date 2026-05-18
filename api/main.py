import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton

from data.database import (
    log_vomi, count_vomis_session, count_vomis_total,
    award_soiree_badge_if_eligible, get_soiree_badges_counts,
    _execute, _fetchall, _fetchone,
    init_db, get_all_users, get_all_active_drinks, get_active_session,
    get_drinks_by_session, get_all_time_stats, get_last_drink_time,
    set_last_inactivity_notif, get_last_inactivity_notif,
    get_active_bets, settle_bet, get_bet, get_user, add_coins, get_coins, try_debit_coins,
    create_bet, get_pending_bet_for, accept_bet, cancel_bet,
    get_user_bets, get_all_balances, get_transactions,
    get_blackjack_session, get_blackjack_session_by_id, get_blackjack_session_by_player,
    get_active_blackjack_sessions, get_blackjack_players, update_blackjack_session,
    update_blackjack_player, create_blackjack_session, add_blackjack_player,
    follow_user, unfollow_user, is_following, get_following, get_followers,
    ban_user, unban_user, is_banned,
    verify_password,
    log_drink as db_log_drink, start_session, end_session,
    upsert_user, set_password, get_user_by_username, rename_user,
    delete_last_drink, update_max_bac, is_username_taken,
    get_session_drinks, get_session_drinks_detail, delete_user, update_location,
    init_push_subscriptions, save_push_subscription,
    delete_push_subscription, get_push_subscriptions,
    get_blackjack_stats, get_profile_follows,
    set_avatar, get_all_avatars,
    get_all_follows,
    clear_password, delete_n_drinks,
    set_premium, clear_premium, get_user_by_stripe_customer, set_stripe_customer_id,
    create_auth_session, get_uid_from_token, revoke_auth_token, revoke_all_user_sessions,
    add_xp as db_add_xp, get_xp, unlock_badge, get_user_badges,
    get_streak, bump_streak, set_referrer, count_referrals,
)
from core.gamification import (
    calc_level, all_badges_meta, BADGES,
    XP_PER_DRINK, XP_PER_BET_WIN, XP_PER_BJ_WIN, XP_REFERRAL,
)
from core.recap import build_weekly_recap, build_weekly_recap_for_user
from core.widmark import total_bac, bac_label, sober_in_hours, alcohol_grams
from core.blackjack import new_deck, hand_value, display_hand, is_blackjack
from core.drinks import DRINKS

load_dotenv()

# ── Stripe (abonnement premium) ───────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "")

# ── Web Push (VAPID) ──────────────────────────────────────────────────────────
try:
    from pywebpush import webpush, WebPushException
    _PUSH_ENABLED = True
except ImportError:
    _PUSH_ENABLED = False

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": "mailto:admin@drunk.app"}

# Threadpool dédié pour les push notifications (initialisé dans lifespan).
# Isolé du pool principal pour qu'un burst de pushs ne bloque pas les
# requêtes HTTP utilisateurs.
_PUSH_EXECUTOR = None

# Sémaphore pour limiter le nombre de tâches background concurrentes des
# actions user (log-drink, undo, reset). Sans limite, 10 users qui boivent
# simultanément créent 10 tâches × 10 calls Turso = saturation event loop.
# Avec 4 max : les autres attendent en queue mais l'event loop reste libre
# pour servir les requêtes HTTP des autres users.
_BG_TASK_SEM = asyncio.Semaphore(4)


async def _run_bg_limited(coro):
    """Wrap une coroutine background dans le semaphore pour limiter
    le nombre de tâches concurrentes (4 max)."""
    async with _BG_TASK_SEM:
        await coro


def _fire_push(user_id: int, title: str, body: str, url: str = "/"):
    """Envoie un push notif sans bloquer l'event loop. Utilise le threadpool
    dédié _PUSH_EXECUTOR si dispo, sinon fallback sur le default executor."""
    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(_PUSH_EXECUTOR, _send_push, user_id, title, body, url)
    except Exception as e:
        print(f"[fire_push] erreur : {e}")


def _send_push(user_id: int, title: str, body: str, url: str = "/"):
    """Envoie une notification push à tous les appareils d'un utilisateur."""
    if not _PUSH_ENABLED or not VAPID_PRIVATE_KEY:
        print(f"[PUSH] désactivé — PUSH_ENABLED={_PUSH_ENABLED} KEY={'oui' if VAPID_PRIVATE_KEY else 'non'}")
        return
    subs = get_push_subscriptions(user_id)
    print(f"[PUSH] envoi à {user_id} — {len(subs)} subscription(s)")
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
        loop.run_in_executor(_PUSH_EXECUTOR, _send_push, fid, title, body, url)

# ── Notifs Telegram : mettre à False pour ne pas spammer par Telegram ─────────
_TG_NOTIFS = False

_ws_clients: set[WebSocket] = set()
from core.bj_broadcast import _bj_clients          # dict partagé avec bot.py
_bot_app = None
_danger_notified: dict[int, datetime] = {}
# (drinker_id, follower_id) → dernière notif "ami ivre" envoyée
_drunk_follower_notified: dict[tuple, datetime] = {}
# (drinker_id, follower_id) → dernière notif "premier verre" envoyée (fenêtre 8h)
_first_drink_notified: dict[tuple, datetime] = {}

# user_id → task asyncio des notifs FOMO en attente (30s après log-drink).
# Annulé si undo-drink dans les 30s.
_pending_first_drink_notifs: dict[int, asyncio.Task] = {}
# user_id → timestamp UTC du dernier verre (cooldown 30s en mémoire, évite
# une query SELECT MAX(logged_at) sur le chemin critique).
_last_drink_at: dict[int, datetime] = {}
# user_id → liste des verres de la session courante [(alc_grams, datetime), ...].
# Permet de calculer le BAC sans aucune query DB sur le chemin critique du
# /log-drink. Mis à jour au log-drink (append) et au undo-drink (pop last).
# Invalidé sur reset-session. Lazy-load depuis DB au premier log d'un user.
_user_drinks_cache: dict[int, list[tuple[float, datetime]]] = {}
_last_weekly_recap_date: str = ""  # "YYYY-MM-DD" du dernier lundi envoyé
PARIS = ZoneInfo("Europe/Paris")

RENDER_URL = os.environ.get("RENDER_URL", "https://drunk-l34t.onrender.com")


# ── Watchdog : détecte un event loop bloqué et force restart ──────────────
# Comment ça marche : un thread Python (indépendant de l'event loop asyncio)
# update un timestamp toutes les 5s. Un autre thread vérifie l'écart et si
# >90s sans update → l'event loop est figé → os._exit() pour forcer Render
# à redémarrer l'instance. Plus rapide que d'attendre que Render détecte.
import threading
_loop_heartbeat = {"ts": 0.0}

# ── Monitoring : snapshot d'état toutes les 60s ───────────────────────────
# Pour pouvoir diagnostiquer un freeze au prochain down. Garde 1h d'historique
# en mémoire (60 snapshots) accessible via /admin/diag.
_diag_snapshots: list[dict] = []
_DIAG_MAX = 60


def _take_diag_snapshot() -> dict:
    """Capture l'état actuel du serveur : RAM, asyncio tasks, threads,
    file descriptors, taille des dicts mémoire. Léger (~quelques ms)."""
    import resource as _resource
    import os as _os
    snap = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        ru = _resource.getrusage(_resource.RUSAGE_SELF)
        # Linux retourne ru_maxrss en KB, macOS en bytes
        snap["ram_mb"] = round(ru.ru_maxrss / 1024, 1)
    except Exception:
        snap["ram_mb"] = -1
    try:
        snap["asyncio_tasks"] = len(asyncio.all_tasks())
    except Exception:
        snap["asyncio_tasks"] = -1
    try:
        snap["threads"] = threading.active_count()
    except Exception:
        snap["threads"] = -1
    try:
        snap["fds"] = len(_os.listdir(f"/proc/{_os.getpid()}/fd"))
    except Exception:
        snap["fds"] = -1
    # Dicts mémoire de l'app (souvent suspects pour les leaks)
    try:
        snap["ws_clients"] = len(_ws_clients)
        snap["bj_clients"] = sum(len(s) for s in _bj_clients.values()) if _bj_clients else 0
        snap["pending_fomo"] = len(_pending_first_drink_notifs)
        snap["user_drinks_cache"] = len(_user_drinks_cache)
        snap["auth_tokens"] = len(_auth_token_cache)
        snap["endpoint_cache"] = len(_endpoint_cache)
        snap["first_drink_notified"] = len(_first_drink_notified)
        snap["drunk_follower_notified"] = len(_drunk_follower_notified)
        snap["danger_notified"] = len(_danger_notified)
        snap["banned_set"] = len(_banned_user_ids)
        snap["last_drink_at_dict"] = len(_last_drink_at)
    except Exception:
        pass
    return snap


async def _diag_loop():
    """Coroutine qui prend un snapshot toutes les 60s et le logge.
    Déclenche aussi un gc.collect() périodique pour combattre le memory leak
    progressif identifié dans les logs (RAM monte de ~12 MB/h)."""
    import gc as _gc
    import os as _os
    iteration = 0
    while True:
        await asyncio.sleep(60)
        iteration += 1
        try:
            snap = _take_diag_snapshot()
            _diag_snapshots.append(snap)
            if len(_diag_snapshots) > _DIAG_MAX:
                _diag_snapshots.pop(0)
            # Log compact pour pouvoir grep facilement dans Render logs
            short = (
                f"ram={snap.get('ram_mb')}MB tasks={snap.get('asyncio_tasks')} "
                f"thr={snap.get('threads')} fds={snap.get('fds')} "
                f"ws={snap.get('ws_clients')} cache={snap.get('endpoint_cache')} "
                f"pending={snap.get('pending_fomo')} "
                f"first_notif={snap.get('first_drink_notified')} "
                f"drunk_notif={snap.get('drunk_follower_notified')}"
            )
            print(f"[DIAG] {short}")

            ram = snap.get("ram_mb", 0)
            # gc.collect() toutes les 5 min pour libérer les objets cycliques
            # que le ref counting ne libère pas tout seul.
            if iteration % 5 == 0:
                collected = _gc.collect()
                snap_after = _take_diag_snapshot()
                ram_after = snap_after.get("ram_mb", 0)
                freed = ram - ram_after
                if freed > 1:
                    print(f"[GC] collected={collected} objects, freed={freed:.1f}MB")

            # Si RAM > 400 MB, force gc + log alerte. Si > 450 MB, restart
            # propre AVANT que le kernel OOM kill (qui perd les tasks en cours).
            if ram > 450:
                print(f"[MEMORY] ALERTE CRITIQUE {ram}MB > 450MB, force restart propre")
                # Petit délai pour que le log soit envoyé
                await asyncio.sleep(2)
                _os._exit(1)
            elif ram > 400:
                print(f"[MEMORY] ALERTE {ram}MB > 400MB, gc.collect() force")
                _gc.collect()
                # Log les stats GC pour identifier ce qui s'accumule
                stats = _gc.get_stats()
                print(f"[MEMORY] gc stats: {stats}")
        except Exception as e:
            print(f"[DIAG] erreur snapshot : {e}")


async def _heartbeat_async_loop():
    """Coroutine qui met à jour _loop_heartbeat toutes les 5s.
    Si l'event loop est bloqué, cette coroutine ne progresse plus."""
    while True:
        _loop_heartbeat["ts"] = time.time()
        await asyncio.sleep(5)


def _watchdog_thread():
    """Thread système (indépendant d'asyncio) qui surveille le heartbeat.
    Si l'event loop n'a pas pulsé depuis >90s, dump l'état complet PUIS
    force kill du process."""
    import os as _os
    consecutive_dead = 0
    while True:
        time.sleep(30)
        last = _loop_heartbeat["ts"]
        if last == 0:
            continue  # pas encore démarré
        elapsed = time.time() - last
        if elapsed > 90:
            consecutive_dead += 1
            print(f"[WATCHDOG] Event loop figé depuis {elapsed:.0f}s (check #{consecutive_dead})")
            # Capture le snapshot AU MOMENT du freeze pour diagnostic
            try:
                snap = _take_diag_snapshot()
                print(f"[WATCHDOG] DIAG au moment du freeze : {snap}")
                # Dump aussi les 5 derniers snapshots pour voir l'évolution
                if _diag_snapshots:
                    print(f"[WATCHDOG] Snapshots récents : {_diag_snapshots[-5:]}")
            except Exception as e:
                print(f"[WATCHDOG] diag dump failed : {e}")
            # Après 2 checks consécutifs (= ~60-90s d'event loop figé), on tue
            if consecutive_dead >= 2:
                print("[WATCHDOG] FORCE EXIT — Render va redémarrer l'instance")
                _os._exit(1)
        else:
            if consecutive_dead > 0:
                print(f"[WATCHDOG] OK, event loop a repris (était figé)")
            consecutive_dead = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app
    # ── 2 threadpools séparés pour isoler les workloads ───────────────────
    # PRINCIPAL (15 workers) : calls Turso, FastAPI sync endpoints.
    # PUSH (5 workers) : webpush sync vers Apple/Mozilla. Isolé pour qu'un
    # burst de push (genre 30 followers notifiés en même temps) ne bloque
    # plus le pool principal et ne fasse plus timeout les requêtes HTTP user.
    import concurrent.futures
    global _PUSH_EXECUTOR
    asyncio.get_event_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=15, thread_name_prefix="drunk-bg")
    )
    _PUSH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="drunk-push")
    init_db()
    init_push_subscriptions()
    # Charge le cache mémoire des bannis pour bloquer leurs requêtes API
    _reload_banned_set()
    # Recalcule les streaks de tous les users depuis drink_logs (répare les
    # valeurs corrompues : race conditions historiques, dérèglements temporels)
    try:
        from data.database import recalc_all_streaks
        n = recalc_all_streaks()
        print(f"[startup] Streaks recalculés pour {n} users")
    except Exception as e:
        print(f"[startup] recalc_all_streaks erreur (non-critique) : {e}")
    # Backfill rétroactif des badges de soirée pour toutes les sessions
    # déjà fermées (one-shot la première fois, idempotent ensuite).
    try:
        from data.database import backfill_soiree_badges
        result = backfill_soiree_badges()
        print(f"[startup] Soirée badges backfill : {result}")
    except Exception as e:
        print(f"[startup] backfill_soiree_badges erreur : {e}")

    from bot.bot import create_application
    _bot_app = create_application()
    await _bot_app.initialize()
    await _bot_app.start()

    # Webhook : Telegram envoie les messages à notre URL.
    # secret_token : Telegram joindra le header X-Telegram-Bot-Api-Secret-Token
    # à chaque update, qu'on valide côté handler pour bloquer les faux POST.
    _tg_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    set_webhook_kwargs = {
        "url": f"{RENDER_URL}/telegram-webhook",
        "drop_pending_updates": True,
    }
    if _tg_secret:
        set_webhook_kwargs["secret_token"] = _tg_secret
    await _bot_app.bot.set_webhook(**set_webhook_kwargs)

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

    # Watchdog : détecte un event loop bloqué et force le restart de l'instance.
    # 2 composants :
    #   - une coroutine qui pulse toutes les 5s (si l'event loop bloque, ça
    #     ne pulse plus)
    #   - un thread système qui vérifie le pulse toutes les 30s et fait
    #     os._exit() si pas de pulse depuis >90s (Render redémarre l'instance)
    asyncio.create_task(_heartbeat_async_loop())
    asyncio.create_task(_diag_loop())
    threading.Thread(target=_watchdog_thread, daemon=True, name="watchdog").start()

    yield

    await _bot_app.stop()
    await _bot_app.shutdown()



app = FastAPI(title="AlcooTracker API", lifespan=lifespan)

_DEFAULT_ALLOWED_ORIGINS = [
    "https://drunk-weld.vercel.app",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
_extra_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
# Autorise aussi tous les sous-domaines Vercel preview (par ex. drunk-weld-git-main-xxx.vercel.app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEFAULT_ALLOWED_ORIGINS + _extra_origins,
    allow_origin_regex=r"https://drunk-weld.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ── Rate limiting (mémoire, par IP, sur POST) ─────────────────────────────────
from collections import defaultdict
_rate_buckets: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60.0   # secondes
RATE_LIMIT_MAX = 180       # requêtes par fenêtre (3/sec en moyenne)
_RATE_SKIP_PREFIXES = ("/stripe/webhook", "/telegram-webhook")


def _client_ip(request: Request) -> str:
    # Sur Render, l'IP réelle du client est le DERNIER élément de X-Forwarded-For
    # (le proxy Render ajoute l'IP réelle après les IPs spoofées par l'attaquant).
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


_rate_last_cleanup = 0.0


def _cleanup_rate_buckets(now: float):
    """Supprime les buckets vides (IPs inactives) pour éviter une fuite mémoire."""
    global _rate_last_cleanup
    if now - _rate_last_cleanup < 300:  # toutes les 5 min max
        return
    _rate_last_cleanup = now
    cutoff = now - RATE_LIMIT_WINDOW
    for ip in list(_rate_buckets.keys()):
        b = _rate_buckets[ip]
        while b and b[0] < cutoff:
            b.pop(0)
        if not b:
            del _rate_buckets[ip]


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.method == "POST" and not any(
        request.url.path.startswith(p) for p in _RATE_SKIP_PREFIXES
    ):
        ip = _client_ip(request)
        now = time.time()
        _cleanup_rate_buckets(now)
        bucket = _rate_buckets[ip]
        cutoff = now - RATE_LIMIT_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= RATE_LIMIT_MAX:
            return Response(
                content=json.dumps({"ok": False, "error": "Trop de requêtes, attends un peu."}),
                status_code=429,
                media_type="application/json",
            )
        bucket.append(now)
    return await call_next(request)


# ── Webhook Telegram ──────────────────────────────────────────────────────────

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if expected:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if received != expected:
            return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, _bot_app.bot)
    await _bot_app.process_update(update)
    return {"ok": True}


# ── Helpers dashboard ─────────────────────────────────────────────────────────

def build_snapshot() -> list[dict]:
    from data.database import get_current_session_drink_counts, _fetchall as _fa_sn
    users = {u["user_id"]: u for u in get_all_users()}
    drinks_by_user = get_all_active_drinks()
    # nb_drinks affiché = verres de la session ACTIVE uniquement (cohérent avec
    # la timeline qui ne montre que la session active). Le BAC reste calculé
    # sur les drinks <48h pour l'alcoolémie résiduelle.
    session_counts = get_current_session_drink_counts()
    # Compte de vomis de la session active de chaque user (visible Live + classement)
    vomi_rows = _fa_sn("""
        SELECT v.user_id, COUNT(*) AS c FROM vomi_logs v
        JOIN sessions s ON v.session_id = s.id
        WHERE s.active = 1
        GROUP BY v.user_id
    """)
    vomis_session = {int(r["user_id"]): int(r["c"] or 0) for r in vomi_rows}
    now = datetime.now(timezone.utc)
    banned_ids = {u["user_id"] for u in _get_banned_list()}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    result = []
    for uid, user in users.items():
        # Note : on n'exclut PAS les users discrets ici, on marque juste avec
        # discreet_mode=true. Le frontend filtre côté client (en gardant l'user
        # lui-même visible pour qu'il se voie sur Live).
        drinks = drinks_by_user.get(uid, [])
        bac = total_bac(drinks, user["weight_kg"], user["gender"], now)
        # Peak BAC during current session (last 24h)
        peak_24h = 0.0
        for i in range(len(drinks)):
            b = total_bac(drinks[:i+1], user["weight_kg"], user["gender"], drinks[i][1])
            if b > peak_24h:
                peak_24h = b
        result.append({
            "user_id":     uid,
            "username":    user["username"],
            "bac":         round(bac, 3),
            "label":       bac_label(bac),
            "sober_in_h":  round(sober_in_hours(bac), 1),
            "nb_drinks":   session_counts.get(uid, 0),
            "has_session": uid in drinks_by_user,
            # lat/lon/weight_kg ne sont plus dans le snapshot public.
            # Pour la carte : utiliser /locations/{telegram_id} (filtré par suivis).
            # Pour le poids perso : /me/{telegram_id}.
            "max_bac":     round(user.get("max_bac") or 0, 2),
            "peak_24h":    round(peak_24h, 2),
            "gender":      user.get("gender", "homme"),
            "is_premium":  uid == admin_id or bool(user.get("is_premium")),
            "level":       calc_level(int(user.get("xp") or 0)),
            "streak":      int(user.get("current_streak") or 0),
            "active_skin": user.get("active_skin") or "default",
            "discreet":    bool(user.get("discreet_mode")),
            "is_banned":   uid in banned_ids,
            "vomis":       vomis_session.get(uid, 0),
        })
    result.sort(key=lambda x: x["bac"], reverse=True)
    return result


async def _broadcast(data: list[dict]):
    try:
        payload = json.dumps(data)
    except Exception:
        return
    # Envoi en parallèle (gather) avec timeout par client pour ne pas qu'un
    # client lent bloque la diffusion aux autres. Avant : envoi en série,
    # un client mobile en zone moche bloquait pendant ~10s tous les autres.
    clients = list(_ws_clients)
    if not clients:
        return
    async def _send_one(ws):
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=3.0)
            return None
        except Exception:
            return ws
    results = await asyncio.gather(*[_send_one(ws) for ws in clients], return_exceptions=True)
    for r in results:
        if isinstance(r, WebSocket):
            _ws_clients.discard(r)


async def _broadcast_loop():
    while True:
        await asyncio.sleep(300)
        # build_snapshot() fait 3 calls Turso HTTP sync — on l'isole dans un
        # thread pour ne pas bloquer l'event loop pendant la requête réseau.
        snapshot = await asyncio.to_thread(build_snapshot)
        await _broadcast(snapshot)


async def _danger_loop():
    global _drunk_follower_notified
    while True:
        await asyncio.sleep(300)
        now   = datetime.now(timezone.utc)
        loop  = asyncio.get_event_loop()
        # Tous les calls DB sync wrapped dans des threads pour libérer
        # l'event loop pendant la latence HTTP Turso (~200-500ms chacun).
        users_list = await asyncio.to_thread(get_all_users)
        users = {u["telegram_id"]: u for u in users_list}
        drinks_by_user = await asyncio.to_thread(get_all_active_drinks)

        # ── Construit la map follower_id → set(following_ids) ────────────────
        all_follows = await asyncio.to_thread(get_all_follows)
        follower_map: dict[int, set[int]] = {}   # follower → who they follow
        drinker_followers: dict[int, list[int]] = {}  # drinker → their followers
        for row in all_follows:
            follower_map.setdefault(row["follower_id"], set()).add(row["following_id"])
            drinker_followers.setdefault(row["following_id"], []).append(row["follower_id"])

        for uid, user in users.items():
            drinks = drinks_by_user.get(uid, [])
            bac    = total_bac(drinks, user["weight_kg"], user["gender"], now) if drinks else 0.0

            if not drinks or bac <= 0:
                _danger_notified.pop(uid, None)
                # Nettoie les notifs "ami ivre" expirées pour ce buveur
                for k in list(_drunk_follower_notified):
                    if k[0] == uid:
                        del _drunk_follower_notified[k]
                continue

            # ── Notif Telegram au buveur lui-même (danger personnel) ─────────
            if bac > 1.5:
                last_drink_t = max(d[1] for d in drinks)
                if (now - last_drink_t).total_seconds() >= 1800:
                    last_notif = _danger_notified.get(uid)
                    if not last_notif or (now - last_notif).total_seconds() >= 3600:
                        _danger_notified[uid] = now
                        if _TG_NOTIFS:
                            try:
                                await _bot_app.bot.send_message(
                                    chat_id=uid,
                                    text=f"👀 *{user['username']}*, t'es encore vivant ? {bac:.2f} g/L depuis un moment...",
                                    parse_mode="Markdown"
                                )
                            except Exception:
                                pass

            # ── Notif PUSH aux abonnés : "ami en charge" (seuil 1.5 g/L) ────
            if bac >= 1.5:
                gender   = user.get("gender", "homme")
                lui_elle = "elle" if gender == "femme" else "lui"
                name     = user["username"]
                followers = drinker_followers.get(uid, [])
                for fid in followers:
                    key = (uid, fid)
                    last_notif = _drunk_follower_notified.get(key)
                    if last_notif and (now - last_notif).total_seconds() < 8 * 3600:
                        continue
                    _drunk_follower_notified[key] = now
                    loop.run_in_executor(
                        _PUSH_EXECUTOR, _send_push, fid,
                        "🚨 Drunk",
                        f"{name} est en train de se mettre une énorme charge, fais attention à {lui_elle} !",
                        "/?tab=live"
                    )

        # ── Notif inactivité — Telegram + Push, une fois par semaine ─────────
        _INACTIVITY_MSGS = [
            ("😤 *{name}*, t'es devenu gay pour pas picoler depuis une semaine ? Allez, bois un verre ! 🍺",
             "😤 {name}, t'es devenu gay pour pas picoler depuis une semaine ? Allez, bois un verre ! 🍺"),
            ("😶 *{name}*, deux semaines sans boire… t'as rejoint les alcooliques anonymes ou quoi ? 🤨",
             "😶 {name}, deux semaines sans boire… t'as rejoint les alcooliques anonymes ou quoi ? 🤨"),
            ("💀 *{name}*, trois semaines. T'es sobre. C'est honteux. Tes potes ont honte de toi. 🫵",
             "💀 {name}, trois semaines. T'es sobre. C'est honteux. Tes potes ont honte de toi. 🫵"),
            ("🚨 *{name}*, un mois sans picoler. Appelle le 15, c'est une urgence médicale. 🏥",
             "🚨 {name}, un mois sans picoler. Appelle le 15, c'est une urgence médicale. 🏥"),
        ]
        for user in await asyncio.to_thread(get_all_users):
            uid    = user["telegram_id"]
            last_t = await asyncio.to_thread(get_last_drink_time, uid)
            if last_t is None:
                continue
            days_inactive = (now - last_t).total_seconds() / 86400
            if days_inactive >= 7:
                last_notif = await asyncio.to_thread(get_last_inactivity_notif, uid)
                if last_notif is None or (now - last_notif).total_seconds() >= 7 * 86400:
                    await asyncio.to_thread(set_last_inactivity_notif, uid, now)
                    weeks = int(days_inactive // 7)
                    idx   = min(weeks - 1, len(_INACTIVITY_MSGS) - 1)
                    tg_tpl, push_tpl = _INACTIVITY_MSGS[idx]
                    name = user["username"]
                    if _TG_NOTIFS:
                        try:
                            await _bot_app.bot.send_message(
                                chat_id=uid,
                                text=tg_tpl.format(name=name),
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                    loop.run_in_executor(
                        _PUSH_EXECUTOR, _send_push, uid,
                        "🍺 Drunk",
                        push_tpl.format(name=name),
                        "/"
                    )


async def _weekly_recap_loop():
    global _last_weekly_recap_date
    while True:
        await asyncio.sleep(60)
        now_paris = datetime.now(PARIS)
        # Lundi à 8h00 heure de Paris
        if now_paris.weekday() == 0 and now_paris.hour == 8:
            today_str = now_paris.strftime("%Y-%m-%d")
            if _last_weekly_recap_date != today_str:
                _last_weekly_recap_date = today_str
                until = datetime.now(timezone.utc)
                since = until - timedelta(days=7)

                # Construit la map suivis par utilisateur (DB sync → thread)
                all_follows = await asyncio.to_thread(get_all_follows)
                user_following: dict[int, list[int]] = {}
                for row in all_follows:
                    user_following.setdefault(row["follower_id"], []).append(row["following_id"])

                loop = asyncio.get_event_loop()
                for user in await asyncio.to_thread(get_all_users):
                    uid      = user["telegram_id"]
                    username = user["username"]
                    following_ids = user_following.get(uid, [])

                    # Recap uniquement si l'utilisateur suit au moins une personne
                    if not following_ids:
                        continue

                    tg_msg, push_body = await asyncio.to_thread(
                        build_weekly_recap_for_user, username, following_ids, since, until
                    )

                    if _TG_NOTIFS:
                        try:
                            await _bot_app.bot.send_message(
                                chat_id=uid,
                                text=tg_msg,
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

                    loop.run_in_executor(
                        _PUSH_EXECUTOR, _send_push, uid,
                        "📊 Recap de la semaine",
                        push_body,
                        "/?tab=classement"
                    )


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
        ("DELETE FROM blackjack_hands_history", []),
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


# ── Admin helpers ──────────────────────────────────────────────────────────────

_auth_token_cache: dict[str, tuple[int, float]] = {}  # token → (uid, expiry_ts)
_AUTH_TOKEN_TTL = 300.0  # 5 min en mémoire

# Cache mémoire des user_ids bannis — évite une query DB à chaque requête.
# Rafraîchi au boot et à chaque ban/unban.
_banned_user_ids: set[int] = set()


def _reload_banned_set():
    """Recharge le set des user_ids bannis depuis la DB."""
    global _banned_user_ids
    try:
        _banned_user_ids = {u["user_id"] for u in _get_banned_list()}
    except Exception:
        pass


def _authed_uid(request: Request) -> int | None:
    """Extrait l'user_id du header Authorization: Bearer <token>.
    Renvoie None si pas de token, token invalide ou expiré.
    Cache mémoire 5min pour éviter 1 query DB à chaque requête."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    cached = _auth_token_cache.get(token)
    if cached:
        uid, exp = cached
        if exp > time.time():
            return uid
        _auth_token_cache.pop(token, None)
    uid = get_uid_from_token(token)
    if uid is not None:
        _auth_token_cache[token] = (uid, time.time() + _AUTH_TOKEN_TTL)
    return uid


def _resolve_user(request: Request, body: dict, key: str = "telegram_id") -> int | None:
    """Renvoie l'user_id à utiliser pour la requête.
    Priorité au token bearer si fourni (sécurisé), sinon fallback sur body[key]
    pour la backwards-compat avec les anciens clients sans token.
    Retourne None si l'user est banni (bloque toutes les actions API)."""
    uid = _authed_uid(request)
    if uid is None:
        val = body.get(key)
        try:
            uid = int(val) if val else None
        except (ValueError, TypeError):
            uid = None
    # Blocage des bannis sur tous les endpoints qui passent par _resolve_user
    if uid is not None and uid in _banned_user_ids:
        return None
    return uid


def _require_authed_uid(request: Request, claimed_id) -> tuple[int | None, dict | None]:
    """Helper pour endpoints sensibles. Mode strict si token fourni :
      - token valide → renvoie (authed_uid, None) ; refuse si claimed_id ne matche pas
      - token invalide → ({}, erreur 401)
      - pas de token → renvoie (claimed_id, None) en mode legacy backwards-compat
    Le tuple None côté erreur signifie : tout va bien, utilise le 1er élément.
    Sinon le 2e élément est le dict d'erreur à renvoyer."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        uid = get_uid_from_token(auth[7:].strip())
        if uid is None:
            return None, {"ok": False, "error": "Session expirée, reconnecte-toi.", "_status": 401}
        if claimed_id is not None and int(claimed_id) != uid:
            admin_id = int(os.environ.get("ADMIN_ID", "0"))
            if uid != admin_id:
                return None, {"ok": False, "error": "Non autorisé"}
        return uid, None
    # Legacy : pas de token → on accepte le claimed_id du body (transition)
    if claimed_id is None:
        return None, {"ok": False, "error": "Non authentifié"}
    return int(claimed_id), None


def _check_badges_for_user(user_id: int) -> list[str]:
    """Check toutes les conditions de badges pour un user et débloque ceux
    qui peuvent l'être. Renvoie les badge_keys nouvellement débloqués."""
    if not user_id:
        return []
    from data.database import _fetchone as _fo, _fetchall as _fa
    newly = []

    # Récupère les stats nécessaires en bulk
    user = get_user(user_id)
    if not user:
        return []
    coins = int(user.get("coins") or 0)
    max_bac = float(user.get("max_bac") or 0)
    is_premium = bool(user.get("is_premium"))
    has_avatar = bool(user.get("avatar"))

    total_drinks = _fo("SELECT COUNT(*) c FROM drink_logs WHERE user_id=?", [user_id])
    total_drinks = int(total_drinks["c"]) if total_drinks else 0

    max_session_drinks = _fo(
        """SELECT MAX(c) m FROM (
             SELECT COUNT(*) c FROM drink_logs WHERE user_id=? GROUP BY session_id
           )""", [user_id]
    )
    max_session_drinks = int((max_session_drinks or {}).get("m") or 0)

    streak = get_streak(user_id)

    # Paris gagnés / coinflips
    bet_wins = _fo(
        "SELECT COUNT(*) c FROM bets WHERE winner_id=? AND status='settled'",
        [user_id]
    )
    bet_wins = int(bet_wins["c"]) if bet_wins else 0

    coinflip_wins = _fo(
        "SELECT COUNT(*) c FROM bets WHERE winner_id=? AND status='settled' AND bet_type='coinflip'",
        [user_id]
    )
    coinflip_wins = int(coinflip_wins["c"]) if coinflip_wins else 0

    # Blackjack wins (compte chaque main jouée via l'historique)
    bj_wins_row = _fo(
        "SELECT COUNT(*) c FROM blackjack_hands_history WHERE user_id=? AND result IN ('win','blackjack')",
        [user_id]
    )
    bj_wins = int(bj_wins_row["c"]) if bj_wins_row else 0

    bj_naturel = _fo(
        "SELECT 1 FROM blackjack_hands_history WHERE user_id=? AND result='blackjack' LIMIT 1",
        [user_id]
    )

    # Social
    following_count = len(get_following(user_id))
    followers_count = len(get_followers(user_id))
    refs = count_referrals(user_id)

    # Map badge_key → condition (bool)
    conds = {
        "first_drink":  total_drinks >= 1,
        "first_friend": following_count >= 1,
        "drink_10":     total_drinks >= 10,
        "drink_50":     total_drinks >= 50,
        "drink_100":    total_drinks >= 100,
        "drink_500":    total_drinks >= 500,
        "drink_1000":   total_drinks >= 1000,
        "night_10":     max_session_drinks >= 10,
        "night_15":     max_session_drinks >= 15,
        "high_bac_1":   max_bac >= 1.0,
        "high_bac_2":   max_bac >= 2.0,
        "streak_3":     streak["current"] >= 3 or streak["longest"] >= 3,
        "streak_7":     streak["current"] >= 7 or streak["longest"] >= 7,
        "streak_14":    streak["current"] >= 14 or streak["longest"] >= 14,
        "coins_500":    coins >= 500,
        "coins_2000":   coins >= 2000,
        "coins_10000":  coins >= 10000,
        "bet_win_5":    bet_wins >= 5,
        "bet_win_20":   bet_wins >= 20,
        "coinflip_5":   coinflip_wins >= 5,
        "bj_win_5":     bj_wins >= 5,
        "bj_win_20":    bj_wins >= 20,
        "bj_blackjack": bj_naturel is not None,
        "friends_5":    following_count >= 5,
        "friends_20":   followers_count >= 20,
        "referral_1":   refs >= 1,
        "referral_5":   refs >= 5,
        "premium":      is_premium,
        "set_avatar":   has_avatar,
        # first_pari / first_bj : seront unlocked au moment de l'action (pas besoin de query)
    }
    for key, ok in conds.items():
        if ok and unlock_badge(user_id, key):
            newly.append(key)
    return newly


def _award_drink(user_id: int) -> dict:
    """Hook après un ajout de verre : +XP, streak, badges."""
    if not user_id:
        return {}
    db_add_xp(user_id, XP_PER_DRINK)
    bump_streak(user_id)
    new_badges = _check_badges_for_user(user_id)
    return {"new_badges": new_badges}


def _award_bet_win(user_id: int):
    if not user_id:
        return
    db_add_xp(user_id, XP_PER_BET_WIN)
    _check_badges_for_user(user_id)


def _award_bj_win(user_id: int):
    if not user_id:
        return
    db_add_xp(user_id, XP_PER_BJ_WIN)
    _check_badges_for_user(user_id)


def _check_admin(caller_id, secret: str | None = None) -> bool:
    """Vérifie qu'un appel est légitime : caller_id == ADMIN_ID ET secret partagé.
    Si ADMIN_SECRET n'est pas défini en env, on tolère sans secret (backwards compat
    en local). En prod il DOIT être défini."""
    aid = int(os.environ.get("ADMIN_ID", "0"))
    expected = os.environ.get("ADMIN_SECRET", "")
    if not aid or int(caller_id or 0) != aid:
        return False
    if expected:
        return secret == expected
    return True


@app.post("/admin/reset-soiree-badges")
async def admin_reset_soiree_badges(request: Request):
    """Efface TOUS les badges de soirée d'un user (et invalide ses caches)."""
    body = await request.json()
    target_id = int(body.get("target_id", 0))
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    _execute("DELETE FROM soiree_badges WHERE user_id=?", [target_id])
    _invalidate_cache(f"me:{target_id}", f"profile:{target_id}")
    return {"ok": True}


@app.post("/admin/remove-soiree-badge")
async def admin_remove_soiree_badge(request: Request):
    """Efface tous les badges d'un type spécifique pour un user.
    body: {target_id, badge_key}"""
    body = await request.json()
    target_id = int(body.get("target_id", 0))
    badge_key = body.get("badge_key", "")
    if not target_id or not badge_key:
        return {"ok": False, "error": "target_id et badge_key requis"}
    _execute(
        "DELETE FROM soiree_badges WHERE user_id=? AND badge_key=?",
        [target_id, badge_key]
    )
    _invalidate_cache(f"me:{target_id}", f"profile:{target_id}")
    return {"ok": True}


@app.post("/admin/reset-max-bac")
async def admin_reset_max_bac(request: Request):
    """Remet à 0 le record all-time max_bac d'un user."""
    body = await request.json()
    target_id = int(body.get("target_id", 0))
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    _execute("UPDATE users SET max_bac=0 WHERE user_id=?", [target_id])
    _invalidate_cache(
        "snapshot", "alltime_cache",
        f"me:{target_id}", f"records:{target_id}", f"profile:{target_id}",
    )
    _invalidate_alltime_cache()
    return {"ok": True}


@app.get("/admin/soiree-badges-by-user")
def admin_soiree_badges_by_user():
    """Liste qui a obtenu quels badges de soirée (pour debug/curiosité)."""
    rows = _fetchall("""
        SELECT u.username, sb.badge_key, sb.peak_bac, sb.had_vomi, sb.awarded_at,
               sb.user_id, sb.session_id
        FROM soiree_badges sb
        JOIN users u ON u.user_id = sb.user_id
        ORDER BY sb.awarded_at DESC
    """)
    return rows


@app.post("/admin/backfill-soiree-badges")
async def admin_backfill_soiree_badges(request: Request):
    """Force le calcul des badges de soirée pour TOUTES les sessions fermées
    dans l'historique. Idempotent : ne crée pas de doublons. Skip les sessions
    encore actives (peak peut encore monter)."""
    from data.database import backfill_soiree_badges
    result = backfill_soiree_badges()
    # Invalide les caches /me et /profile pour refléter les nouveaux badges
    for key in list(_endpoint_cache.keys()):
        if key.startswith("me:") or key.startswith("profile:"):
            _endpoint_cache.pop(key, None)
    return {"ok": True, **result}


@app.post("/admin/recalc-streaks")
async def admin_recalc_streaks(request: Request):
    """Force le recalcul de tous les streaks depuis drink_logs."""
    from data.database import recalc_all_streaks
    n = recalc_all_streaks()
    # Invalide les caches /me pour refléter les nouveaux streaks immédiatement
    for key in list(_endpoint_cache.keys()):
        if key.startswith("me:") or key.startswith("profile:"):
            _endpoint_cache.pop(key, None)
    return {"ok": True, "users_recalculated": n}


@app.get("/admin/diag")
def admin_diag():
    """Diagnostic temps réel : état actuel + historique des 60 dernières min.
    Permet d'identifier ce qui croît anormalement (memory leak, tasks accumulées,
    file descriptors épuisés, etc.) avant un freeze."""
    return {
        "current": _take_diag_snapshot(),
        "history_60min": _diag_snapshots,
    }


@app.get("/admin/debug-drinks-dates/{telegram_id}")
def admin_debug_drinks_dates(telegram_id: int, days: int = 14):
    """DEBUG : retourne le count de verres par jour pour un user sur les N derniers jours.
    Utile pour vérifier la cohérence du streak."""
    from data.database import _fetchall as _fa
    rows = _fa(
        f"""SELECT DATE(logged_at) AS d, COUNT(*) AS c
            FROM drink_logs
            WHERE user_id = ?
              AND logged_at >= datetime('now', '-{int(days)} days')
            GROUP BY DATE(logged_at)
            ORDER BY d DESC""",
        [telegram_id]
    )
    return {"telegram_id": telegram_id, "days": [{"date": r["d"], "count": int(r["c"])} for r in rows]}


@app.get("/admin/users")
def admin_get_users(caller_id: int = 0, admin_secret: str = ""):
    if not _check_admin(caller_id, admin_secret):
        return {"ok": False, "error": "Non autorisé"}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    users    = get_all_users()
    drinks_by_user = get_all_active_drinks()
    now = datetime.now(timezone.utc)
    banned = {u["telegram_id"] for u in _get_banned_list()}
    # Set des user_ids qui ont au moins 1 push_subscription
    push_rows = _fetchall("SELECT DISTINCT user_id FROM push_subscriptions")
    push_users = {int(r["user_id"]) for r in push_rows if r.get("user_id")}
    result = []
    for u in users:
        uid    = u["telegram_id"]
        drinks = drinks_by_user.get(uid, [])
        bac    = round(total_bac(drinks, u["weight_kg"], u["gender"], now), 2)
        result.append({
            "telegram_id": uid,
            "username":    u["username"],
            "gender":      u.get("gender", "homme"),
            "is_admin":    uid == admin_id,
            "is_banned":   uid in banned,
            "coins":       get_coins(uid),
            "bac":         bac,
            "nb_drinks":   len(drinks),
            "has_push":    uid in push_users,
        })
    result.sort(key=lambda x: x["username"].lower())
    return {"ok": True, "users": result}


def _get_banned_list():
    # Utilise la même détection de colonne (user_id vs telegram_id) que les
    # helpers ban_user/unban_user pour être robuste à l'état réel de la DB.
    from data.database import _banned_col
    col = _banned_col()
    return _fetchall(f"SELECT {col} FROM banned_users")


@app.post("/admin/ban")
async def admin_ban(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    tid = int(target_id)
    ban_user(tid)
    # Ajoute au cache mémoire des bannis : _resolve_user bloquera toutes les
    # requêtes (même celles sans token, en mode legacy avec telegram_id en body).
    _banned_user_ids.add(tid)
    # Révoque tous les auth_token du user banni : il sera déconnecté au prochain
    # appel API et ne pourra pas se reconnecter (login vérifie aussi is_banned).
    revoke_all_user_sessions(tid)
    # Vider le cache mémoire des tokens pour ce user (sinon il pourrait
    # continuer à passer pendant 5min avec un token caché)
    for tok, (uid, _) in list(_auth_token_cache.items()):
        if uid == tid:
            _auth_token_cache.pop(tok, None)
    # Virer de toute session BJ active
    bj_sess = get_blackjack_session_by_player(tid)
    if bj_sess:
        token = bj_sess["token"]
        players = get_blackjack_players(bj_sess["id"])
        remaining = [p for p in players if p["telegram_id"] != tid]
        if not remaining or bj_sess["creator_id"] == tid:
            update_blackjack_session(bj_sess["id"], status="finished")
        await _bj_broadcast(token)
    # Diffuse le snapshot mis à jour (tous les clients voient is_banned=True)
    # → render() côté frontend détecte is_banned et déconnecte le user.
    await _broadcast(build_snapshot())
    return {"ok": True}


@app.post("/admin/unban")
async def admin_unban(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    unban_user(int(target_id))
    _banned_user_ids.discard(int(target_id))
    await _broadcast(build_snapshot())
    return {"ok": True}


@app.post("/admin/rename")
async def admin_rename(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    new_name  = (body.get("new_name") or "").strip()
    if not target_id or not new_name:
        return {"ok": False, "error": "target_id et new_name requis"}
    if is_username_taken(new_name, int(target_id)):
        return {"ok": False, "error": "Pseudo déjà utilisé"}
    rename_user(int(target_id), new_name)
    await _broadcast(build_snapshot())
    return {"ok": True}


@app.post("/admin/remove-drinks")
async def admin_remove_drinks(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    n         = int(body.get("n", 1))
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    tid = int(target_id)
    removed = delete_n_drinks(tid, n)
    # Vider le cache mémoire des drinks de ce user — sinon son prochain
    # log-drink utiliserait l'ancienne liste (avec les verres déjà supprimés)
    # pour calculer le BAC, et le user verrait ses verres "ressusciter".
    _user_drinks_cache.pop(tid, None)
    _last_drink_at.pop(tid, None)
    _invalidate_alltime_cache()
    _invalidate_cache(
        "snapshot", "coins_all", "history_24h",
        f"me:{tid}", f"records:{tid}",
        f"profile:{tid}", f"locations:{tid}",
        f"favorites:{tid}:3", f"favorites:{tid}:2",
    )
    await _broadcast(build_snapshot())
    return {"ok": True, "removed": removed}


@app.post("/admin/end-session")
async def admin_end_session(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    tid = int(target_id)
    end_session(tid)
    # Vide cache mémoire — session finie = 0 verres pour la prochaine
    _user_drinks_cache.pop(tid, None)
    _last_drink_at.pop(tid, None)
    _invalidate_cache(
        "snapshot", "history_24h",
        f"me:{tid}", f"profile:{tid}", f"locations:{tid}",
    )
    await _broadcast(build_snapshot())
    return {"ok": True}


@app.post("/admin/reset-password")
async def admin_reset_password(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    clear_password(int(target_id))
    return {"ok": True}


@app.post("/admin/give-coins")
async def admin_give_coins(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    amount    = body.get("amount")
    if not target_id or amount is None:
        return {"ok": False, "error": "target_id et amount requis"}
    add_coins(int(target_id), int(amount), "Admin")
    return {"ok": True, "new_coins": get_coins(int(target_id))}


@app.post("/admin/broadcast-push")
async def admin_broadcast_push(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    title   = (body.get("title") or "").strip()
    message = (body.get("message") or "").strip()
    url     = body.get("url", "/")
    if not title or not message:
        return {"ok": False, "error": "title et message requis"}
    import asyncio
    loop = asyncio.get_event_loop()
    users = get_all_users()
    for u in users:
        loop.run_in_executor(_PUSH_EXECUTOR, _send_push, u["telegram_id"], title, message, url)
    return {"ok": True, "sent_to": len(users)}


@app.post("/admin/delete-user")
async def admin_delete_user(request: Request):
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    target_id = body.get("target_id")
    if not target_id:
        return {"ok": False, "error": "target_id requis"}
    tid = int(target_id)
    # Fermer la session BJ si active
    bj_sess = get_blackjack_session_by_player(tid)
    if bj_sess:
        update_blackjack_session(bj_sess["id"], status="finished")
        await _bj_broadcast(bj_sess["token"])
    delete_user(tid)
    await _broadcast(build_snapshot())
    return {"ok": True}


@app.post("/admin/close-all-bj")
async def admin_close_all_bj(request: Request):
    """Clôture toutes les parties BJ en cours (waiting + active)."""
    body = await request.json()
    if not _check_admin(body.get("caller_id"), body.get("admin_secret")):
        return {"ok": False, "error": "Non autorisé"}
    sessions = get_active_blackjack_sessions()
    closed = 0
    for sess in sessions:
        if sess["status"] in ("waiting", "active"):
            update_blackjack_session(sess["id"], status="finished")
            await _bj_broadcast(sess["token"])
            closed += 1
    return {"ok": True, "closed": closed}


@app.get("/snapshot")
def get_snapshot():
    # Cache 5s : sans ça, chaque user qui ouvre l'app + WS qui broadcast
    # = saturation Turso. 5s de fraîcheur est largement acceptable pour le Live.
    return _cached("snapshot", 5.0, build_snapshot)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        try:
            # build_snapshot fait 3 calls Turso sync — to_thread pour ne pas
            # bloquer l'event loop à chaque accept de WS.
            snapshot = await asyncio.to_thread(build_snapshot)
            await ws.send_text(json.dumps(snapshot))
        except Exception:
            pass
        while True:
            # On ignore le contenu (le client ne nous parle pas), mais on doit
            # consommer les messages pour détecter la déconnexion proprement.
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                raise
            except Exception:
                # Message mal formé / binaire / etc. → on continue sans crash
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


@app.post("/refresh")
async def trigger_refresh():
    snapshot = build_snapshot()
    await _broadcast(snapshot)
    return {"ok": True}


_alltime_cache = {"data": None, "ts": 0.0}
ALLTIME_CACHE_TTL = 60.0


def _invalidate_alltime_cache():
    _alltime_cache["data"] = None
    _alltime_cache["ts"] = 0.0


# ── Cache TTL générique pour endpoints lourds ─────────────────────────────────
# Coupe la cascade DB sur Render free tier (chaque requête Turso = 100-300ms
# HTTP). Sans ce cache, 50 users qui ouvrent l'app = 50× tous les fetchs en
# parallèle = saturation du serveur.
_endpoint_cache: dict[str, dict] = {}


def _cached(key: str, ttl: float, builder):
    """Retourne la valeur cachée si fraîche, sinon rebuild et cache."""
    now = time.time()
    entry = _endpoint_cache.get(key)
    if entry and (now - entry["ts"]) < ttl:
        return entry["data"]
    data = builder()
    _endpoint_cache[key] = {"data": data, "ts": now}
    return data


def _invalidate_cache(*keys: str):
    """Invalide une ou plusieurs entrées de cache."""
    if not keys:
        _endpoint_cache.clear()
        return
    for k in keys:
        _endpoint_cache.pop(k, None)


@app.get("/me/{telegram_id}")
def get_me(telegram_id: int):
    """Renvoie les infos perso d'un user (poids, gender, premium, XP, badges, streak)."""
    user = get_user(telegram_id)
    if not user:
        return {"ok": False}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    uid = user.get("user_id") or user.get("telegram_id")
    xp = int(user.get("xp") or 0)
    lvl = calc_level(xp)
    badges_unlocked = get_user_badges(uid)
    streak = get_streak(uid)
    return {
        "ok": True,
        "telegram_id": uid,
        "username": user.get("username"),
        "weight_kg": user.get("weight_kg"),
        "gender": user.get("gender", "homme"),
        "coins": user.get("coins", 0),
        "is_admin": uid == admin_id,
        "is_premium": uid == admin_id or bool(user.get("is_premium")),
        "level": lvl,
        "badges_unlocked": badges_unlocked,
        "badges_count": len(badges_unlocked),
        "badges_total": len(BADGES),
        "streak": streak,
        "referrals_count": count_referrals(uid),
        "discreet_mode": bool(user.get("discreet_mode")),
        "soiree_badges": get_soiree_badges_counts(uid),
        "vomis_total": count_vomis_total(uid),
    }


@app.get("/records/{telegram_id}")
def get_records(telegram_id: int):
    """Records personnels d'un user : pic alcool, plus grosse session, plus
    gros pari gagné, jours depuis l'inscription."""
    from data.database import _fetchone as _fo, _fetchall as _fa
    user = get_user(telegram_id)
    if not user:
        return {"ok": False}
    max_bac = float(user.get("max_bac") or 0)
    max_session = _fo(
        """SELECT MAX(c) m FROM (
             SELECT COUNT(*) c FROM drink_logs WHERE user_id=? GROUP BY session_id
           )""", [telegram_id]
    )
    max_session_drinks = int((max_session or {}).get("m") or 0)
    biggest_bet = _fo(
        "SELECT MAX(amount) m FROM bets WHERE winner_id=? AND status='settled'",
        [telegram_id]
    )
    biggest_bet_won = int((biggest_bet or {}).get("m") or 0)
    biggest_bj = _fo(
        "SELECT MAX(bet) m FROM blackjack_hands_history WHERE user_id=? AND result IN ('win','blackjack')",
        [telegram_id]
    )
    biggest_bj_win = int((biggest_bj or {}).get("m") or 0)
    # Premier verre = "anniversaire"
    first = _fo(
        "SELECT MIN(logged_at) m FROM drink_logs WHERE user_id=?",
        [telegram_id]
    )
    first_drink = (first or {}).get("m")
    days_since_first = None
    if first_drink:
        try:
            d = datetime.fromisoformat(first_drink).replace(tzinfo=timezone.utc)
            days_since_first = (datetime.now(timezone.utc) - d).days
        except Exception:
            pass
    total_drinks = _fo("SELECT COUNT(*) c FROM drink_logs WHERE user_id=?", [telegram_id])
    return {
        "ok": True,
        "max_bac": round(max_bac, 2),
        "max_session_drinks": max_session_drinks,
        "biggest_bet_won": biggest_bet_won,
        "biggest_bj_win": biggest_bj_win,
        "first_drink_at": first_drink,
        "days_since_first": days_since_first,
        "total_drinks": int((total_drinks or {}).get("c") or 0),
    }


@app.get("/suggestions/{telegram_id}")
def get_friend_suggestions(telegram_id: int, limit: int = 5):
    """Suggestions d'amis : amis d'amis non encore suivis par l'utilisateur."""
    from data.database import _fetchall as _fa
    my_following = set(get_following(telegram_id))
    if not my_following:
        return []
    # Récupère les follows des gens que je suis
    placeholders = ",".join("?" * len(my_following))
    rows = _fa(
        f"""SELECT following_id, COUNT(*) as score
            FROM follows
            WHERE follower_id IN ({placeholders})
              AND following_id != ?
            GROUP BY following_id
            ORDER BY score DESC
            LIMIT ?""",
        [*my_following, telegram_id, max(1, min(limit, 20))]
    )
    # Exclure ceux que je suis déjà
    suggestions = []
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    for r in rows:
        fid = int(r["following_id"])
        if fid in my_following:
            continue
        u = get_user(fid)
        if not u or u.get("discreet_mode"):
            continue
        suggestions.append({
            "telegram_id": fid,
            "username": u.get("username"),
            "gender": u.get("gender", "homme"),
            "is_admin": fid == admin_id,
            "is_premium": fid == admin_id or bool(u.get("is_premium")),
            "mutual_count": int(r["score"]),
        })
    return suggestions


@app.get("/favorites/{telegram_id}")
def get_favorites(telegram_id: int, limit: int = 3):
    """Retourne les drink_keys les plus utilisés par l'utilisateur ET le jeu
    favori (BJ vs Paris)."""
    rows = _fetchall(
        """SELECT drink_key, COUNT(*) as c FROM drink_logs
           WHERE user_id=? GROUP BY drink_key ORDER BY c DESC LIMIT ?""",
        [telegram_id, max(1, min(limit, 10))]
    )
    drinks = [
        {"drink_key": r["drink_key"], "count": int(r["c"])}
        for r in rows
        if r["drink_key"] in DRINKS
    ]
    # Jeu favori : nb mains BJ vs nb paris créés. BJ par défaut.
    bj_row = _fetchone(
        "SELECT COUNT(*) c FROM blackjack_hands_history WHERE user_id=?",
        [telegram_id]
    )
    bj_count = int((bj_row or {}).get("c") or 0)
    bets_row = _fetchone(
        "SELECT COUNT(*) c FROM bets WHERE challenger_id=? OR opponent_id=?",
        [telegram_id, telegram_id]
    )
    bets_count = int((bets_row or {}).get("c") or 0)
    favorite_game = "paris" if bets_count > bj_count else "blackjack"
    return {"drinks": drinks, "favorite_game": favorite_game}


@app.get("/badges/{telegram_id}")
def get_badges(telegram_id: int):
    """Liste tous les badges du système avec leur état (unlocked: bool)."""
    unlocked = set(get_user_badges(telegram_id))
    return [
        {**b, "unlocked": b["key"] in unlocked}
        for b in all_badges_meta()
    ]


# ── Boutique de skins (cadres d'avatar) ──────────────────────────────────────

# Skins disponibles = accessoires posés au-dessus de l'avatar (visible par tous).
# 'emoji' est l'overlay rendu côté frontend. 'default' = aucun accessoire.
SKINS = [
    {"key": "default",   "name": "Aucun",           "price": 0,    "emoji": "",     "icon": "⚪"},
    {"key": "top_hat",   "name": "Haut-de-forme",   "price": 200,  "emoji": "🎩",   "icon": "🎩"},
    {"key": "sunglasses","name": "Lunettes soleil", "price": 400,  "emoji": "🕶",    "icon": "🕶"},
    {"key": "party",     "name": "Fêtard",          "price": 600,  "emoji": "🥳",   "icon": "🥳"},
    {"key": "cowboy",    "name": "Cowboy",          "price": 800,  "emoji": "🤠",   "icon": "🤠"},
    {"key": "pirate",    "name": "Pirate",          "price": 1200, "emoji": "🏴‍☠️", "icon": "🏴‍☠️"},
    {"key": "horns",     "name": "Diable",          "price": 1500, "emoji": "😈",   "icon": "😈"},
    {"key": "halo",      "name": "Ange",            "price": 2000, "emoji": "😇",   "icon": "😇"},
    {"key": "santa",     "name": "Père Noël",       "price": 2500, "emoji": "🎅",   "icon": "🎅"},
    {"key": "alien",     "name": "Alien",           "price": 3000, "emoji": "👽",   "icon": "👽"},
]


def _get_user_skins(user_id: int) -> list[str]:
    from data.database import _fetchall as _fa
    rows = _fa("SELECT skin_key FROM user_skins WHERE user_id=?", [user_id])
    return [r["skin_key"] for r in rows]


@app.get("/shop/{telegram_id}")
def get_shop(telegram_id: int):
    user = get_user(telegram_id)
    if not user:
        return {"ok": False}
    owned = set(_get_user_skins(telegram_id))
    owned.add("default")  # toujours owned
    active = user.get("active_skin") or "default"
    return {
        "ok": True,
        "balance": int(user.get("coins") or 0),
        "active_skin": active,
        "skins": [
            {**s, "owned": s["key"] in owned, "active": s["key"] == active}
            for s in SKINS
        ],
    }


@app.post("/shop/buy")
async def shop_buy(request: Request):
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    skin_key = body.get("skin_key")
    skin = next((s for s in SKINS if s["key"] == skin_key), None)
    if not skin:
        return {"ok": False, "error": "Skin inconnu"}
    if skin["price"] <= 0:
        return {"ok": False, "error": "Skin gratuit"}
    # Déjà possédé ?
    if skin_key in _get_user_skins(telegram_id):
        return {"ok": False, "error": "Tu possèdes déjà ce skin"}
    if not try_debit_coins(telegram_id, skin["price"], f"Achat skin {skin['name']}"):
        return {"ok": False, "error": f"Solde insuffisant ({get_coins(telegram_id)} 🪙)"}
    _execute("INSERT OR IGNORE INTO user_skins (user_id, skin_key) VALUES (?, ?)",
             [telegram_id, skin_key])
    _execute("UPDATE users SET active_skin=? WHERE user_id=?", [skin_key, telegram_id])
    await _broadcast(build_snapshot())
    return {"ok": True, "balance": get_coins(telegram_id), "active_skin": skin_key}


@app.post("/shop/equip")
async def shop_equip(request: Request):
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    skin_key = body.get("skin_key")
    if skin_key != "default" and skin_key not in _get_user_skins(telegram_id):
        return {"ok": False, "error": "Tu ne possèdes pas ce skin"}
    _execute("UPDATE users SET active_skin=? WHERE user_id=?", [skin_key, telegram_id])
    await _broadcast(build_snapshot())
    return {"ok": True, "active_skin": skin_key}


# ── Roue de la fortune ────────────────────────────────────────────────────────

# (poids, montant_coins, label). Le total des poids n'a pas besoin de faire 1.
# Le super jackpot a une probabilité ≈ 1/19M (comme le loto français).
SPIN_WHEEL = [
    (30.0,      10,      "10 🪙"),
    (25.0,      25,      "25 🪙"),
    (20.0,      50,      "50 🪙"),
    (15.0,      100,     "100 🪙"),
    (7.0,       250,     "250 🪙"),
    (3.0,       1000,    "🎉 JACKPOT 1000 🪙"),
    (5.24e-6,   1_000_000, "🌟 SUPER JACKPOT 1 000 000 🪙"),
]


def _can_spin_today(user) -> bool:
    last = (user or {}).get("last_spin_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return last_dt.date() < datetime.now(timezone.utc).date()
    except Exception:
        return True


@app.get("/spin/{telegram_id}")
def get_spin_status(telegram_id: int):
    """Retourne si l'utilisateur peut tourner la roue aujourd'hui."""
    user = get_user(telegram_id)
    if not user:
        return {"ok": False}
    return {"ok": True, "can_spin": _can_spin_today(user), "wheel": [{"coins": w[1], "label": w[2]} for w in SPIN_WHEEL]}


@app.post("/spin")
async def post_spin(request: Request):
    """Fait tourner la roue : crédite des coins. 1x/jour."""
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    if not _can_spin_today(user):
        return {"ok": False, "error": "Déjà tourné aujourd'hui — reviens demain ! 🌙"}
    # Tirage pondéré
    import random
    total_w = sum(w[0] for w in SPIN_WHEEL)
    n = random.uniform(0, total_w)
    cumul = 0.0
    winner_idx = 0
    for i, w in enumerate(SPIN_WHEEL):
        cumul += w[0]
        if n <= cumul:
            winner_idx = i
            break
    weight, coins, label = SPIN_WHEEL[winner_idx]
    add_coins(telegram_id, coins, f"Roue de la fortune : {label}")
    _execute("UPDATE users SET last_spin_at=? WHERE user_id=?",
             [datetime.now(timezone.utc).isoformat(), telegram_id])
    _check_badges_for_user(telegram_id)
    return {
        "ok": True,
        "coins": coins,
        "label": label,
        "balance": get_coins(telegram_id),
        "index": winner_idx,
    }


# ── Défis hebdomadaires ───────────────────────────────────────────────────────

# (key, label, target, reward_coins, stat_key)
WEEKLY_CHALLENGES = [
    {"key": "drinks_10",   "label": "🍺 Bois 10 verres cette semaine",   "target": 10, "reward": 200, "stat": "drinks_week"},
    {"key": "bets_win_3",  "label": "🎰 Gagne 3 paris cette semaine",     "target": 3,  "reward": 300, "stat": "bets_won_week"},
    {"key": "bj_played_5", "label": "🃏 Joue 5 parties de Blackjack",    "target": 5,  "reward": 150, "stat": "bj_played_week"},
]


def _current_iso_week() -> str:
    d = datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _week_stats(user_id: int) -> dict:
    """Calcule les stats de la semaine courante pour les défis."""
    from data.database import _fetchone as _fo
    week_start = "datetime('now', 'weekday 0', '-6 days', 'start of day')"
    drinks = _fo(
        f"SELECT COUNT(*) c FROM drink_logs WHERE user_id=? AND logged_at >= {week_start}",
        [user_id]
    )
    bets_won = _fo(
        f"SELECT COUNT(*) c FROM bets WHERE winner_id=? AND status='settled' AND created_at >= {week_start}",
        [user_id]
    )
    bj_played = _fo(
        f"""SELECT COUNT(*) c FROM blackjack_hands_history
            WHERE user_id=? AND finished_at >= {week_start}""",
        [user_id]
    )
    return {
        "drinks_week":    int((drinks or {}).get("c") or 0),
        "bets_won_week":  int((bets_won or {}).get("c") or 0),
        "bj_played_week": int((bj_played or {}).get("c") or 0),
    }


@app.get("/challenges/{telegram_id}")
def get_challenges(telegram_id: int):
    """Retourne les 3 défis de la semaine avec progress et état claimed."""
    from data.database import _fetchall as _fa
    user = get_user(telegram_id)
    if not user:
        return {"ok": False}
    stats = _week_stats(telegram_id)
    week = _current_iso_week()
    claims = _fa(
        "SELECT challenge_key FROM challenge_claims WHERE user_id=? AND week_iso=?",
        [telegram_id, week]
    )
    claimed_keys = {c["challenge_key"] for c in claims}
    out = []
    for c in WEEKLY_CHALLENGES:
        prog = stats.get(c["stat"], 0)
        out.append({
            "key": c["key"],
            "label": c["label"],
            "target": c["target"],
            "reward": c["reward"],
            "progress": min(prog, c["target"]),
            "completed": prog >= c["target"],
            "claimed": c["key"] in claimed_keys,
        })
    return {"ok": True, "week": week, "challenges": out}


@app.post("/challenges/claim")
async def claim_challenge(request: Request):
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    key = body.get("key")
    if not telegram_id or not key:
        return {"ok": False, "error": "Non authentifié"}
    challenge = next((c for c in WEEKLY_CHALLENGES if c["key"] == key), None)
    if not challenge:
        return {"ok": False, "error": "Défi inconnu"}
    week = _current_iso_week()
    from data.database import _fetchone as _fo
    already = _fo(
        "SELECT 1 FROM challenge_claims WHERE user_id=? AND challenge_key=? AND week_iso=?",
        [telegram_id, key, week]
    )
    if already:
        return {"ok": False, "error": "Déjà réclamé"}
    stats = _week_stats(telegram_id)
    if stats.get(challenge["stat"], 0) < challenge["target"]:
        return {"ok": False, "error": "Défi non complété"}
    _execute(
        "INSERT INTO challenge_claims (user_id, challenge_key, week_iso) VALUES (?, ?, ?)",
        [telegram_id, key, week]
    )
    add_coins(telegram_id, challenge["reward"], f"Défi hebdo : {challenge['label']}")
    return {"ok": True, "reward": challenge["reward"], "balance": get_coins(telegram_id)}


# ── Mode discret (privacy toggle) ─────────────────────────────────────────────

@app.post("/profile/discreet")
async def toggle_discreet(request: Request):
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    val = 1 if body.get("on") else 0
    _execute("UPDATE users SET discreet_mode=? WHERE user_id=?", [val, telegram_id])
    await _broadcast(build_snapshot())
    return {"ok": True, "discreet": bool(val)}


@app.get("/recap/{telegram_id}")
def get_recap(telegram_id: int):
    """Récap de la dernière "vraie soirée" : on prend tous les verres du user
    et on identifie le bloc contigu le plus récent (sans gap > 6h). Ça évite
    qu'une session jamais fermée s'étende sur plusieurs jours."""
    from collections import Counter
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    # Tous les verres du user (limité à 200 lignes pour éviter une grosse charge)
    all_rows = _fetchall(
        "SELECT drink_key, alc_grams, logged_at FROM drink_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 200",
        [telegram_id]
    )
    if not all_rows:
        return {"ok": True, "has_session": False}
    # Identifie la "vraie soirée" = bloc contigu avec moins de SESSION_TIMEOUT_HOURS
    # entre deux verres successifs (parcouru du plus récent vers le plus ancien).
    gap_sec = SESSION_TIMEOUT_HOURS * 3600
    real_session_desc = []
    last_t = None
    for d in all_rows:
        try:
            t = datetime.fromisoformat(d["logged_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_t is None or (last_t - t).total_seconds() < gap_sec:
            real_session_desc.append(d)
            last_t = t
        else:
            break
    drinks_rows = list(reversed(real_session_desc))
    if not drinks_rows:
        return {"ok": True, "has_session": False}
    # On synthétise un "sess" virtuel basé sur la première heure de la soirée
    sess = {
        "started_at": drinks_rows[0]["logged_at"],
        "active": 1 if last_t and (datetime.now(timezone.utc) - last_t).total_seconds() < gap_sec else 0,
    }
    weight_kg = user.get("weight_kg") or 70
    gender = user.get("gender", "homme")
    peak_bac = 0.0
    peak_time = None
    cumul = []
    for d in drinks_rows:
        t = datetime.fromisoformat(d["logged_at"]).replace(tzinfo=timezone.utc)
        cumul.append((d["alc_grams"], t))
        b = total_bac(cumul, weight_kg, gender, t)
        if b > peak_bac:
            peak_bac = b
            peak_time = t
    drink_counts = Counter(d["drink_key"] for d in drinks_rows)
    top_drinks = [
        {"drink_key": k, "count": c} for k, c in drink_counts.most_common(3)
    ]
    # Coins gagnés depuis le début de la session
    txs = _fetchall(
        "SELECT amount FROM transactions WHERE user_id=? AND created_at >= ?",
        [telegram_id, sess["started_at"]]
    )
    coins_diff = sum(int(t["amount"] or 0) for t in txs)
    duration_h = None
    try:
        start_dt = datetime.fromisoformat(sess["started_at"]).replace(tzinfo=timezone.utc)
        last_dt = cumul[-1][1] if cumul else datetime.now(timezone.utc)
        duration_h = round((last_dt - start_dt).total_seconds() / 3600, 1)
    except Exception:
        pass
    return {
        "ok": True,
        "has_session": True,
        "username": user.get("username"),
        "started_at": sess["started_at"],
        "active": bool(sess.get("active")),
        "nb_drinks": len(drinks_rows),
        "peak_bac": round(peak_bac, 2),
        "peak_time": peak_time.isoformat() if peak_time else None,
        "top_drinks": top_drinks,
        "coins_diff": coins_diff,
        "duration_h": duration_h,
    }


@app.get("/locations/{telegram_id}")
def get_locations(telegram_id: int):
    """Renvoie les positions [{username, lat, lon, bac}] uniquement pour les
    users que telegram_id suit (+ lui-même + admin). Évite de fuiter les GPS
    de tous les users à n'importe quel client WebSocket."""
    user = get_user(telegram_id)
    if not user:
        return []
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    following_ids = set(get_following(telegram_id))
    visible_ids = following_ids | {telegram_id}
    if admin_id:
        visible_ids.add(admin_id)
    drinks_by_user = get_all_active_drinks()
    now = datetime.now(timezone.utc)
    result = []
    for u in get_all_users():
        uid = u.get("user_id") or u.get("telegram_id")
        if uid not in visible_ids:
            continue
        # Mode discret : cache la position sur la carte (mais le user reste
        # visible sur Live). Le user lui-même n'est pas concerné.
        if u.get("discreet_mode") and uid != telegram_id:
            continue
        if not u.get("latitude") or not u.get("longitude"):
            continue
        drinks = drinks_by_user.get(uid, [])
        bac = total_bac(drinks, u["weight_kg"], u["gender"], now) if drinks else 0.0
        if bac <= 0:
            continue
        result.append({
            "username": u["username"],
            "lat": u["latitude"],
            "lon": u["longitude"],
            "bac": round(bac, 3),
            "nb_drinks": len(drinks),
            "sober_in_h": round(sober_in_hours(bac), 1),
        })
    return result


@app.get("/alltime")
def get_alltime():
    now = time.time()
    if _alltime_cache["data"] is not None and now - _alltime_cache["ts"] < ALLTIME_CACHE_TTL:
        return _alltime_cache["data"]
    data = get_all_time_stats()
    _alltime_cache["data"] = data
    _alltime_cache["ts"] = now
    return data


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

        # Calls DB sync wrapped dans asyncio.to_thread pour ne pas bloquer
        # l'event loop pendant la latence HTTP Turso.
        active_bets = await asyncio.to_thread(get_active_bets)
        if not active_bets:
            continue
        users_list = await asyncio.to_thread(get_all_users)
        users = {u["telegram_id"]: u for u in users_list}

        for bet in active_bets:
            if not bet.get("end_time"):
                continue
            if current_time < bet["end_time"]:
                continue

            from data.database import get_session_drinks
            uid1, uid2 = bet["challenger_id"], bet["opponent_id"]

            if bet["bet_type"] == "verres":
                drinks1 = await asyncio.to_thread(get_session_drinks, uid1)
                drinks2 = await asyncio.to_thread(get_session_drinks, uid2)
                count1, count2 = len(drinks1), len(drinks2)
                winner_id = uid1 if count1 >= count2 else uid2
                loser_id = uid2 if winner_id == uid1 else uid1
                detail = f"({count1} vs {count2} verres)"

            elif bet["bet_type"] == "ivre":
                from core.widmark import total_bac as _total_bac
                from data.database import get_session_drinks
                u1 = users.get(uid1, {})
                u2 = users.get(uid2, {})
                drinks1 = await asyncio.to_thread(get_session_drinks, uid1)
                drinks2 = await asyncio.to_thread(get_session_drinks, uid2)
                now_utc = datetime.now(timezone.utc)
                bac1 = _total_bac(drinks1, u1.get("weight_kg", 70), u1.get("gender", "homme"), now_utc)
                bac2 = _total_bac(drinks2, u2.get("weight_kg", 70), u2.get("gender", "homme"), now_utc)
                winner_id = uid1 if bac1 >= bac2 else uid2
                loser_id = uid2 if winner_id == uid1 else uid1
                detail = f"({bac1:.2f} vs {bac2:.2f} g/L)"

            else:
                continue

            await asyncio.to_thread(settle_bet, bet["id"], winner_id)
            winner = users.get(winner_id) or await asyncio.to_thread(get_user, winner_id)
            loser = users.get(loser_id) or await asyncio.to_thread(get_user, loser_id)
            amount = bet["amount"]
            await asyncio.to_thread(add_coins, winner_id, amount * 2, f"Pari gagné contre {loser['username']}")
            _award_bet_win(winner_id)

            msg = (
                f"🏁 *Résultat du pari !* {detail}\n\n"
                f"🏆 Gagnant : *{winner['username']}* +{amount} 🪙\n"
                f"💸 Perdant : *{loser['username']}* -{amount} 🪙"
            )

            if _TG_NOTIFS:
                for uid in [uid1, uid2]:
                    try:
                        await _bot_app.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
                    except Exception:
                        pass


@app.get("/history")
def get_history():
    """Pour chaque user, renvoie tous les verres des dernières 24h
    (peu importe que la session soit encore active ou pas). Sert au
    graphique Historique côté frontend."""
    from data.database import _fetchall as _fa
    users = get_all_users()
    # Récupère tous les verres des dernières 24h en une seule query
    all_drinks = _fa(
        """SELECT user_id, drink_key, alc_grams, logged_at
           FROM drink_logs
           WHERE logged_at >= datetime('now','-24 hours')
           ORDER BY user_id, logged_at"""
    )
    drinks_by_uid: dict[int, list] = {}
    for r in all_drinks:
        try:
            t = datetime.fromisoformat(r["logged_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            uid = r.get("user_id") or r.get("telegram_id")
            if uid is None:
                continue
            drinks_by_uid.setdefault(uid, []).append({
                "t": t.isoformat(),
                "alc_g": r["alc_grams"],
                "drink_key": r["drink_key"],
            })
        except Exception:
            continue
    result = []
    for user in users:
        if user.get("discreet_mode"):
            continue
        uid = user.get("telegram_id") or user.get("user_id")
        points = drinks_by_uid.get(uid, [])
        # On inclut tous les users (même sans points) → frontend affiche "Aucun verre"
        result.append({
            "username": user["username"],
            "weight_kg": user["weight_kg"],
            "gender": user["gender"],
            "points": points,
        })
    return result


@app.get("/coins")
def get_coins_endpoint():
    # Avant : 1 + N queries Turso (1 par user pour les transactions). Sur 50
    # users = 51 calls HTTP en série = 10-20s par appel. Maintenant : 1 query.
    # Les transactions sont fetchées à la demande via /coins/{tid}/transactions
    # quand l'user clique sur quelqu'un dans le classement.
    def _build():
        balances = get_all_balances()
        return [
            {
                "telegram_id": b["telegram_id"],
                "username":    b["username"],
                "coins":       b["coins"] or 0,
            }
            for b in balances
        ]
    return _cached("coins_all", 10.0, _build)


@app.get("/coins/{telegram_id}/transactions")
def get_coins_transactions(telegram_id: int, limit: int = 10):
    """Transactions récentes d'un user (fetchées à la demande)."""
    txs = get_transactions(telegram_id, limit)
    return [
        {"amount": t["amount"], "reason": t["reason"], "at": t["created_at"]}
        for t in txs
    ]


@app.get("/balance/{telegram_id}")
def get_user_balance(telegram_id: int):
    """Retourne le solde de pièces d'un utilisateur."""
    bal = get_coins(telegram_id)
    return {"ok": True, "balance": bal or 0}


@app.get("/users")
def get_all_users_endpoint():
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    users = get_all_users()
    result = []
    for u in users:
        uid = u.get("telegram_id") or u.get("user_id")
        if uid is None:
            continue
        result.append({
            "telegram_id": uid,
            "username":    u.get("username"),
            "gender":      u.get("gender", "homme"),
            "is_admin":    uid == admin_id,
            "is_premium":  uid == admin_id or bool(u.get("is_premium")),
            "level":       calc_level(int(u.get("xp") or 0)),
            "streak":      int(u.get("current_streak") or 0),
            "active_skin": u.get("active_skin") or "default",
        })
    return result

@app.get("/following/{telegram_id}")
def get_following_endpoint(telegram_id: int):
    return {"following": get_following(telegram_id)}

@app.get("/followers/{telegram_id}")
def get_followers_endpoint(telegram_id: int):
    return {"followers": get_followers(telegram_id)}

@app.post("/follow")
async def follow_endpoint(request: Request):
    body = await request.json()
    follower_id  = _resolve_user(request, body, "follower_id")
    following_id = body.get("following_id")
    if not follower_id or not following_id:
        return {"ok": False, "error": "Missing IDs"}
    follow_user(follower_id, following_id)
    _check_badges_for_user(follower_id)
    _check_badges_for_user(int(following_id))
    # Notif push à la personne suivie
    follower = get_user(int(follower_id))
    if follower:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            _PUSH_EXECUTOR, _send_push, int(following_id),
            "👤 Nouveau follower",
            f"{follower['username']} vient de s'abonner à toi sur Drunk !",
            "/?tab=amis"
        )
    return {"ok": True}

@app.post("/unfollow")
async def unfollow_endpoint(request: Request):
    body = await request.json()
    follower_id  = _resolve_user(request, body, "follower_id")
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
    uid_check = user.get("telegram_id") or user.get("user_id")
    if is_banned(uid_check):
        return {"ok": False, "error": "🚫 Ton compte a été banni par un administrateur."}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    uid = user.get("telegram_id") or user.get("user_id")
    is_adm = uid == admin_id
    token = create_auth_session(uid)
    return {
        "ok": True,
        "telegram_id": uid,
        "username": user["username"],
        "is_admin": is_adm,
        "is_premium": is_adm or bool(user.get("is_premium")),
        "auth_token": token,
    }


SESSION_TIMEOUT_HOURS = 6   # Une soirée se termine après 6h sans verre


def _ensure_session(telegram_id: int):
    """Garantit qu'une session active existe pour l'utilisateur. Si la session
    existante n'a pas vu de verre depuis SESSION_TIMEOUT_HOURS, on la ferme
    automatiquement et on en ouvre une nouvelle. Permet de bien séparer les
    soirées : on rentre se coucher, on se réveille, c'est une nouvelle soirée
    dès le prochain verre. À la fermeture, on attribue le badge de soirée
    (petite_chauffe / bleu_bite / cuite_monumentale / coma_ethylique) selon
    le TAC max atteint et la présence de vomi."""
    sess = get_active_session(telegram_id)
    if sess:
        row = _fetchone(
            "SELECT MAX(logged_at) m FROM drink_logs WHERE session_id=?",
            [sess["id"]]
        )
        last = row and row.get("m")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() > SESSION_TIMEOUT_HOURS * 3600:
                    # Avant de fermer : attribuer le badge de soirée
                    try:
                        award_soiree_badge_if_eligible(telegram_id, sess["id"])
                    except Exception as e:
                        print(f"[soiree-badge] erreur: {e}")
                    end_session(telegram_id)
                    sess = None
            except Exception:
                pass
    if not sess:
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

    # Parrainage : si le body contient un referrer_id, on l'enregistre + XP/coins
    referrer_id = body.get("referrer_id")
    if referrer_id:
        try:
            referrer_id = int(referrer_id)
            ref_user = get_user(referrer_id)
            if ref_user and referrer_id != web_id:
                set_referrer(web_id, referrer_id)
                # Bonus pour parrain et filleul
                add_coins(referrer_id, 200, f"Parrainage de {username}")
                add_coins(web_id, 200, f"Bonus parrainage par {ref_user['username']}")
                db_add_xp(referrer_id, XP_REFERRAL)
                _check_badges_for_user(referrer_id)
        except Exception:
            pass

    token = create_auth_session(web_id)
    return {
        "ok": True,
        "telegram_id": web_id,
        "username": username,
        "is_admin": web_id == admin_id,
        "auth_token": token,
    }


# ── Modification de profil ────────────────────────────────────────────────────

@app.post("/update-profile")
async def update_profile_endpoint(request: Request):
    body          = await request.json()
    telegram_id   = _resolve_user(request, body)
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

    # Le mot de passe actuel est exigé uniquement pour changer le mot de passe.
    if new_password and not verify_password(user["username"], current_pwd):
        return {"ok": False, "error": "Mot de passe actuel incorrect"}

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
    telegram_id = _resolve_user(request, body)
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
    telegram_id = _resolve_user(request, body)
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
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_PUSH_EXECUTOR, _send_push, telegram_id, "🍺 Test Drunk", "Les notifications fonctionnent !", "/")
    return {"ok": True}

# ── Logger un verre depuis le web ─────────────────────────────────────────────

@app.post("/log-drink")
async def log_drink_web(request: Request):
    body        = await request.json()
    telegram_id = _resolve_user(request, body)
    drink_key   = body.get("drink_key")
    lat         = body.get("lat")
    lon         = body.get("lon")
    # logged_at_override : ISO datetime string fournie par le frontend quand
    # l'user rattrape un verre oublié plus tôt dans la soirée. Si présent,
    # on utilise ce timestamp au lieu de "maintenant" et on skip le cooldown.
    logged_at_override = body.get("logged_at")

    if not telegram_id or not drink_key:
        return {"ok": False, "error": "Paramètres manquants"}
    if drink_key not in DRINKS:
        return {"ok": False, "error": "Boisson inconnue"}

    # ─── CHEMIN CRITIQUE : 0 query DB (tout en mémoire) ─────────────────────
    real_now = datetime.now(timezone.utc)
    drink_ts: datetime
    is_past = False
    if logged_at_override:
        try:
            dt = datetime.fromisoformat(logged_at_override.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > real_now:
                return {"ok": False, "error": "L'heure doit être dans le passé"}
            if (real_now - dt).total_seconds() > 6 * 3600:
                return {"ok": False, "error": "Trop ancien (max 6h en arrière)"}
            drink_ts = dt
            is_past = True
        except Exception:
            return {"ok": False, "error": "Heure invalide"}
    else:
        drink_ts = real_now
        # Cooldown 30s (uniquement pour les verres en temps réel)
        last_dt = _last_drink_at.get(telegram_id)
        if last_dt and (real_now - last_dt).total_seconds() < 30:
            remaining = int(30 - (real_now - last_dt).total_seconds()) + 1
            return {"ok": False, "error": f"Attends encore {remaining}s avant le prochain verre"}
    # now_ts est conservé pour la compat avec le reste du code (heure du verre)
    now_ts = drink_ts

    # User : réutilise le map cached
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    user = users_by_id.get(telegram_id)
    if not user:
        # Fallback DB (très rare : 1er accès après restart serveur)
        user = get_user(telegram_id)
        if not user:
            return {"ok": False, "error": "Utilisateur introuvable"}

    drink = DRINKS[drink_key]
    g = alcohol_grams(drink.volume_ml, drink.abv)

    # Drinks de la session : cache mémoire (lazy-load DB seulement au 1er log)
    existing_drinks = _user_drinks_cache.get(telegram_id)
    # Si le dernier verre du cache est > 6h, on considère une nouvelle session
    # (cohérent avec _ensure_session) → on vide le cache pour repartir frais.
    if existing_drinks and (now_ts - existing_drinks[-1][1]).total_seconds() > 6 * 3600:
        existing_drinks = None
        _user_drinks_cache.pop(telegram_id, None)
    if existing_drinks is None:
        existing_drinks = get_session_drinks(telegram_id)
        # Même check : si les drinks DB sont vieux, on démarre frais
        if existing_drinks and (now_ts - existing_drinks[-1][1]).total_seconds() > 6 * 3600:
            existing_drinks = []
        _user_drinks_cache[telegram_id] = list(existing_drinks)
    is_first = len(existing_drinks) == 0

    # Compute le BAC + update cache mémoire ATOMIQUEMENT.
    # Si verre dans le passé : insertion dans l'ordre chronologique.
    if is_past:
        new_drinks = sorted(existing_drinks + [(g, drink_ts)], key=lambda d: d[1])
        # Pas de mise à jour de _last_drink_at : ce verre est dans le passé,
        # l'user doit pouvoir cliquer à nouveau sans cooldown si besoin.
    else:
        new_drinks = existing_drinks + [(g, drink_ts)]
        _last_drink_at[telegram_id] = drink_ts
    _user_drinks_cache[telegram_id] = new_drinks
    # BAC calculé à l'instant présent (real_now), pas à l'heure du verre :
    # ainsi le verre rattrapé dans le passé montre déjà son élimination partielle.
    bac = total_bac(new_drinks, user["weight_kg"], user["gender"], real_now)

    _invalidate_alltime_cache()
    _invalidate_cache("snapshot", "coins_all")

    # ─── BACKGROUND : INSERT DB + gamification + broadcast + notifs ─────────
    # Le user a déjà sa réponse. Tout ça tourne sans bloquer.
    async def _post_drink_work():
        try:
            # 1) DB write (essentiel, en premier). Tous les calls Turso sync
            # sont wrappés dans asyncio.to_thread pour ne pas bloquer l'event loop.
            await asyncio.to_thread(_ensure_session, telegram_id)
            # Si verre dans le passé : on passe drink_ts au log_drink pour
            # que la DB stocke l'heure réelle, pas datetime('now').
            db_logged_at = drink_ts if is_past else None
            inserted = await asyncio.to_thread(db_log_drink, telegram_id, drink_key, g, db_logged_at)
            if not inserted:
                print(f"[log_drink] INSERT failed pour user {telegram_id}")
                return
            # 2) Gamification + coins
            await asyncio.to_thread(add_coins, telegram_id, 5, f"Verre bu ({drink.name})")
            await asyncio.to_thread(_award_drink, telegram_id)
            await asyncio.to_thread(update_max_bac, telegram_id, bac)
            if lat is not None and lon is not None:
                try:
                    await asyncio.to_thread(update_location, telegram_id, float(lat), float(lon))
                except Exception:
                    pass
            # 3) Broadcast snapshot aux WS
            snapshot = await asyncio.to_thread(build_snapshot)
            await _broadcast(snapshot)

            # 3bis) Notif spéciale "cercle Max" : à CHAQUE verre de Maximelebg,
            # envoie un push aux 4 destinataires hardcoded avec un message
            # aléatoire. S'ajoute aux notifs normales (followers).
            if user.get("username") == "Maximelebg":
                import random
                msgs = [
                    "Max est en train de se faire péter le cul, ça envoie !",
                    "Max est en train de se faire limer l'oignon",
                    "Max est en train de se faire péter le sac de bille",
                ]
                targets = ["Allain", "Gab", "Brian", "Sarah"]
                name_to_tid = {u.get("username"): u.get("telegram_id") for u in users_by_id.values()}
                body_text = random.choice(msgs)
                push_loop = asyncio.get_event_loop()
                for tname in targets:
                    tid_t = name_to_tid.get(tname)
                    if tid_t:
                        push_loop.run_in_executor(
                            _PUSH_EXECUTOR, _send_push, tid_t,
                            "🚨 Max boit !", body_text, "/?tab=live"
                        )

            # 4) Notifs aux abonnés (premier verre uniquement, différé 30s)
            if is_first:
                gender = user.get("gender", "homme")
                le_la  = "la" if gender == "femme" else "le"
                name   = user["username"]
                followers = await asyncio.to_thread(get_followers, telegram_id)
                active_drinkers_map = await asyncio.to_thread(get_all_active_drinks)
                active_drinkers = set(active_drinkers_map.keys()) - {telegram_id}
                all_follows = await asyncio.to_thread(get_all_follows)
                follower_following: dict[int, set[int]] = {}
                for row in all_follows:
                    follower_following.setdefault(row["follower_id"], set()).add(row["following_id"])

                plan = []
                now_ts2 = datetime.now(timezone.utc)
                for fid in followers:
                    key = (telegram_id, fid)
                    last_fn = _first_drink_notified.get(key)
                    if last_fn and (now_ts2 - last_fn).total_seconds() < 8 * 3600:
                        continue
                    fid_following = follower_following.get(fid, set())
                    others = [oid for oid in fid_following if oid != telegram_id and oid in active_drinkers]
                    n_others = len(others)
                    if n_others == 0:
                        title = "🍺 Drunk"
                        body  = f"{name} est en train de se mettre des verres, rejoins {le_la} !"
                    elif n_others == 1:
                        other_user = users_by_id.get(others[0])
                        other_name = other_user["username"] if other_user else "quelqu'un"
                        title = "🍺 Drunk"
                        body  = (f"{name} et {other_name} sont en train de se péter le cabanon, "
                                 f"sers toi un verre en urgence !")
                    else:
                        title = "🍺 Drunk"
                        body  = f"{name} s'est également envoyé un godet, tu attends quoi toi ?"
                    plan.append((fid, key, title, body))

                async def _delayed_first_drink_notifs(drinker_id, plan):
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        return
                    loop = asyncio.get_event_loop()
                    now3 = datetime.now(timezone.utc)
                    for fid, key, title, body in plan:
                        _first_drink_notified[key] = now3
                        loop.run_in_executor(_PUSH_EXECUTOR, _send_push, fid, title, body, "/?tab=live")
                    _pending_first_drink_notifs.pop(drinker_id, None)

                old = _pending_first_drink_notifs.get(telegram_id)
                if old and not old.done():
                    old.cancel()
                if plan:
                    _pending_first_drink_notifs[telegram_id] = asyncio.create_task(
                        _delayed_first_drink_notifs(telegram_id, plan)
                    )
        except Exception as e:
            print(f"[post_drink_work] erreur (non-critique) : {e}")

    asyncio.create_task(_run_bg_limited(_post_drink_work()))

    # Réponse instantanée (0 query DB). Badges visibles au prochain refresh.
    return {
        "ok": True,
        "bac": round(bac, 3),
        "nb_drinks": len(new_drinks),
        "label": bac_label(bac),
        "new_badges": [],
    }


# ── Vomi (compté pour les badges de soirée + classement) ──────────────────────

@app.post("/vomi")
async def log_vomi_endpoint(request: Request):
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}
    # Assure qu'une session existe (idem que pour drink)
    _ensure_session(telegram_id)
    ok = log_vomi(telegram_id)
    if not ok:
        return {"ok": False, "error": "Pas de session active"}
    # Invalide les caches qui exposent les vomis
    _invalidate_cache(
        "snapshot", "coins_all",
        f"me:{telegram_id}", f"profile:{telegram_id}",
    )
    return {"ok": True}


# ── Annuler le dernier verre ──────────────────────────────────────────────────

@app.post("/undo-drink")
async def undo_drink_web(request: Request):
    body        = await request.json()
    telegram_id = _resolve_user(request, body)

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    # ─── CHEMIN CRITIQUE : 0 query DB (tout en mémoire) ─────────────────────
    # User : réutilise le map cached
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    user = users_by_id.get(telegram_id) or get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}

    # Vérifie qu'il y a un verre à retirer via le cache mémoire (fast path).
    # Si le cache n'a rien, on vérifie quand même la DB en background pour
    # gérer le cas du serveur fraichement redémarré.
    cached = _user_drinks_cache.get(telegram_id)
    if cached is not None and not cached:
        return {"ok": False, "error": "Aucun verre à annuler"}

    # Mise à jour cache mémoire : retire le dernier verre
    if cached:
        cached.pop()
    # Libère le cooldown — l'user peut recliquer immédiatement
    _last_drink_at.pop(telegram_id, None)
    # Annule toute notif FOMO en attente (cas où le drink était dans la
    # fenêtre 30s avant d'envoyer les push)
    pending = _pending_first_drink_notifs.get(telegram_id)
    if pending and not pending.done():
        pending.cancel()
        _pending_first_drink_notifs.pop(telegram_id, None)

    _invalidate_alltime_cache()
    _invalidate_cache("snapshot", "coins_all")

    # BAC calculé en mémoire à partir du cache mis à jour
    drinks_data = cached if cached is not None else []
    bac = total_bac(drinks_data, user["weight_kg"], user["gender"])

    # ─── BACKGROUND : DELETE DB + retour coins/XP + broadcast ───────────────
    async def _post_undo_work():
        try:
            deleted = await asyncio.to_thread(delete_last_drink, telegram_id)
            if not deleted:
                return
            await asyncio.to_thread(add_coins, telegram_id, -5, "Annulation verre")
            await asyncio.to_thread(
                _execute,
                "UPDATE users SET xp = MAX(0, COALESCE(xp,0) - ?) WHERE user_id=?",
                [XP_PER_DRINK, telegram_id]
            )
            snapshot = await asyncio.to_thread(build_snapshot)
            await _broadcast(snapshot)
        except Exception as e:
            print(f"[post_undo_work] erreur (non-critique) : {e}")

    asyncio.create_task(_run_bg_limited(_post_undo_work()))

    # Réponse instantanée
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
    telegram_id = _resolve_user(request, body)

    if not telegram_id:
        return {"ok": False, "error": "Non authentifié"}

    # Vide le cache mémoire immédiatement — réponse instantanée
    _user_drinks_cache.pop(telegram_id, None)
    _last_drink_at.pop(telegram_id, None)
    _invalidate_cache("snapshot")

    # Tout le DB work en background
    async def _post_reset_work():
        try:
            # Avant de fermer la session : attribuer le badge de soirée si éligible
            try:
                sess = await asyncio.to_thread(get_active_session, telegram_id)
                if sess:
                    await asyncio.to_thread(award_soiree_badge_if_eligible, telegram_id, sess["id"])
            except Exception as e:
                print(f"[reset/soiree-badge] erreur: {e}")
            await asyncio.to_thread(end_session, telegram_id)
            await asyncio.to_thread(start_session, telegram_id)
            snapshot = await asyncio.to_thread(build_snapshot)
            await _broadcast(snapshot)
        except Exception as e:
            print(f"[post_reset_work] erreur : {e}")

    asyncio.create_task(_run_bg_limited(_post_reset_work()))
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
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    player_data = []
    for p in players:
        u = users_by_id.get(p["telegram_id"])
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


@app.get("/blackjack/my-session/{telegram_id}")
def get_my_bj_session(telegram_id: int):
    """Retourne la session BJ active/waiting/finished la plus récente du joueur (pour reconnexion)."""
    from data.database import _fetchone as _fo
    row = _fo("""
        SELECT bs.* FROM blackjack_sessions bs
        JOIN blackjack_players bp ON bs.id = bp.session_id
        WHERE bp.telegram_id=? AND bp.status != 'left'
        ORDER BY bs.created_at DESC LIMIT 1
    """, [telegram_id])
    if not row:
        return {"ok": False}
    players = get_blackjack_players(row["id"])
    # Avant : 1 get_user(creator) + N get_user(player) → N+1.
    # Maintenant : 1 query cached pour tous les users, résolution en mémoire.
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    creator = users_by_id.get(row["creator_id"])
    player_data = []
    for p in players:
        u = users_by_id.get(p["telegram_id"])
        player_data.append({
            "telegram_id": p["telegram_id"],
            "username": u["username"] if u else str(p["telegram_id"]),
            "hand": json.loads(p["hand"]),
            "status": p["status"],
            "result": p["result"],
            "bet": p["bet"],
        })
    dealer_hand = json.loads(row["dealer_hand"])
    hide_dealer = row["status"] == "active"
    return {
        "ok": True,
        "token": row["token"],
        "status": row["status"],
        "creator_id": row["creator_id"],
        "creator": creator["username"] if creator else "?",
        "dealer_hand": ([dealer_hand[0], "?"] if dealer_hand else []) if hide_dealer else dealer_hand,
        "dealer_value": hand_value(dealer_hand) if not hide_dealer else None,
        "players": player_data,
    }


@app.get("/blackjack/sessions")
async def list_bj_sessions():
    """Liste toutes les sessions blackjack en attente ou actives."""
    # Avant : N+1 imbriqué — get_user pour chaque creator + chaque player.
    # 5 sessions × 3 players = 25 calls Turso. Maintenant : 1 cache + résolution mémoire.
    sessions = get_active_blackjack_sessions()
    if not sessions:
        return []
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    result = []
    for s in sessions:
        players = get_blackjack_players(s["id"])
        creator = users_by_id.get(s["creator_id"])
        result.append({
            "token":   s["token"],
            "status":  s["status"],
            "creator": creator["username"] if creator else "?",
            "creator_id": s["creator_id"],
            "players": [
                {
                    "telegram_id": p["telegram_id"],
                    "username": (users_by_id.get(p["telegram_id"]) or {}).get("username", "?"),
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
    telegram_id = _resolve_user(request, body)
    bet = int(body.get("bet", 50))
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    if bet <= 0:
        return {"ok": False, "error": "Mise invalide"}
    existing = get_blackjack_session_by_player(telegram_id)
    if existing:
        if existing["status"] == "active":
            return {"ok": False, "error": "Tu es déjà dans une partie active — rejoins-la !"}
        # Session en attente → la fermer et rembourser la mise du joueur
        players = get_blackjack_players(existing["id"])
        old_me = next((p for p in players if p["telegram_id"] == telegram_id), None)
        if old_me and old_me["bet"] > 0:
            add_coins(telegram_id, old_me["bet"], "Blackjack - remboursement ancienne table")
        update_blackjack_session(existing["id"], status="finished")
        await _bj_broadcast(existing["token"])
    # Débit atomique de la mise — empêche les double-spends parallèles
    if not try_debit_coins(telegram_id, bet, "Blackjack - mise"):
        return {"ok": False, "error": f"Solde insuffisant ({get_coins(telegram_id)} 🪙)"}
    token = secrets.token_urlsafe(8)
    session_id = create_blackjack_session(telegram_id, token)
    add_blackjack_player(session_id, telegram_id, bet)
    unlock_badge(telegram_id, "first_bj")
    return {"ok": True, "token": token}


@app.post("/blackjack/{token}/join-web")
async def bj_join_web(token: str, request: Request):
    """Rejoint une session blackjack depuis le web."""
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    bet = int(body.get("bet", 50))
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    players = get_blackjack_players(sess["id"])
    # Déjà dedans (et pas parti) ?
    me = next((p for p in players if p["telegram_id"] == telegram_id), None)
    if me and me["status"] != "left":
        return {"ok": True, "token": token}
    if bet <= 0:
        return {"ok": False, "error": "Mise invalide"}

    if sess["status"] == "waiting":
        # Salle d'attente → rejoindre normalement et payer la mise
        non_left = [p for p in players if p["status"] != "left"]
        if len(non_left) >= 4:
            return {"ok": False, "error": "Table complète (4 joueurs max)"}
        # Débit atomique avant d'ajouter le joueur — pas de double-spend possible
        if not try_debit_coins(telegram_id, bet, "Blackjack - mise"):
            return {"ok": False, "error": f"Solde insuffisant ({get_coins(telegram_id)} 🪙)"}
        if me:  # était "left" → réactiver
            update_blackjack_player(sess["id"], telegram_id,
                bet=bet, status="waiting", hand="[]", result=None)
        else:
            add_blackjack_player(sess["id"], telegram_id, bet)
        await _bj_broadcast(token)
        return {"ok": True, "token": token}

    if sess["status"] in ("active", "finished"):
        # Rejoindre comme waiting_next (pas de débit immédiat)
        active_count = len([p for p in players
                            if p["status"] not in ("left", "waiting_next", "waiting")])
        if active_count >= 4:
            return {"ok": False, "error": "Table complète (4 joueurs max)"}
        if me:  # était "left" → réactiver
            update_blackjack_player(sess["id"], telegram_id,
                bet=bet, status="waiting_next", hand="[]", result=None)
        else:
            add_blackjack_player(sess["id"], telegram_id, bet)
            update_blackjack_player(sess["id"], telegram_id, status="waiting_next")
        await _bj_broadcast(token)
        return {"ok": True, "token": token, "waiting_next": True}

    return {"ok": False, "error": "Partie terminée"}


@app.post("/blackjack/{token}/leave")
async def bj_leave(token: str, request: Request):
    """Quitte une table blackjack. Rembourse la mise si la partie n'a pas commencé."""
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Non connecté"}
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": True}
    players = get_blackjack_players(sess["id"])
    me = next((p for p in players if p["telegram_id"] == telegram_id), None)
    if not me:
        return {"ok": True}

    from data.database import _execute

    # ── Salle d'attente ──────────────────────────────────────────────────────
    if sess["status"] == "waiting":
        add_coins(telegram_id, me["bet"], "Blackjack - remboursement mise")
        _execute("DELETE FROM blackjack_players WHERE session_id=? AND telegram_id=?",
                 [sess["id"], telegram_id])
        remaining = [p for p in players if p["telegram_id"] != telegram_id]
        if not remaining or int(sess["creator_id"]) == int(telegram_id):
            update_blackjack_session(sess["id"], status="finished")
        await _bj_broadcast(token)
        return {"ok": True, "refunded": me["bet"]}

    # ── Partie active ─────────────────────────────────────────────────────────
    if sess["status"] == "active":
        if me["status"] == "waiting_next":
            # Pas encore débité → juste supprimer
            _execute("DELETE FROM blackjack_players WHERE session_id=? AND telegram_id=?",
                     [sess["id"], telegram_id])
        else:
            # playing / stand / bust / done → marquer left (mise perdue)
            update_blackjack_player(sess["id"], telegram_id, status="left")

        # Vérifier si tous les joueurs actifs ont fini
        players = get_blackjack_players(sess["id"])
        active = [p for p in players
                  if p["status"] not in ("waiting_next", "left", "waiting")]
        if not active:
            # Plus personne → fermer
            update_blackjack_session(sess["id"], status="finished")
            await _bj_broadcast(token)
        elif all(p["status"] in ("stand", "bust", "done") for p in active):
            await _resolve_hand(sess["id"], token)
        else:
            await _bj_broadcast(token)
        return {"ok": True, "refunded": 0}

    # ── Partie terminée ───────────────────────────────────────────────────────
    if sess["status"] == "finished":
        if me["status"] == "waiting_next":
            _execute("DELETE FROM blackjack_players WHERE session_id=? AND telegram_id=?",
                     [sess["id"], telegram_id])
        else:
            update_blackjack_player(sess["id"], telegram_id, status="left")
        await _bj_broadcast(token)
        return {"ok": True, "refunded": 0}

    return {"ok": True, "refunded": 0}


@app.post("/blackjack/{token}/start-web")
async def bj_start_web(token: str, request: Request):
    """Lance la partie (deal les cartes) depuis le web."""
    body = await request.json()
    caller_id = _resolve_user(request, body)
    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    if int(sess["creator_id"]) != int(caller_id):
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
            _award_bj_win(p["telegram_id"])

    # Si tous done immédiatement (tous blackjack) → résoudre et terminer
    players = get_blackjack_players(sess["id"])
    if all(p["status"] in ("stand", "bust", "done") for p in players):
        await _resolve_hand(sess["id"], token)
    else:
        await _bj_broadcast(token)
    return {"ok": True}


@app.post("/blackjack/{token}/action-web")
async def bj_action_web(token: str, request: Request):
    """Hit ou stand depuis le web (fallback HTTP si WebSocket non dispo)."""
    body = await request.json()
    player_id = _resolve_user(request, body)
    action    = body.get("action")

    if action not in ("hit", "stand"):
        return {"ok": False, "error": "Action invalide"}

    sess = get_blackjack_session(token)
    if not sess:
        return {"ok": False, "error": "Session introuvable"}
    if sess["status"] != "active":
        return {"ok": False, "error": "Partie non active"}

    players = get_blackjack_players(sess["id"])
    player  = next((p for p in players if p["telegram_id"] == player_id), None)
    if not player:
        return {"ok": False, "error": "Joueur introuvable"}
    if player["status"] != "playing":
        return {"ok": False, "error": "Ce n'est pas ton tour"}

    hand        = json.loads(player["hand"])
    deck        = json.loads(sess["deck"])
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

    # Check if all active players done (exclure waiting_next / left)
    players = get_blackjack_players(sess["id"])
    active  = [p for p in players
               if p["status"] not in ("waiting_next", "left", "waiting")]
    all_done = bool(active) and all(
        p["status"] in ("stand", "bust", "done") for p in active
    )

    if all_done:
        await _resolve_hand(sess["id"], token)
    else:
        await _bj_broadcast(token)
    return {"ok": True}


# ── Paris web endpoints ────────────────────────────────────────────────────────

@app.get("/bets/user/{telegram_id}")
async def get_bets_user(telegram_id: int):
    """Retourne les paris d'un utilisateur."""
    # Avant : N+1 (3 queries get_user par pari → 30 paris = 91 calls Turso).
    # Maintenant : 1 query pour les paris + 1 query pour tous les users en
    # cache 30s (les usernames ne changent quasi jamais), résolution en mémoire.
    bets = get_user_bets(telegram_id)
    if not bets:
        return []
    users_by_id = _cached("users_by_id_map", 30.0,
                          lambda: {u["telegram_id"]: u for u in get_all_users()})
    result = []
    for b in bets:
        ch = users_by_id.get(b["challenger_id"])
        op = users_by_id.get(b["opponent_id"])
        wn = users_by_id.get(b["winner_id"]) if b.get("winner_id") else None
        result.append({
            **b,
            "challenger_name": ch["username"] if ch else "?",
            "opponent_name":   op["username"] if op else "?",
            "winner_name":     wn["username"] if wn else None,
        })
    return result


@app.post("/bets/create")
async def create_bet_web(request: Request):
    """Crée un pari depuis le web."""
    body          = await request.json()
    challenger_id = _resolve_user(request, body, "challenger_id")
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

    if get_coins(opponent["telegram_id"]) < amount:
        return {"ok": False, "error": f"{opponent['username']} n'a pas assez de 🪙"}

    # Escrow : on débite le challenger IMMÉDIATEMENT et on stocke l'argent
    # dans le pari. À la résolution, le winner récupère 2x amount. En cas de
    # refus, on rembourse.
    if not try_debit_coins(challenger_id, amount, f"Pari créé contre {opponent['username']}"):
        return {"ok": False, "error": f"Solde insuffisant ({get_coins(challenger_id)} 🪙)"}

    bet_id = create_bet(challenger_id, opponent["telegram_id"], bet_type, amount, end_time or None)
    unlock_badge(challenger_id, "first_pari")

    # Notif push à l'adversaire
    type_labels = {"verres": "plus de verres", "ivre": "TAC le plus haut", "coinflip": "pile ou face"}
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_PUSH_EXECUTOR, _send_push,
        opponent["telegram_id"],
        f"🎰 Pari de {challenger['username']}",
        f"{amount} 🪙 sur {type_labels[bet_type]} — accepte ou refuse !",
        "/?tab=menu"
    )

    return {"ok": True, "bet_id": bet_id}


@app.post("/bets/accept")
async def accept_bet_web(request: Request):
    body       = await request.json()
    telegram_id = _resolve_user(request, body)
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

    # Escrow opponent : on débite sa mise (le challenger a déjà été débité à la création)
    if not try_debit_coins(telegram_id, bet["amount"], "Pari accepté"):
        return {"ok": False, "error": f"Solde insuffisant ({get_coins(telegram_id)} 🪙)"}

    # Coinflip : résoudre immédiatement → winner reçoit les 2 mises
    if bet["bet_type"] == "coinflip":
        import random
        winner_id = random.choice([bet["challenger_id"], bet["opponent_id"]])
        accept_bet(bet_id)
        settle_bet(bet_id, winner_id)
        add_coins(winner_id, bet["amount"] * 2, "Coinflip gagné")
        _award_bet_win(winner_id)
        winner = get_user(winner_id)
        return {"ok": True, "coinflip": True, "winner": winner["username"] if winner else "?"}

    accept_bet(bet_id)
    return {"ok": True, "coinflip": False}


@app.post("/bets/relance")
async def relance_bet(request: Request):
    """Envoie une notif push de relance à l'opponent d'un pari en attente."""
    body        = await request.json()
    telegram_id = _resolve_user(request, body)
    bet_id      = body.get("bet_id")
    if not telegram_id or not bet_id:
        return {"ok": False, "error": "Données manquantes"}
    bet = get_bet(bet_id)
    if not bet:
        return {"ok": False, "error": "Pari introuvable"}
    if bet["challenger_id"] != telegram_id:
        return {"ok": False, "error": "Seul le créateur peut relancer"}
    if bet["status"] != "pending":
        return {"ok": False, "error": "Pari déjà traité"}
    # Rate limit anti-spam : max 1 relance / heure par pari
    from data.database import _execute as _ex
    try:
        _ex("ALTER TABLE bets ADD COLUMN last_relance_at TEXT", [])
    except Exception:
        pass
    last = _fetchone("SELECT last_relance_at FROM bets WHERE id=?", [bet_id])
    if last and last.get("last_relance_at"):
        try:
            last_dt = datetime.fromisoformat(last["last_relance_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:
                return {"ok": False, "error": "Tu as déjà relancé ce pari récemment"}
        except Exception:
            pass
    _execute("UPDATE bets SET last_relance_at=? WHERE id=?",
             [datetime.now(timezone.utc).isoformat(), bet_id])
    # Push à l'opponent
    challenger = get_user(telegram_id)
    if challenger:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(_PUSH_EXECUTOR, _send_push,
            bet["opponent_id"],
            "⏰ Pari en attente !",
            f"{challenger['username']} te relance : {bet['amount']} 🪙. Accepte ou refuse !",
            "/?tab=menu"
        )
    return {"ok": True}


@app.post("/bets/refuse")
async def refuse_bet_web(request: Request):
    body        = await request.json()
    telegram_id = _resolve_user(request, body)
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
    # Rembourse le challenger qui avait été débité à la création
    add_coins(bet["challenger_id"], bet["amount"], "Pari refusé - remboursement")
    return {"ok": True}


@app.get("/profile/{telegram_id}")
def get_profile(telegram_id: int):
    """Retourne le profil complet d'un utilisateur en un seul fetch :
    user info + level + streak + badges + followers + following + session drinks
    + bj stats. Permet à openProfile() côté frontend de tout afficher
    immédiatement au lieu de faire 6 round-trips parallèles."""
    user = get_user(telegram_id)
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    uid = user.get("telegram_id") or user.get("user_id")
    follows = get_profile_follows(telegram_id)
    bj      = get_blackjack_stats(telegram_id)
    xp      = int(user.get("xp") or 0)
    lvl     = calc_level(xp)
    badges_unlocked = get_user_badges(telegram_id)
    streak  = get_streak(telegram_id)
    session_detail = get_session_drinks_detail(telegram_id)
    return {
        "ok": True,
        "telegram_id":   uid,
        "username":      user["username"],
        "gender":        user.get("gender", "homme"),
        "is_admin":      uid == admin_id,
        "is_premium":    uid == admin_id or bool(user.get("is_premium")),
        "active_skin":   user.get("active_skin") or "default",
        "coins":         int(user.get("coins") or 0),
        "level":         lvl,
        "badges_unlocked": badges_unlocked,
        "streak":        streak,
        "session_drinks": [
            {"drink_key": d["drink_key"], "logged_at": d["logged_at"]}
            for d in session_detail
        ],
        "bj": bj,
        "soiree_badges": get_soiree_badges_counts(uid),
        "vomis_total": count_vomis_total(uid),
        **follows,
    }


@app.get("/session/{telegram_id}")
def get_user_session_drinks(telegram_id: int):
    """Verres de la session active d'un utilisateur, avec heures."""
    detail = get_session_drinks_detail(telegram_id)
    return {
        "drinks": [
            {"drink_key": d["drink_key"], "logged_at": d["logged_at"]}
            for d in detail
        ]
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
    tid    = _resolve_user(request, body)
    avatar = body.get("avatar", "")
    if not tid or not avatar:
        return {"ok": False, "error": "Données manquantes"}
    user = get_user(int(tid))
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    # Limite à ~80 Ko en base64 (≈ 60 Ko image réelle)
    if len(avatar) > 100_000:
        return {"ok": False, "error": "Image trop grande (max 60 Ko)"}
    # Whitelist stricte : pas de SVG (peut contenir du JS exécuté quand l'avatar
    # est rendu via <img src=...>), pas de data:text/*, etc.
    _ALLOWED_AVATAR_PREFIXES = (
        "data:image/png;base64,",
        "data:image/jpeg;base64,",
        "data:image/jpg;base64,",
        "data:image/webp;base64,",
        "data:image/gif;base64,",
    )
    if not any(avatar.startswith(p) for p in _ALLOWED_AVATAR_PREFIXES):
        return {"ok": False, "error": "Format invalide (png/jpeg/webp uniquement)"}
    set_avatar(int(tid), avatar)
    _check_badges_for_user(int(tid))
    return {"ok": True}


@app.get("/bets/stats/all")
def get_all_bets_stats():
    """Stats de paris (settled uniquement) pour le classement."""
    return _cached("bets_stats_all", 15.0, _build_bets_stats_all)


def _build_bets_stats_all():
    from data.database import _fetchall as _fa
    bets  = _fa("SELECT * FROM bets WHERE status='settled'")
    users = {u["telegram_id"]: u for u in get_all_users()}
    stats: dict[int, dict] = {}
    for b in bets:
        for uid in [b["challenger_id"], b["opponent_id"]]:
            if uid not in stats:
                u = users.get(uid)
                stats[uid] = {
                    "telegram_id": uid,
                    "username":    u["username"] if u else "?",
                    "played": 0, "won": 0, "lost": 0,
                    "coins_gained": 0,
                }
            stats[uid]["played"] += 1
            if b["winner_id"] == uid:
                stats[uid]["won"]         += 1
                stats[uid]["coins_gained"] += b["amount"]
            else:
                stats[uid]["lost"]         += 1
                stats[uid]["coins_gained"] -= b["amount"]
    result = list(stats.values())
    result.sort(key=lambda x: (-x["won"], x["lost"]))
    return result


@app.get("/blackjack/stats/all")
def get_all_bj_stats():
    """Stats blackjack de tous les utilisateurs (classement)."""
    # Avant : N+1 (1 query par user). Maintenant : cache 15s + 1 query par user.
    # Le cache absorbe le burst de plusieurs users qui ouvrent l'app en même temps.
    def _build():
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
    return _cached("bj_stats_all", 15.0, _build)


async def _resolve_hand(sess_id: int, token: str):
    """Résout la main du croupier et distribue les gains. Passe la session en 'finished'."""
    try:
        sess = get_blackjack_session_by_id(sess_id)
        if not sess:
            return
        # Idempotence : si déjà terminé, juste broadcaster l'état actuel
        if sess["status"] == "finished":
            await _bj_broadcast(token)
            return
        players = get_blackjack_players(sess_id)
        # Seuls les joueurs qui ont joué cette main (pas waiting_next / left / waiting)
        hand_players = [p for p in players if p["status"] in ("stand", "bust", "done")]
        dealer_hand = json.loads(sess["dealer_hand"])
        deck = json.loads(sess["deck"])
        # Le croupier tire jusqu'à 17 (s'arrête si le deck est vide par sécurité)
        while hand_value(dealer_hand) < 17 and deck:
            dealer_hand.append(deck.pop())
        dealer_val = hand_value(dealer_hand)
        update_blackjack_session(sess_id,
            dealer_hand=json.dumps(dealer_hand),
            deck=json.dumps(deck),
            status="finished",
        )
        for p in hand_players:
            if p["status"] == "bust":
                continue
            p_val = hand_value(json.loads(p["hand"]))
            bet = p["bet"]
            if dealer_val > 21 or p_val > dealer_val:
                add_coins(p["telegram_id"], bet * 2, "Blackjack gagné")
                update_blackjack_player(sess_id, p["telegram_id"], result="win")
                _award_bj_win(p["telegram_id"])
            elif p_val == dealer_val:
                add_coins(p["telegram_id"], bet, "Blackjack égalité")
                update_blackjack_player(sess_id, p["telegram_id"], result="push")
            else:
                update_blackjack_player(sess_id, p["telegram_id"], result="lose")
    except Exception as e:
        print(f"[_resolve_hand error] {e}")
        # En cas d'erreur partielle, forcer 'finished' et broadcaster quand même
        try:
            update_blackjack_session(sess_id, status="finished")
        except Exception:
            pass
    await _bj_broadcast(token)


async def _bj_broadcast(token: str):
    """Diffuse l'état actuel d'une session blackjack à tous ses clients WS."""
    # Calls Turso sync wrappés dans threads pour ne pas bloquer l'event loop
    sess = await asyncio.to_thread(get_blackjack_session, token)
    if not sess:
        return
    players = await asyncio.to_thread(get_blackjack_players, sess["id"])
    dealer_hand = json.loads(sess["dealer_hand"])
    hide_dealer = sess["status"] == "active"

    # Cache users (évite N get_user calls par broadcast)
    users_by_id = await asyncio.to_thread(
        _cached, "users_by_id_map", 30.0,
        lambda: {u["telegram_id"]: u for u in get_all_users()}
    )
    player_data = []
    for p in players:
        u = users_by_id.get(p["telegram_id"])
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

    # Envoi en parallèle avec timeout par client (idem _broadcast principal)
    payload = json.dumps(state)
    clients = list(_bj_clients.get(token, set()))
    if not clients:
        return
    async def _send_one(ws):
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=3.0)
            return None
        except Exception:
            return ws
    results = await asyncio.gather(*[_send_one(ws) for ws in clients], return_exceptions=True)
    bj_set = _bj_clients.get(token, set())
    for r in results:
        if isinstance(r, WebSocket):
            bj_set.discard(r)


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
                try:
                    sess = get_blackjack_session(token)
                    players = get_blackjack_players(sess["id"])
                    player = next((p for p in players if p["telegram_id"] == player_id), None)

                    if player and player["status"] == "playing":
                        hand = json.loads(player["hand"])
                        deck = json.loads(sess["deck"])

                        if action == "hit":
                            if deck:
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

                        # Check if all active players done (exclure waiting_next / left)
                        players = get_blackjack_players(sess["id"])
                        active = [p for p in players
                                  if p["status"] not in ("waiting_next", "left", "waiting")]
                        all_done = bool(active) and all(
                            p["status"] in ("stand", "bust", "done") for p in active
                        )

                        if all_done:
                            await _resolve_hand(sess["id"], token)
                        else:
                            await _bj_broadcast(token)
                except Exception as e:
                    print(f"[BJ WS action error] {e}")
                    await _bj_broadcast(token)  # broadcast quand même pour synchro client

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _bj_clients.get(token, set()).discard(ws)


@app.post("/blackjack/{token}/rematch")
async def blackjack_rematch(token: str, request: Request):
    """Relance une partie sur la MÊME session. Seul le créateur de la table peut initier."""
    body = await request.json()
    caller_id = _resolve_user(request, body) or 0

    session = get_blackjack_session(token)
    if not session:
        return {"ok": False, "error": "Session introuvable"}
    if session["status"] not in ("finished", "active"):
        return {"ok": False, "error": "La partie n'est pas encore terminée"}

    if int(session.get("creator_id") or 0) != caller_id:
        return {"ok": False, "error": "Seul le créateur de la table peut lancer la revanche"}

    from data.database import _execute

    players = get_blackjack_players(session["id"])
    # Joueurs encore présents (pas partis)
    remaining = [p for p in players if p["status"] != "left"]
    if not any(int(p["telegram_id"]) == caller_id for p in remaining):
        return {"ok": False, "error": "Tu n'es plus dans cette session"}
    if not remaining:
        return {"ok": False, "error": "Aucun joueur restant"}

    # Débit atomique des mises de tous les joueurs restants. Si l'un d'eux n'a
    # pas assez, on rembourse ceux qu'on a déjà débités et on annule la revanche.
    debited = []
    for p in remaining:
        if try_debit_coins(p["telegram_id"], p["bet"], "Blackjack - mise (revanche)"):
            debited.append(p)
        else:
            for d in debited:
                add_coins(d["telegram_id"], d["bet"], "Blackjack - remboursement (revanche annulée)")
            u = get_user(p["telegram_id"])
            name = u["username"] if u else str(p["telegram_id"])
            return {"ok": False, "error": f"Solde insuffisant pour {name}"}

    # Supprimer les joueurs partis
    _execute("DELETE FROM blackjack_players WHERE session_id=? AND status='left'",
             [session["id"]])

    # Distribuer une nouvelle donne
    deck = new_deck()
    dealer_hand = [deck.pop(), deck.pop()]

    for p in remaining:
        hand = [deck.pop(), deck.pop()]
        update_blackjack_player(session["id"], p["telegram_id"],
            hand=json.dumps(hand),
            status="playing",
            result=None,
        )

    update_blackjack_session(session["id"],
        status="active",
        deck=json.dumps(deck),
        dealer_hand=json.dumps(dealer_hand),
    )

    # Gérer les blackjacks immédiats
    players = get_blackjack_players(session["id"])
    for p in players:
        if p["status"] != "playing":
            continue
        hand = json.loads(p["hand"])
        if is_blackjack(hand):
            winnings = int(p["bet"] * 1.5)
            add_coins(p["telegram_id"], p["bet"] + winnings, "Blackjack naturel !")
            update_blackjack_player(session["id"], p["telegram_id"],
                status="done", result="blackjack")
            _award_bj_win(p["telegram_id"])

    # Si tous ont un blackjack, résoudre immédiatement
    players = get_blackjack_players(session["id"])
    active = [p for p in players
              if p["status"] not in ("waiting_next", "left", "waiting")]
    if active and all(p["status"] in ("stand", "bust", "done") for p in active):
        await _resolve_hand(session["id"], token)
    else:
        await _bj_broadcast(token)

    return {"ok": True, "token": token}


# ── Abonnement Premium (Stripe) ───────────────────────────────────────────────

@app.post("/premium/checkout")
async def premium_checkout(request: Request):
    """Crée une session Stripe Checkout pour s'abonner au premium."""
    if not stripe.api_key or not STRIPE_PRICE_ID:
        return {"ok": False, "error": "Paiement temporairement indisponible"}
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Identifiant manquant"}
    user = get_user(int(telegram_id))
    if not user:
        return {"ok": False, "error": "Utilisateur introuvable"}
    try:
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(
                metadata={"user_id": str(telegram_id), "username": user["username"]},
                name=user["username"],
            )
            customer_id = customer.id
            set_stripe_customer_id(int(telegram_id), customer_id)
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{FRONTEND_URL}/?premium=success",
            cancel_url=f"{FRONTEND_URL}/?premium=cancel",
            metadata={"user_id": str(telegram_id)},
            allow_promotion_codes=True,
        )
        return {"ok": True, "url": session.url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/premium/portal")
async def premium_portal(request: Request):
    """Renvoie l'URL du Stripe Customer Portal pour gérer l'abonnement."""
    if not stripe.api_key:
        return {"ok": False, "error": "Paiement temporairement indisponible"}
    body = await request.json()
    telegram_id = _resolve_user(request, body)
    if not telegram_id:
        return {"ok": False, "error": "Identifiant manquant"}
    user = get_user(int(telegram_id))
    if not user or not user.get("stripe_customer_id"):
        return {"ok": False, "error": "Aucun abonnement trouvé"}
    try:
        session = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=FRONTEND_URL,
        )
        return {"ok": True, "url": session.url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/premium/status/{telegram_id}")
def premium_status(telegram_id: int):
    """Retourne le statut premium d'un utilisateur."""
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if telegram_id == admin_id:
        return {"is_premium": True, "source": "admin"}
    user = get_user(telegram_id)
    if not user:
        return {"is_premium": False}
    return {
        "is_premium": bool(user.get("is_premium")),
        "premium_until": user.get("premium_until"),
        "has_stripe_customer": bool(user.get("stripe_customer_id")),
    }


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Webhook Stripe : active/désactive l'abonnement selon les événements."""
    if not STRIPE_WEBHOOK_SECRET:
        return Response(status_code=503)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return Response(status_code=400)

    t = event["type"]
    obj = event["data"]["object"]

    if t == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        user_id_str = (obj.get("metadata") or {}).get("user_id")
        if user_id_str and subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                premium_until = datetime.fromtimestamp(
                    sub["current_period_end"], tz=timezone.utc
                ).isoformat()
                set_premium(int(user_id_str), customer_id, subscription_id, premium_until)
                _check_badges_for_user(int(user_id_str))
            except Exception:
                pass
    elif t in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = obj.get("customer")
        user = get_user_by_stripe_customer(customer_id) if customer_id else None
        if user:
            status = obj.get("status")
            uid = user.get("user_id") or user.get("telegram_id")
            if status in ("active", "trialing"):
                premium_until = datetime.fromtimestamp(
                    obj["current_period_end"], tz=timezone.utc
                ).isoformat()
                set_premium(uid, customer_id, obj["id"], premium_until)
            else:
                clear_premium(uid)
    elif t == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        user = get_user_by_stripe_customer(customer_id) if customer_id else None
        if user:
            uid = user.get("user_id") or user.get("telegram_id")
            clear_premium(uid)

    return {"received": True}
