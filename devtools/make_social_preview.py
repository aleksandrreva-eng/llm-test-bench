import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "screenshots", "compare.png")

CANVAS = (1280, 640)
BG = (11, 15, 20)
ACCENT = (77, 212, 172)
TEXT = (230, 237, 243)
MUTED = (139, 152, 165)
DIM = (95, 108, 120)
LINE = (42, 56, 70)

CROP = (150, 30, 1360, 560)
SHOT_W = 780
SHOT_POS = (460, 149)

F_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
F_SEMI = r"C:\Windows\Fonts\seguisb.ttf"
F_REG = r"C:\Windows\Fonts\segoeui.ttf"

TEXTS = {
    "en": {
        "h1_1": "Numbers,",
        "h1_2": "not vibes",
        "sub_1": "12 suites · 120 cases",
        "sub_2": "prefill, generation, VRAM —",
        "sub_3": "one report.",
        "foot": "Windows · free · MIT",
    },
    "ru": {
        "h1_1": "Числа вместо",
        "h1_2": "ощущений",
        "sub_1": "12 наборов · 120 кейсов",
        "sub_2": "префилл, генерация, VRAM —",
        "sub_3": "один отчёт",
        "foot": "Windows · бесплатно · MIT",
    },
}


def render(lang, out_path):
    t = TEXTS[lang]

    canvas = Image.new("RGB", CANVAS, BG)
    draw = ImageDraw.Draw(canvas)

    # Screenshot: crop, resize, round corners, paste
    shot = Image.open(SRC).convert("RGB")
    shot = shot.crop(CROP)
    h = round(SHOT_W * shot.height / shot.width)
    shot = shot.resize((SHOT_W, h), Image.LANCZOS)

    mask = Image.new("L", shot.size, 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((0, 0, shot.width - 1, shot.height - 1), radius=12, fill=255)

    x, y = SHOT_POS
    canvas.paste(shot, (x, y), mask)
    draw.rounded_rectangle((459, 148, 459 + 780, 148 + h), radius=13, outline=LINE, width=1)

    # Text
    draw.text((56, 150), "LLM TEST BENCH", font=ImageFont.truetype(F_BOLD, 18), fill=ACCENT)
    draw.text((56, 186), t["h1_1"], font=ImageFont.truetype(F_BOLD, 56), fill=TEXT)
    draw.text((56, 250), t["h1_2"], font=ImageFont.truetype(F_BOLD, 56), fill=TEXT)
    draw.rectangle((56, 330, 56 + 64, 330 + 4), fill=ACCENT)
    draw.text((56, 358), t["sub_1"], font=ImageFont.truetype(F_SEMI, 21), fill=TEXT)
    draw.text((56, 390), t["sub_2"], font=ImageFont.truetype(F_SEMI, 21), fill=MUTED)
    draw.text((56, 422), t["sub_3"], font=ImageFont.truetype(F_SEMI, 21), fill=MUTED)
    draw.text((56, 486), t["foot"], font=ImageFont.truetype(F_REG, 18), fill=DIM)

    canvas.save(out_path)


if __name__ == "__main__":
    targets = {
        "en": os.path.join(ROOT, "docs", "social-preview.png"),
        "ru": os.path.join(ROOT, "docs", "social-preview-ru.png"),
    }
    for lang, out_path in targets.items():
        render(lang, out_path)
    for out_path in targets.values():
        im = Image.open(out_path)
        print(out_path, os.path.getsize(out_path), im.size)
