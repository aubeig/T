"""
Карточки = HTML/CSS (cards/*.html + cards/card.css), для Telegram — растр.

Браузера в рантайме нет, поэтому этот модуль рисует ту же вёрстку:
те же размеры, радиусы, стеклянная панель, орбы, колодец аватара.
Меняется только текст (имя / статус / баланс / заголовок меню).
Иконка виол — только assets/viols_mark.png, без перерисовки.
"""
from __future__ import annotations

import io
import os
import logging

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_DIR = os.path.dirname(os.path.abspath(__file__))
MARK_PATH = os.path.join(_DIR, "assets", "viols_mark.png")
NUNITO_PATHS = (
    os.path.join(_DIR, "fonts", "Nunito-ExtraBold.ttf"),
    os.path.join(_DIR, "fonts", "Nunito-Bold.ttf"),
    os.path.join(_DIR, "fonts", "Nunito-Bold.otf"),
)
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# как в cards/card.css
OUT_W, OUT_H = 1280, 720
PANEL = (72, 78, 72 + 1136, 78 + 564)  # left top right bottom
# border-radius: 72px 28px 88px 36px
PANEL_R = (72, 28, 88, 36)
WELL_C = (72 + 86 + 134, 78 + 168 + 134)  # 292, 380
WELL_R = 134
CHIP_C = (72 + 78 + 26, 78 + 48 + 26)  # 176, 152
CHIP_R = 26
TEXT_X = 72 + 420  # 492
KICKER_Y = 78 + 120
NAME_Y = 78 + 158
SUB_Y = 78 + 236
LABEL_Y = 78 + 320
AMOUNT_Y = 78 + 360

_mark_rgba = None
_font_path = None
_bg_cache = None


def _pick_font() -> str | None:
    global _font_path
    if _font_path:
        return _font_path
    for p in NUNITO_PATHS:
        if os.path.isfile(p) and os.path.getsize(p) > 1000:
            _font_path = p
            return p
    _font_path = DEJAVU if os.path.isfile(DEJAVU) else None
    return _font_path


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _pick_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _load_mark() -> Image.Image:
    """Иконка виол как есть. Чёрный фон → альфа, эмблему не перерисовываем."""
    global _mark_rgba
    if _mark_rgba is not None:
        return _mark_rgba.copy()
    im = Image.open(MARK_PATH).convert("RGBA")
    pixels = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            lum = (r + g + b) / 3
            if lum < 18:
                pixels[x, y] = (r, g, b, 0)
            else:
                pixels[x, y] = (r, g, b, min(255, int(a * (lum / 255) * 1.4)))
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    _mark_rgba = im
    return im.copy()


def _circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((1, 1, size - 2, size - 2), fill=255)
    return m


def _fit_circle(src: Image.Image, size: int) -> Image.Image:
    src = src.convert("RGBA")
    sw, sh = src.size
    scale = max(size / max(sw, 1), size / max(sh, 1))
    nw, nh = max(size, int(sw * scale)), max(size, int(sh * scale))
    src = src.resize((nw, nh), Image.LANCZOS)
    left = (nw - size) // 2
    top = (nh - size) // 2
    src = src.crop((left, top, left + size, top + size))
    mask = _circle_mask(size)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0))
    out.putalpha(mask)
    return out


def _paste_center(base: Image.Image, overlay: Image.Image, center: tuple[int, int]) -> None:
    x = center[0] - overlay.width // 2
    y = center[1] - overlay.height // 2
    base.alpha_composite(overlay, (x, y))


def _ellipsis(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_w:
        return text
    t = text
    while t and draw.textlength(t + "…", font=font) > max_w:
        t = t[:-1]
    return t + "…"


def _vert_gradient(size, c0, c1) -> Image.Image:
    w, h = size
    im = Image.new("RGBA", (w, h))
    px = im.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(c0[0] + (c1[0] - c0[0]) * t)
        g = int(c0[1] + (c1[1] - c0[1]) * t)
        b = int(c0[2] + (c1[2] - c0[2]) * t)
        a = int(c0[3] + (c1[3] - c0[3]) * t)
        for x in range(w):
            px[x, y] = (r, g, b, a)
    return im


def _radial_orb(size, color) -> Image.Image:
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy = w / 2, h / 2
    r = min(w, h) / 2
    steps = 18
    cr, cg, cb, ca = color
    for i in range(steps, 0, -1):
        t = i / steps
        a = int(ca * (t ** 1.6))
        rr = int(r * t)
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(cr, cg, cb, a))
    return im.filter(ImageFilter.GaussianBlur(28))


