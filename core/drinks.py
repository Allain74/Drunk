from dataclasses import dataclass

@dataclass
class Drink:
    name: str
    volume_ml: float   # volume en mL
    abv: float         # taux d'alcool en % (ex: 5.0 pour une bière à 5%)
    aliases: list[str] # mots-clés acceptés dans le bot

DRINKS: dict[str, Drink] = {
    "demi":          Drink("Demi (25cl)",               250,  5.0,  ["demi", "biere", "bière"]),
    "pinte":         Drink("Pinte (50cl)",              500,  5.0,  ["pinte"]),
    "demif":         Drink("Demi forte (25cl, 8.5%)",  250,  8.5,  ["demif"]),
    "pintef":        Drink("Pinte forte (50cl, 8.5%)", 500,  8.5,  ["pintef"]),
    "biere_bouteille": Drink("Bière bouteille 33cl",   330,  5.0,  ["biere33", "bouteille"]),
    "vin_rouge":     Drink("Verre vin rouge",          150, 13.0,  ["vin", "vinrouge", "rouge"]),
    "vin_blanc":     Drink("Verre vin blanc",          150, 12.0,  ["vinblanc", "blanc"]),
    "champagne":     Drink("Coupe champagne",          125, 12.0,  ["champagne", "bulles", "prosecco"]),
    "shot_vodka":    Drink("Shot vodka",                40, 40.0,  ["vodka", "shot", "shotvodka"]),
    "shot_tequila":  Drink("Shot tequila",              40, 38.0,  ["tequila", "shottequila"]),
    "shot_whisky":   Drink("Shot whisky",               40, 40.0,  ["whisky", "whiskey", "shotwhisky"]),
    "shot_rhum":     Drink("Shot rhum",                 40, 40.0,  ["rhum", "rum", "shotrhum"]),
    "mojito":        Drink("Mojito",                   200,  8.0,  ["mojito"]),
    "gin_tonic":     Drink("Gin tonic",                200, 10.0,  ["gintonic", "gin"]),
    "aperol":        Drink("Aperol spritz",            200,  8.0,  ["aperol", "spritz"]),
    "long_island":   Drink("Long Island",              250, 22.0,  ["longisland"]),
    "sangria":       Drink("Verre sangria",            200,  9.0,  ["sangria"]),
    "cidre":         Drink("Verre cidre",              250,  4.5,  ["cidre", "cider"]),
    "pastis":        Drink("Pastis",                    25, 45.0,  ["pastis", "ricard", "51"]),
}

def find_drink(query: str) -> Drink | None:
    """Trouve une boisson à partir d'un alias (insensible à la casse)."""
    q = query.lower().strip()
    for drink in DRINKS.values():
        if q in [a.lower() for a in drink.aliases]:
            return drink
    return None

def list_drinks_text() -> str:
    lines = ["📋 *Boissons disponibles :*\n"]
    for key, d in DRINKS.items():
        lines.append(f"• `/{d.aliases[0]}` — {d.name} ({d.abv}%)")
    return "\n".join(lines)
