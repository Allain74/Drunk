"""Système de gamification : XP, niveaux, badges.

XP gagnée par action :
  - Ajouter un verre : 10 XP
  - Gagner un pari : 50 XP
  - Gagner au Blackjack : 30 XP
  - Connexion quotidienne : 20 XP (max 1/jour)
  - Inviter un ami qui s'inscrit : 200 XP
"""

XP_PER_DRINK       = 10
XP_PER_BET_WIN     = 50
XP_PER_BJ_WIN      = 30
XP_DAILY_LOGIN     = 20
XP_REFERRAL        = 200

# Seuils de niveaux. Format : (xp_threshold, level_number, title, emoji)
LEVELS: list[tuple[int, int, str, str]] = [
    (0,      1,  "Sobre Sam",         "🥤"),
    (100,    2,  "Buveur curieux",    "🍶"),
    (300,    3,  "Apéro du dimanche", "🍷"),
    (600,    4,  "Festif",            "🍻"),
    (1_000,  5,  "Buveur confirmé",   "🥂"),
    (1_500,  6,  "Étudiant en soif",  "🍺"),
    (2_500,  7,  "Apéro mafioso",     "🍸"),
    (4_000,  8,  "Roi de la soirée",  "👑"),
    (6_000,  9,  "Bacchanale",        "🏆"),
    (10_000, 10, "Légende",           "💎"),
    (20_000, 11, "Dieu de Bacchus",   "🍷👑"),
]


def calc_level(xp: int) -> dict:
    """Renvoie {level, title, emoji, current_xp, next_threshold, progress_pct}."""
    xp = max(0, int(xp or 0))
    current = LEVELS[0]
    next_t = None
    for i, lvl in enumerate(LEVELS):
        if xp >= lvl[0]:
            current = lvl
            next_t = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
        else:
            break
    if next_t:
        span = next_t[0] - current[0]
        progress = (xp - current[0]) / span if span > 0 else 1.0
        next_threshold = next_t[0]
    else:
        progress = 1.0
        next_threshold = current[0]
    return {
        "xp": xp,
        "level": current[1],
        "title": current[2],
        "emoji": current[3],
        "next_threshold": next_threshold,
        "progress_pct": round(progress * 100, 1),
    }


# ── Badges ───────────────────────────────────────────────────────────────────
# Chaque badge a une condition (callable) qui prend un contexte et renvoie bool.
# Le contexte contient les stats actuelles de l'utilisateur.

BADGES: dict[str, dict] = {
    # ─── Premiers pas
    "first_drink":      {"name": "Premier verre",         "emoji": "🍺", "desc": "Logger ton premier verre"},
    "first_pari":       {"name": "Premier pari",          "emoji": "🎰", "desc": "Créer ton premier pari"},
    "first_bj":         {"name": "Première carte",        "emoji": "🃏", "desc": "Jouer ta première partie de Blackjack"},
    "first_friend":     {"name": "Pas tout seul",         "emoji": "🤝", "desc": "Suivre ton premier ami"},

    # ─── Marathon verres
    "drink_10":         {"name": "Échauffé",              "emoji": "🍻", "desc": "10 verres au total"},
    "drink_50":         {"name": "Régulier",              "emoji": "🍷", "desc": "50 verres au total"},
    "drink_100":        {"name": "Centenaire",            "emoji": "💯", "desc": "100 verres au total"},
    "drink_500":        {"name": "Marathonien",           "emoji": "🏃", "desc": "500 verres au total"},
    "drink_1000":       {"name": "Légende vivante",       "emoji": "🏛", "desc": "1000 verres au total"},

    # ─── Soirée du soir
    "night_10":         {"name": "Grosse soirée",         "emoji": "🌙", "desc": "10 verres en une seule session"},
    "night_15":         {"name": "Naufrage",              "emoji": "🌊", "desc": "15 verres en une seule session"},
    "high_bac_1":       {"name": "Bourré",                "emoji": "🥴", "desc": "Atteindre 1.0 g/L"},
    "high_bac_2":       {"name": "Carbonisé",             "emoji": "💀", "desc": "Atteindre 2.0 g/L"},

    # ─── Streaks
    "streak_3":         {"name": "Week-end chargé",       "emoji": "🔥", "desc": "3 jours d'affilée avec un verre"},
    "streak_7":         {"name": "Semaine de fou",        "emoji": "🔥🔥", "desc": "7 jours d'affilée"},
    "streak_14":        {"name": "Deux semaines",         "emoji": "🔥🔥🔥", "desc": "14 jours d'affilée"},

    # ─── Économie / Pièces
    "coins_500":        {"name": "Épargnant",             "emoji": "🪙", "desc": "Accumuler 500 🪙"},
    "coins_2000":       {"name": "Capitaliste",           "emoji": "💰", "desc": "Accumuler 2 000 🪙"},
    "coins_10000":      {"name": "Magnat",                "emoji": "🤑", "desc": "Accumuler 10 000 🪙"},

    # ─── Paris
    "bet_win_5":        {"name": "Bookmaker",             "emoji": "🎯", "desc": "Gagner 5 paris"},
    "bet_win_20":       {"name": "Tireur d'élite",        "emoji": "💥", "desc": "Gagner 20 paris"},
    "coinflip_5":       {"name": "Chanceux",              "emoji": "🪙🍀", "desc": "Gagner 5 coinflips"},

    # ─── Blackjack
    "bj_win_5":         {"name": "Carteur",               "emoji": "♠️", "desc": "Gagner 5 parties de Blackjack"},
    "bj_win_20":        {"name": "Croupier déchu",        "emoji": "♣️", "desc": "Gagner 20 parties de Blackjack"},
    "bj_blackjack":     {"name": "Vingt-et-un !",         "emoji": "🃏✨", "desc": "Faire un blackjack naturel"},

    # ─── Social
    "friends_5":        {"name": "Cercle d'amis",         "emoji": "👥", "desc": "5 amis suivis"},
    "friends_20":       {"name": "Star locale",           "emoji": "🌟", "desc": "20 abonnés"},
    "referral_1":       {"name": "Recruteur",             "emoji": "📣", "desc": "1 ami parrainé qui s'est inscrit"},
    "referral_5":       {"name": "Ambassadeur",           "emoji": "🏅", "desc": "5 amis parrainés"},

    # ─── Premium
    "premium":          {"name": "Soutien",               "emoji": "👑", "desc": "Devenir Premium"},

    # ─── Profil
    "set_avatar":       {"name": "Visage connu",          "emoji": "📸", "desc": "Mettre une photo de profil"},
}


def all_badges_meta() -> list[dict]:
    """Liste de tous les badges pour l'affichage (avec key inclus)."""
    return [{"key": k, **v} for k, v in BADGES.items()]
