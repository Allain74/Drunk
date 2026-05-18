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
    Contribution d'une boisson au TAC (g/L) à un instant donné, avec un
    modèle d'absorption réaliste en 3 phases :
      1) Pendant les ABSORPTION_DELAY premières minutes (~30 min) : le BAC
         monte LINÉAIREMENT de 0 jusqu'au peak.
      2) Au-delà : phase d'élimination à ELIMINATION_RATE g/L par heure.
      3) Quand tout est éliminé : 0.

    Avant cette version, le BAC montait instantanément au peak (irréaliste).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if now < drink_time:
        # Le verre n'a pas encore été bu (futur) : 0
        return 0.0

    r = WIDMARK_R.get(gender, 0.68)
    hours_elapsed = (now - drink_time).total_seconds() / 3600

    # Peak TAC apporté par cette boisson (atteint après ABSORPTION_DELAY)
    peak = alc_grams / (weight_kg * r)

    if hours_elapsed < ABSORPTION_DELAY:
        # Phase 1 : absorption progressive (montée linéaire de 0 à peak)
        return peak * (hours_elapsed / ABSORPTION_DELAY)

    # Phase 2 : élimination après le peak
    hours_eliminating = hours_elapsed - ABSORPTION_DELAY
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
