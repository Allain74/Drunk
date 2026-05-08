from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from core.widmark import total_bac
from data.database import get_all_users, get_weekly_drink_logs
from core.drinks import DRINKS

PARIS = ZoneInfo("Europe/Paris")
MEDALS = ["🥇", "🥈", "🥉"]

JOURS_FR = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi", "Sunday": "Dimanche",
}
MOIS_FR = {
    "Jan": "jan", "Feb": "fév", "Mar": "mar", "Apr": "avr",
    "May": "mai", "Jun": "juin", "Jul": "juil", "Aug": "aoû",
    "Sep": "sep", "Oct": "oct", "Nov": "nov", "Dec": "déc",
}


def _fmt_date(dt: datetime) -> str:
    m = MOIS_FR.get(dt.strftime("%b"), dt.strftime("%b"))
    return f"{dt.day} {m}"


def build_weekly_recap(since: datetime, until: datetime) -> str:
    users = {u["telegram_id"]: u for u in get_all_users()}
    rows = get_weekly_drink_logs(since)

    drinks_by_user: dict[int, list] = {}
    for r in rows:
        uid = r["telegram_id"]
        t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc)
        drinks_by_user.setdefault(uid, []).append((r["alc_grams"], t, r["drink_key"]))

    stats = []
    for uid, user in users.items():
        drinks = drinks_by_user.get(uid, [])
        nb_drinks = len(drinks)
        total_alc = sum(d[0] for d in drinks)
        doses = total_alc / 10

        peak_bac = 0.0
        drink_tuples = [(d[0], d[1]) for d in drinks]
        for i in range(len(drink_tuples)):
            b = total_bac(drink_tuples[:i+1], user["weight_kg"], user["gender"], drink_tuples[i][1])
            if b > peak_bac:
                peak_bac = b

        stats.append({
            "username": user["username"],
            "nb_drinks": nb_drinks,
            "doses": round(doses, 1),
            "peak_bac": round(peak_bac, 2),
        })

    stats.sort(key=lambda x: x["doses"], reverse=True)
    drinkers = [s for s in stats if s["nb_drinks"] > 0]
    sobres = [s["username"] for s in stats if s["nb_drinks"] == 0]

    # Boisson la plus bue
    drink_counts: dict[str, int] = {}
    for r in rows:
        drink_counts[r["drink_key"]] = drink_counts.get(r["drink_key"], 0) + 1
    top_drink_key = max(drink_counts, key=drink_counts.get) if drink_counts else None
    top_drink_name = DRINKS[top_drink_key].name if top_drink_key and top_drink_key in DRINKS else top_drink_key
    top_drink_count = drink_counts[top_drink_key] if top_drink_key else 0

    # Jour le plus actif
    day_counts: dict[str, int] = {}
    for r in rows:
        t = datetime.fromisoformat(r["logged_at"]).replace(tzinfo=timezone.utc).astimezone(PARIS)
        day_fr = JOURS_FR.get(t.strftime("%A"), t.strftime("%A"))
        month_fr = MOIS_FR.get(t.strftime("%b"), t.strftime("%b"))
        day_str = f"{day_fr} {t.day} {month_fr}"
        day_counts[day_str] = day_counts.get(day_str, 0) + 1
    top_day = max(day_counts, key=day_counts.get) if day_counts else None

    # Record de la semaine (pic TAC le plus haut)
    best_peak = max(drinkers, key=lambda x: x["peak_bac"]) if drinkers else None

    since_p = since.astimezone(PARIS)
    until_p = (until - timedelta(seconds=1)).astimezone(PARIS)

    lines = [
        f"📊 *Recap de la semaine — {_fmt_date(since_p)} au {_fmt_date(until_p)}*\n",
        "🏆 *Classement de la semaine :*",
    ]

    for i, s in enumerate(drinkers):
        medal = MEDALS[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {s['username']} — {s['nb_drinks']} verres • {s['doses']} doses • pic {s['peak_bac']} g/L")

    if not drinkers:
        lines.append("_Personne n'a bu cette semaine 🤔_")

    if top_drink_name:
        lines.append(f"\n🍺 *Boisson de la semaine :* {top_drink_name} (x{top_drink_count})")
    if top_day:
        lines.append(f"📅 *Soirée la plus arrosée :* {top_day}")
    if best_peak:
        lines.append(f"💀 *Record de la semaine :* {best_peak['username']} avec {best_peak['peak_bac']} g/L")

    if sobres:
        lines.append(f"\n🏳️‍🌈 *Elus les plus gros pédés de la semaine :* {', '.join(sobres)}")

    lines.append("\nÀ la semaine prochaine ! 🍻")
    return "\n".join(lines)