def _panel_mask(w, h, radii) -> Image.Image:
    tl, tr, br, bl = radii
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(radii), fill=255)
    # дорисовываем «необычные» углы поверх единого радиуса
    # верх-лево 72, верх-право 28, низ-право 88, низ-лево 36
    extra = Image.new("L", (w, h), 0)
    ed = ImageDraw.Draw(extra)
    ed.rectangle((0, 0, w, h), fill=255)
    # вырезаем углы и кладём pieslice своего радиуса
    corners = [
        ((0, 0, tl * 2, tl * 2), 180, 270, (0, 0, tl, tl)),
        ((w - tr * 2, 0, w, tr * 2), 270, 360, (w - tr, 0, w, tr)),
        ((w - br * 2, h - br * 2, w, h), 0, 90, (w - br, h - br, w, h)),
        ((0, h - bl * 2, bl * 2, h), 90, 180, (0, h - bl, bl, h)),
    ]
    cut = Image.new("L", (w, h), 255)
    cd = ImageDraw.Draw(cut)
    for box, _a0, _a1, sq in corners:
        cd.rectangle(sq, fill=0)
    pie = Image.new("L", (w, h), 0)
    pd = ImageDraw.Draw(pie)
    for box, a0, a1, _sq in corners:
        pd.pieslice(box, a0, a1, fill=255)
    # база: прямоугольник без углов + секторные углы
    body = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(body)
    bd.rectangle((tl, 0, w - tr, h), fill=255)
    bd.rectangle((0, tl, w, h - bl), fill=255)
    bd.rectangle((0, 0, w - br, h - tr), fill=255)
    # проще и чище: rounded_rectangle max + ручные углы
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.rectangle((max(tl, bl), 0, w - max(tr, br), h), fill=255)
    d.rectangle((0, max(tl, tr), w, h - max(bl, br)), fill=255)
    d.pieslice((0, 0, tl * 2, tl * 2), 180, 270, fill=255)
    d.pieslice((w - tr * 2, 0, w - 1, tr * 2), 270, 360, fill=255)
    d.pieslice((w - br * 2, h - br * 2, w - 1, h - 1), 0, 90, fill=255)
    d.pieslice((0, h - bl * 2, bl * 2, h - 1), 90, 180, fill=255)
    d.rectangle((tl, 0, w - tr, max(tl, tr)), fill=255)
    d.rectangle((bl, h - max(bl, br), w - br, h), fill=255)
    d.rectangle((0, tl, max(tl, bl), h - bl), fill=255)
    d.rectangle((w - max(tr, br), tr, w, h - br), fill=255)
    return m


def _organic_blob(size) -> Image.Image:
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # border-radius: 62% 38% 48% 52% / 44% 56% 44% 56% ≈ смещённые эллипсы
    d.ellipse((int(w * 0.02), int(h * 0.08), int(w * 0.78), int(h * 0.92)), fill=(255, 255, 255, 36))
    d.ellipse((int(w * 0.18), int(h * 0.00), int(w * 0.98), int(h * 0.70)), fill=(180, 140, 255, 28))
    d.ellipse((int(w * 0.10), int(h * 0.35), int(w * 0.88), int(h * 0.98)), fill=(124, 58, 237, 22))
    return im.filter(ImageFilter.GaussianBlur(12))


