"""
Génère icon-192.png et icon-512.png avec Pillow (pas besoin de Cairo)
Prérequis : pip install pillow  (déjà installé)
"""
from PIL import Image, ImageDraw, ImageFont
import pathlib, math

here = pathlib.Path(__file__).parent

def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ── Fond arrondi sombre ──────────────────────────────────────────────────
    radius = size // 5
    d.rounded_rectangle([0, 0, size, size], radius=radius, fill="#111111")

    # ── Chope de bière ──────────────────────────────────────────────────────
    s = size / 512  # facteur d'échelle

    # Corps de la chope (rectangle arrondi)
    bx1, by1 = int(140*s), int(195*s)
    bx2, by2 = int(340*s), int(420*s)
    d.rounded_rectangle([bx1, by1, bx2, by2], radius=int(20*s), fill="#f59e0b")

    # Anse (dessinée comme un arc épais)
    hx, hy = int(340*s), int(230*s)
    hw = int(60*s)
    hh = int(100*s)
    for thickness in range(int(14*s)):
        d.arc(
            [hx-thickness, hy, hx+hw+thickness, hy+hh],
            start=-90, end=90,
            fill="#f59e0b",
            width=int(28*s),
        )
    d.arc([hx, hy, hx+hw, hy+hh], start=-90, end=90, fill="#f59e0b", width=int(28*s))

    # Mousse (ellipse en haut)
    mx1, my1 = int(133*s), int(155*s)
    mx2, my2 = int(347*s), int(210*s)
    d.ellipse([mx1, my1, mx2, my2], fill="#fef3c7")
    # Bulles de mousse
    d.ellipse([int(150*s), int(145*s), int(230*s), int(190*s)], fill="white")
    d.ellipse([int(240*s), int(140*s), int(310*s), int(178*s)], fill="#fef9c3")
    d.ellipse([int(185*s), int(155*s), int(240*s), int(192*s)], fill="white")

    # Reflet sur la chope
    d.rounded_rectangle(
        [int(165*s), int(215*s), int(192*s), int(400*s)],
        radius=int(10*s),
        fill=(255, 255, 255, 30),
    )

    # ── Emoji 🍺 au centre comme fallback texte ──────────────────────────────
    # (on dessine "D" en blanc transparent pour rappeler "Drunk")
    try:
        font = ImageFont.truetype("arial.ttf", size=int(90*s))
    except Exception:
        font = ImageFont.load_default()

    text = "D"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    tx = (size - tw) // 2
    ty = int(280*s)
    d.text((tx, ty), text, font=font, fill=(255, 255, 255, 40))

    return img


for sz in [192, 512]:
    icon = make_icon(sz)
    out = here / f"icon-{sz}.png"
    icon.save(out, "PNG")
    print(f"✅ {out.name} ({sz}×{sz})")

print("Icônes générées !")
