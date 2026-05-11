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
    "vin":           Drink("Verre de vin",              120, 12.0,  ["vin", "vinrouge", "rouge", "vinblanc", "blanc"]),
    "champagne":     Drink("Coupe champagne",          100, 12.0,  ["champagne", "bulles", "prosecco"]),
    "shot_vodka":    Drink("Shot vodka",                40, 40.0,  ["vodka", "shot", "shotvodka"]),
    "shot_tequila":  Drink("Shot tequila",              40, 38.0,  ["tequila", "shottequila"]),
    "shot_whisky":   Drink("Shot whisky",               40, 40.0,  ["whisky", "whiskey", "shotwhisky"]),
    "shot_rhum":     Drink("Shot rhum",                 40, 40.0,  ["rhum", "rum", "shotrhum"]),
    "shot_96":       Drink("Shot alcool 96°",            40, 96.0,  ["shot96", "96"]),
    "mojito":        Drink("Mojito",                   200,  6.5,  ["mojito"]),
    "gin_tonic":     Drink("Gin tonic",                200,  7.5,  ["gintonic", "gin"]),
    "aperol":        Drink("Spritz",                   200,  8.0,  ["aperol", "spritz"]),
    "long_island":   Drink("Long Island",              250, 17.0,  ["longisland"]),
    "sangria":       Drink("Verre sangria",            200,  9.0,  ["sangria"]),
    "cidre":         Drink("Verre cidre",              250,  4.5,  ["cidre", "cider"]),
    "ricard":        Drink("Ricard / Pastis",             25, 45.0,  ["ricard", "pastis", "51"]),
    "perroquet":     Drink("Perroquet Sauvage 🦜",         45, 34.5,  ["perroquet", "sauvage"]),
    "get27":         Drink("Get 27",                     40, 21.4,  ["get27", "get"]),
    "bucket":        Drink("Bucket thaïlandais 🪣",     125, 40.0, ["bucket", "buckethai"]),
    "gnole":         Drink("Gnôle",                      40, 50.0,  ["gnole", "gnôle"]),
    "caipirinha":    Drink("Caïpirinha",                200, 25.0,  ["caipirinha", "caïpirinha", "caipi"]),
    "chartreuse":    Drink("Chartreuse",                 40, 55.0,  ["chartreuse"]),
    "sex_beach":     Drink("Sex on the beach",          200, 12.0,  ["sex", "sexonthebeach", "sotb"]),
    "picon_biere":   Drink("Picon bière (50cl)",        540,  6.5,  ["picon", "piconbiere"]),
    "martini":       Drink("Martini (vermouth)",         80, 15.0,  ["martini"]),
    "pina_colada":   Drink("Piña Colada",               250, 12.0,  ["pina", "pinacolada", "colada"]),
    "moscow_mule":   Drink("Moscow Mule",               200, 10.0,  ["moscow", "mule", "moscowmule"]),
    "daiquiri":      Drink("Daiquiri",                  120, 20.0,  ["daiquiri"]),
    "limoncello":    Drink("Limoncello",                 30, 28.0,  ["limoncello"]),
    "punch":         Drink("Punch",                     200, 15.0,  ["punch"]),
    "kir":           Drink("Kir",                       120, 10.0,  ["kir"]),
    "suze":          Drink("Suze",                       40, 20.0,  ["suze"]),
    "jagermeister":  Drink("Jägermeister",               40, 35.0,  ["jagermeister", "jager"]),
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