def _stage_background() -> Image.Image:
    """Фон + орбы + стеклянная панель — кэш, текст поверх."""
    global _bg_cache
    if _bg_cache is not None:
        return _bg_cache.copy()

    bg = _vert_gradient((OUT_W, OUT_H), (7, 4, 18, 255), (26, 10, 50, 255))
    # linear-gradient 145deg overlay
    diag = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(diag)
    dd.rectangle((0, 0, OUT_W, OUT_H), fill=(22, 8, 44, 40))
    bg.alpha_composite(diag)

    orb_a = _radial_orb((420, 420), (124, 58, 237, 160))
    bg.alpha_composite(orb_a, (-70, -90))
    orb_b = _radial_orb((360, 360), (103, 232, 249, 90))
    bg.alpha_composite(orb_b, (OUT_W - 300, OUT_H - 280))

    x0, y0, x1, y1 = PANEL
    pw, ph = x1 - x0, y1 - y0
    mask = _panel_mask(pw, ph, PANEL_R)

    glass = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    fill = Image.new("RGBA", (pw, ph), (255, 255, 255, 28))
    tint = _vert_gradient((pw, ph), (255, 255, 255, 40), (80, 40, 140, 36))
    glass.alpha_composite(fill)
    glass.alpha_composite(tint)
    glass.putalpha(ImageChops_multiply(glass.split()[-1], mask))

    # блик сверху панели
    shine = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shine)
    sd.ellipse((-pw * 0.1, -ph * 0.55, pw * 1.1, ph * 0.55), fill=(255, 255, 255, 28))
    shine.putalpha(ImageChops_multiply(shine.split()[-1], mask))
    glass.alpha_composite(shine)

    blob = _organic_blob((520, 520))
    glass.alpha_composite(blob, (-80, 40))

    # обводка
    stroke = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    st = ImageDraw.Draw(stroke)
    st.rounded_rectangle((1, 1, pw - 2, ph - 2), radius=48, outline=(255, 255, 255, 70), width=2)
    stroke.putalpha(ImageChops_multiply(stroke.split()[-1], mask))
    glass.alpha_composite(stroke)

    # тень панели
    shadow = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    shd = ImageDraw.Draw(shadow)
    shd.rounded_rectangle((x0 + 8, y0 + 18, x1 + 8, y1 + 22), radius=56, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    bg.alpha_composite(shadow)
    bg.alpha_composite(glass, (x0, y0))

    _bg_cache = bg
    return bg.copy()


def ImageChops_multiply(a: Image.Image, b: Image.Image) -> Image.Image:
    from PIL import ImageChops
    if a.size != b.size:
        b = b.resize(a.size, Image.NEAREST)
    return ImageChops.multiply(a, b)


def render_card(
    *,
    name: str,
    status: str,
    balance,
    avatar: bytes | None = None,
    title: str | None = None,
) -> bytes:
    """Одна вёрстка. Меняется только текст и аватар."""
    base = _stage_background()
    mark = _load_mark()

    mini = _fit_circle(mark, CHIP_R * 2)
    # тёмный чип под иконкой
    chip_bg = Image.new("RGBA", (CHIP_R * 2 + 6, CHIP_R * 2 + 6), (0, 0, 0, 0))
    cd = ImageDraw.Draw(chip_bg)
    cd.ellipse((0, 0, chip_bg.width - 1, chip_bg.height - 1), fill=(5, 3, 10, 255), outline=(255, 255, 255, 70))
    _paste_center(base, chip_bg, CHIP_C)
    _paste_center(base, mini, CHIP_C)

    well_size = WELL_R * 2
    ring = Image.new("RGBA", (well_size + 16, well_size + 16), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((0, 0, ring.width - 1, ring.height - 1), outline=(103, 232, 249, 180), width=4)
    glow = ring.filter(ImageFilter.GaussianBlur(6))
    _paste_center(base, glow, WELL_C)
    _paste_center(base, ring, WELL_C)

    well_disk = Image.new("RGBA", (well_size, well_size), (0, 0, 0, 0))
    wd = ImageDraw.Draw(well_disk)
    wd.ellipse((0, 0, well_size - 1, well_size - 1), fill=(10, 6, 20, 255))
    _paste_center(base, well_disk, WELL_C)

    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGBA")
            _paste_center(base, _fit_circle(av, well_size), WELL_C)
        except Exception as e:
            logging.info(f"cards: аватар не наложился ({e}), ставлю иконку виол")
            _paste_center(base, _fit_circle(mark, well_size), WELL_C)
    else:
        _paste_center(base, _fit_circle(mark, well_size), WELL_C)

    overlay = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_kicker = _font(22)
    font_name = _font(56)
    font_title = _font(64)
    font_sub = _font(28)
    font_label = _font(18)
    font_bal = _font(92)

    white = (244, 240, 255, 255)
    muted = (196, 180, 232, 255)
    accent = (212, 184, 255, 255)
    max_text_w = OUT_W - TEXT_X - 90

    def put(text, font, y, fill):
        t = _ellipsis(draw, str(text), font, max_text_w)
        draw.text((TEXT_X + 2, y + 2), t, font=font, fill=(10, 0, 30, 150))
        draw.text((TEXT_X, y), t, font=font, fill=fill)

    put("VIOLS", font_kicker, KICKER_Y, accent)

    if title:
        put(title, font_title, NAME_Y, white)
        if name:
            put(name, font_sub, SUB_Y, muted)
        if status:
            put(status, font_label, SUB_Y + 44, accent)
        if balance is not None:
            put("БАЛАНС", font_label, LABEL_Y + 36, muted)
            put(f"{balance}", font_bal, AMOUNT_Y + 36, white)
    else:
        put(name or "Профиль", font_name, NAME_Y, white)
        put(status or "участник", font_sub, SUB_Y, muted)
        put("БАЛАНС", font_label, LABEL_Y, muted)
        put(f"{0 if balance is None else balance}", font_bal, AMOUNT_Y, white)

    base.alpha_composite(overlay)
    out = base.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_balance_card(name: str, status: str, balance, avatar: bytes | None = None) -> bytes:
    return render_card(name=name, status=status, balance=balance, avatar=avatar)


def render_menu_card(title: str, subtitle: str = "", balance=None, avatar: bytes | None = None) -> bytes:
    return render_card(name=subtitle, status="", balance=balance, avatar=avatar, title=title)


def tree_frames(name: str, status: str, balance, deco: str = "✧") -> list[str]:
    """Кадры дерева сверху вниз — для анимации edit/stream."""
    lines = [
        f"├─|  {name}",
        f"├─|  {status}",
        f"├─|  {balance} виол",
        f"└─|  {deco}",
    ]
    frames = []
    acc = []
    for line in lines:
        acc.append(line)
        frames.append("\n".join(acc))
    return frames


def tree_full(name: str, status: str, balance, deco: str = "✧") -> str:
    return tree_frames(name, status, balance, deco)[-1]
