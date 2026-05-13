"""
Génère icon-192-v2.png et icon-512-v2.png avec Pillow.
Design : gradient violet → magenta, chope dorée avec halo et glow.
Prérequis : pip install pillow
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pathlib

here = pathlib.Path(__file__).parent


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _radial_overlay(size: int, cx: int, cy: int, radius: int, color, alpha_max: int = 120):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pix = layer.load()
    for y in range(size):
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d < radius:
                t = 1 - (d / radius)
                a = int(alpha_max * (t ** 2))
                pix[x, y] = (color[0], color[1], color[2], a)
    return layer


def _round_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size, size], radius=radius, fill=255)
    return mask


def make_icon(size: int) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Fond gradient violet -> magenta
    bg = Image.new("RGB", (size, size), (30, 27, 75))
    pix = bg.load()
    c1, c2, c3 = (30, 27, 75), (76, 29, 149), (131, 24, 67)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (size * 2)
            if t < 0.5:
                pix[x, y] = _lerp(c1, c2, t * 2)
            else:
                pix[x, y] = _lerp(c2, c3, (t - 0.5) * 2)
    base.paste(bg.convert("RGBA"), (0, 0))

    # Halo violet/rose
    base.alpha_composite(_radial_overlay(size, size // 2, int(size * 0.45),
                                          int(size * 0.42), (168, 85, 247), 100))
    base.alpha_composite(_radial_overlay(size, int(size * 0.7), int(size * 0.6),
                                          int(size * 0.35), (236, 72, 153), 70))

    d = ImageDraw.Draw(base)
    s = size / 512.0

    # Anse
    d.arc([int(310 * s), int(225 * s), int(440 * s), int(365 * s)],
          start=-90, end=90, fill=(245, 158, 11, 255), width=int(34 * s))

    # Corps de la chope (gradient doré)
    bx1, by1 = int(125 * s), int(195 * s)
    bx2, by2 = int(345 * s), int(440 * s)
    beer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(beer)
    for y in range(by1, by2):
        t = (y - by1) / (by2 - by1)
        if t < 0.5:
            col = _lerp((253, 224, 71), (245, 158, 11), t * 2)
        else:
            col = _lerp((245, 158, 11), (180, 83, 9), (t - 0.5) * 2)
        bdraw.line([(bx1, y), (bx2, y)], fill=col + (255,))
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([bx1, by1, bx2, by2], radius=int(22 * s), fill=255)
    # Glow doux
    glow_mask = mask.filter(ImageFilter.GaussianBlur(int(10 * s)))
    glow_layer = Image.new("RGBA", (size, size), (245, 158, 11, 0))
    gpix = glow_layer.load()
    for y in range(size):
        for x in range(size):
            a = glow_mask.getpixel((x, y))
            if a > 0:
                gpix[x, y] = (245, 158, 11, min(a // 2, 80))
    base.alpha_composite(glow_layer)
    beer.putalpha(mask)
    base.alpha_composite(beer)

    # Reflet
    d.rounded_rectangle(
        [int(148 * s), int(215 * s), int(176 * s), int(410 * s)],
        radius=int(12 * s), fill=(255, 255, 255, 76),
    )

    # Mousse + bulles
    d.ellipse([int(115 * s), int(150 * s), int(355 * s), int(230 * s)],
              fill=(254, 243, 199, 255))
    d.ellipse([int(167 * s), int(142 * s), int(223 * s), int(198 * s)],
              fill=(255, 255, 255, 242))
    d.ellipse([int(223 * s), int(138 * s), int(267 * s), int(182 * s)],
              fill=(255, 255, 255, 230))
    d.ellipse([int(270 * s), int(152 * s), int(310 * s), int(192 * s)],
              fill=(254, 249, 195, 242))
    d.ellipse([int(156 * s), int(171 * s), int(184 * s), int(199 * s)],
              fill=(255, 255, 255, 180))
    d.ellipse([int(308 * s), int(173 * s), int(332 * s), int(197 * s)],
              fill=(255, 255, 255, 180))

    # Bulles pétillantes
    for bx, by, r, alpha in [(180, 280, 6, 100), (200, 350, 4, 130),
                              (270, 320, 5, 100), (290, 395, 3, 150),
                              (230, 415, 4, 120)]:
        d.ellipse([int((bx - r) * s), int((by - r) * s),
                   int((bx + r) * s), int((by + r) * s)],
                  fill=(255, 255, 255, alpha))

    # Lettre D filigrane
    try:
        font = ImageFont.truetype("arialbd.ttf", size=int(120 * s))
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", size=int(120 * s))
        except Exception:
            font = ImageFont.load_default()
    text = "D"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    tx = (size - tw) // 2 - int(10 * s)
    d.text((tx, int(290 * s)), text, font=font, fill=(255, 255, 255, 38))

    # Masque global arrondi
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(base, (0, 0), _round_mask(size, size // 5))
    return rounded


for sz in [192, 512]:
    icon = make_icon(sz)
    out = here / f"icon-{sz}-v2.png"
    icon.save(out, "PNG")
    print(f"[OK] {out.name} ({sz}x{sz})")

print("Icones generees !")
