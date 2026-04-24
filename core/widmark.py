from datetime import datetime, timezone

ELIMINATION_RATE = 0.15   # g/L par heure (moyenne)
ABSORPTION_DELAY = 0.5    # heure avant que l'alcool soit absorbé

# Coefficient de Widmark selon le sexe
WIDMARK_R = {"homme": 0.68, "femme": 0.55}


def alcohol_grams(volume_ml: float, abv_percent: float) -> float:
    """Masse d'alcool pur en grammes dans une boisson."""
    return volume_ml * (abv_percent / 100) * 0.789


def bac_contribution(
    alc_grams: float,
    weight_kg: float,
    gender: str,
    drink_time: datetime,
    now: datetime | None = None,
) -> float:
    """
    Contribution d'une boisson au TAC (g/L) à un instant donné.
    Retourne 0 si l'alcool est déjà éliminé.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    r = WIDMARK_R.get(gender, 0.68)
    hours_elapsed = (now - drink_time).total_seconds() / 3600

    # Pic TAC apporté par cette boisson
    peak = alc_grams / (weight_kg * r)

    # L'élimination commence après la période d'absorption
    hours_eliminating = max(0.0, hours_elapsed - ABSORPTION_DELAY)
    eliminated = ELIMINATION_RATE * hours_eliminating

    return max(0.0, peak - eliminated)


def total_bac(
    drinks: list[tuple[float, datetime]],   # [(alc_grams, drink_time), ...]
    weight_kg: float,
    gender: str,
    now: datetime | None = None,
) -> float:
    """TAC total en g/L (‰) à l'instant `now`."""
    if now is None:
        now = datetime.now(timezone.utc)
    return sum(
        bac_contribution(alc_g, weight_kg, gender, t, now)
        for alc_g, t in drinks
    )


def bac_label(bac: float) -> str:
    """Retourne une description humaine du niveau d'alcoolémie."""
    if bac == 0:
        return "😶 Sobre"
    elif bac < 0.2:
        return "🟢 Légèrement déshinibé"
    elif bac < 0.5:
        return "🟡 Sous l'influence"
    elif bac < 0.8:
        return "🟠 Alcoolisé (limite légale FR)"
    elif bac < 1.5:
        return "🔴 Ivre"
    else:
        return "💀 Très ivre — attention !"


def sober_in_hours(bac: float) -> float:
    """Estimation du temps restant avant retour à 0 (heures)."""
    if bac <= 0:
        return 0.0
    return bac / ELIMINATION_RATE
