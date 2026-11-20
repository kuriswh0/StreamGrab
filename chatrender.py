"""chatrender.py: Pillow port of macos/chatrender.swift (used on Windows and Linux).

Draws a Twitch-style chat panel and either
  * streams raw BGRA (premultiplied) frames to stdout for ffmpeg to composite, or
  * writes a single PNG frame (--preview-at SECONDS --out file.png) for the style editor.

  chatrender.py --input render.json --width W --height H --duration S [--fps 15] [--cache DIR]
  chatrender.py --input render.json --width W --height H --preview-at 12.5 --out frame.png
  chatrender.py --list-fonts [--cache DIR]

render.json is the same as for the Swift tool (see render.render_input).
"""
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"


def parse_args(argv):
    opts, it = {}, iter(argv)
    for k in it:
        if not k.startswith("--"):
            continue
        key = k[2:]
        if key == "list-fonts":
            opts[key] = "1"
            continue
        opts[key] = next(it, "")
    return opts


# ---------------------------------------------------------------- fonts

def font_dirs():
    if WINDOWS:
        windir = os.environ.get("WINDIR", r"C:\Windows")
        local = os.environ.get("LOCALAPPDATA", "")
        return [os.path.join(windir, "Fonts"), os.path.join(local, "Microsoft", "Windows", "Fonts")]
    if MAC:
        return ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts")]


def _name(table, *ids):
    for i in ids:
        rec = table.getDebugName(i)
        if rec:
            return rec
    return ""


def font_index(cache):
    """[[family, style, path, index], ...] for installed fonts; cached until the font folders change."""
    files = []
    for d in font_dirs():
        for root, _, names in os.walk(d):
            for n in names:
                if n.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                    files.append(os.path.join(root, n))
    files.sort()
    stamp = [len(files)] + [int(os.path.getmtime(d)) for d in font_dirs() if os.path.isdir(d)]
    path = os.path.join(cache, "fonts-index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("stamp") == stamp:
            return data["fonts"]
    except (OSError, ValueError):
        pass
    from fontTools.ttLib import TTCollection, TTFont
    out = []
    for f in files:
        try:
            if f.lower().endswith((".ttc", ".otc")):
                fonts = TTCollection(f, lazy=True).fonts
            else:
                fonts = [TTFont(f, lazy=True)]
            for i, ft in enumerate(fonts):
                name = ft["name"]
                fam = _name(name, 16, 1)
                if fam and not fam.startswith("."):
                    out.append([fam, _name(name, 17, 2) or "Regular", f, i])
        except Exception:  # noqa: BLE001 - skip unreadable fonts
            continue
    try:
        os.makedirs(cache, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stamp": stamp, "fonts": out}, fh)
    except OSError:
        pass
    return out


WEIGHT_WORDS = {"thin": 1, "extralight": 2, "ultralight": 2, "light": 3, "semilight": 3.5, "regular": 4,
                "normal": 4, "book": 4, "roman": 4, "medium": 5, "semibold": 6, "demibold": 6, "bold": 7,
                "extrabold": 8, "ultrabold": 8, "heavy": 8.5, "black": 9}


def style_weight(style):
    s = style.lower().replace(" ", "").replace("-", "")
    best = 4
    for word, w in WEIGHT_WORDS.items():
        if word in s:
            best = w if word not in ("regular", "normal", "book", "roman") else best
    return best


def pick_face(index, family, bold):
    faces = [f for f in index if f[0].lower() == family.lower()
             and "italic" not in f[1].lower() and "oblique" not in f[1].lower()]
    if not faces:
        return None
    target = 7 if bold else 4.5  # regular text a touch heavier than Regular, like the Swift renderer's medium
    return min(faces, key=lambda f: (abs(style_weight(f[1]) - target), f[1]))


SYSTEM_FAMILIES = (["Segoe UI"] if WINDOWS else ["Helvetica Neue", "Helvetica", "Arial"] if MAC
                   else ["DejaVu Sans", "Noto Sans", "Liberation Sans"])
MONO_FAMILIES = ["Consolas", "Cascadia Mono", "Menlo", "Courier New", "DejaVu Sans Mono"]
FALLBACK_FAMILIES = ["Segoe UI Symbol", "Segoe UI", "Arial Unicode MS", "Microsoft YaHei", "Malgun Gothic",
                     "Yu Gothic", "Nirmala UI", "Leelawadee UI", "Ebrima", "Gadugi", "Arial",
                     "Apple Symbols", "PingFang SC", "Apple SD Gothic Neo", "Hiragino Sans",
                     "Noto Sans", "Noto Sans CJK SC", "DejaVu Sans"]
EMOJI_FAMILIES = ["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"]


class Face:
    """A font file at one pixel size, with its character map for fallback decisions."""

    def __init__(self, path, index, size, bold_fallback=False):
        self.path, self.index, self.size = path, index, size
        self.fake_bold = bold_fallback
        self.bitmap_size = None
        try:
            self.font = ImageFont.truetype(path, size, index=index, layout_engine=ImageFont.Layout.RAQM)
        except (OSError, ImportError, KeyError):
            try:
                self.font = ImageFont.truetype(path, size, index=index)
            except OSError:
                # Bitmap colour fonts (Apple Color Emoji) only come in fixed sizes.
                self.font = ImageFont.truetype(path, 160 if MAC else 109, index=index)
                self.bitmap_size = 160 if MAC else 109
        self._cmap = None

    @property
    def cmap(self):
        if self._cmap is None:
            try:
                from fontTools.ttLib import TTFont
                ft = TTFont(self.path, fontNumber=self.index, lazy=True)
                self._cmap = set(ft.getBestCmap() or {})
            except Exception:  # noqa: BLE001
                self._cmap = set()
        return self._cmap

    def has(self, ch):
        return ord(ch) in self.cmap


class Fonts:
    def __init__(self, family, size, cache):
        self.size = size
        self.index = font_index(cache)
        self._faces = {}
        fams = [family] if family and family != "System" else []
        self.regular = self._first(fams + SYSTEM_FAMILIES, False)
        self.bold = self._first(fams + SYSTEM_FAMILIES, True)
        self.mono = self._first(MONO_FAMILIES, False, size=round(size * 0.8))
        self._fallbacks = None
        self._emoji = None

    def _face(self, rec, size, want_bold=False):
        key = (rec[2], rec[3], size)
        if key not in self._faces:
            self._faces[key] = Face(rec[2], rec[3], size,
                                    bold_fallback=want_bold and style_weight(rec[1]) < 6)
        return self._faces[key]

    def _first(self, families, bold, size=None):
        for fam in families:
            rec = pick_face(self.index, fam, bold)
            if rec:
                try:
                    return self._face(rec, size or self.size, bold)
                except OSError:
                    continue
        f = Face.__new__(Face)  # last resort: Pillow's built-in font
        f.path, f.index, f.size, f.fake_bold, f.bitmap_size = "", 0, size or self.size, bold, None
        f.font = ImageFont.load_default(size or self.size)
        f._cmap = set(range(32, 127))
        return f

    @property
    def fallbacks(self):
        if self._fallbacks is None:
            self._fallbacks = []
            for fam in FALLBACK_FAMILIES:
                rec = pick_face(self.index, fam, False)
                if rec:
                    try:
                        self._fallbacks.append(self._face(rec, self.size))
                    except OSError:
                        pass
        return self._fallbacks

    @property
    def emoji(self):
        if self._emoji is None:
            self._emoji = []
            for fam in EMOJI_FAMILIES:
                rec = pick_face(self.index, fam, False)
                if rec:
                    try:
                        self._emoji = [self._face(rec, self.size)]
                        break
                    except OSError:
                        pass
        return self._emoji


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D\U000E0020-\U000E007F]")


def is_emoji(ch):
    return bool(EMOJI_RE.match(ch))


def split_runs(text, primary, fonts):
    """Splits text into (face, substring) runs, falling back per character for missing glyphs and emoji."""
    runs = []
    for ch in text:
        if ch in "\u200d\ufe0f" or 0x1F3FB <= ord(ch) <= 0x1F3FF or 0xE0020 <= ord(ch) <= 0xE007F:
            face = runs[-1][0] if runs else primary  # joiners / skin tones stay with the emoji
        elif is_emoji(ch) and fonts.emoji and fonts.emoji[0].has(ch):
            face = fonts.emoji[0]
        elif primary.has(ch) or ch.isspace():
            face = primary
        else:
            face = next((f for f in fonts.fallbacks if f.has(ch)), primary)
        if runs and runs[-1][0] is face:
            runs[-1][1] += ch
        else:
            runs.append([face, ch])
    return runs


# ---------------------------------------------------------------- images (emotes + badges)

class Picture:
    """A decoded image; animated GIF/WebP keep every frame with its delay (seconds)."""

    def __init__(self, img, still=False):
        self.frames, self.delays = [], []
        for fr in ImageSequence.Iterator(img):
            self.frames.append(fr.convert("RGBA"))
            d = (fr.info.get("duration") or 0) / 1000
            self.delays.append(d if d > 0.011 else 0.1)
            if still:  # URL ending in #still: first frame only
                break
        if not self.frames:
            raise ValueError("no frames")
        self.total = sum(self.delays)
        self.size = self.frames[0].size
        self._scaled = {}

    @classmethod
    def from_bytes(cls, data, still=False):
        return cls(Image.open(io.BytesIO(data)), still)

    @property
    def animated(self):
        return len(self.frames) > 1

    def width_for(self, h):
        w, hh = self.size
        return w / hh * h if hh else h

    def frame(self, t, h, animate):
        i = 0
        if animate and self.animated and self.total > 0:
            x = t % self.total
            for i, d in enumerate(self.delays):
                if x < d:
                    break
                x -= d
        key = (i, h)
        if key not in self._scaled:
            w = max(1, round(self.width_for(h)))
            self._scaled[key] = self.frames[i].resize((w, max(1, round(h))), Image.LANCZOS)
        return self._scaled[key]


def hi_res(url):
    return url.replace("/1.0", "/2.0") if "static-cdn.jtvnw.net/emoticons" in url else url


def load_pictures(urls, cache):
    pictures, lock = {}, threading.Lock()
    os.makedirs(cache, exist_ok=True)

    def one(url):
        name = re.sub(r"[^A-Za-z0-9]", "_", url)[-140:]
        file = os.path.join(cache, name + ".img")
        try:
            with open(file, "rb") as fh:
                p = Picture.from_bytes(fh.read(), url.endswith("#still"))
            with lock:
                pictures[url] = p
            return
        except (OSError, ValueError, SyntaxError):
            pass
        try:
            req = urllib.request.Request(hi_res(url.split("#")[0]), headers={"User-Agent": "StreamGrab"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            p = Picture.from_bytes(data, url.endswith("#still"))
            with open(file, "wb") as fh:
                fh.write(data)
            with lock:
                pictures[url] = p
        except Exception:  # noqa: BLE001 - a missing emote falls back to its text
            pass

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(one, urls))
    return pictures


# ---------------------------------------------------------------- styling

def parse_hex(h):
    s = (h or "").strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        v = int(s, 16)
    except ValueError:
        return None
    return (v >> 16 & 0xFF, v >> 8 & 0xFF, v & 0xFF)


PALETTE = ["#FF4A4A", "#5C7CFF", "#2ECC71", "#FF7F50", "#9ACD32", "#FF6A3D", "#3CB371",
           "#DAA520", "#E07B39", "#5F9EA0", "#1E90FF", "#FF69B4", "#A970FF", "#00D68F"]


def user_color(m):
    c = parse_hex(m.get("color")) or parse_hex(PALETTE[sum(ord(ch) for ch in m["user"]) % len(PALETTE)])
    lum = (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255
    if lum < 0.35:
        c = tuple(round(v + (255 - v) * 0.45) for v in c)
    return c


PILLS = {
    "broadcaster": ("★", (232, 25, 22)), "moderator": ("M", (0, 173, 3)), "vip": ("V", (224, 5, 186)),
    "subscriber": ("S", (145, 71, 255)), "founder": ("F", (145, 71, 255)), "verified": ("✓", (145, 71, 255)),
    "member": ("M", (41, 153, 77)),
}


# ---------------------------------------------------------------- renderer

class Renderer:
    def __init__(self, data, W, H, cache):
        self.W, self.H = W, H
        st = data.get("style") or {}
        self.msgs = data["messages"]
        self.times = [m["t"] for m in self.msgs]
        self.badge_urls = data.get("badges") or {}
        self.F = F = float(st.get("font_size") or 22)
        self.show_badges = st.get("show_badges", True) is not False
        self.show_emotes = st.get("show_emotes", True) is not False
        self.animate = st.get("animate", True) is not False
        self.timestamps = bool(st.get("timestamps"))
        self.shadow = bool(st.get("shadow"))
        self.text_color = parse_hex(st.get("text_color")) or (240, 240, 242)
        bg = parse_hex(st.get("bg_color")) or (24, 24, 27)
        self.bg = bg + (round(255 * max(0, min(100, float(st.get("bg_opacity", 100)))) / 100),)
        self.radius = float(st.get("radius") or 0)
        self.fonts = Fonts(st.get("font_family") or "System", max(6, round(F)), cache)

        urls = set()
        if self.show_emotes:
            urls |= {p["url"] for m in self.msgs for p in m["parts"] if p.get("url")}
        if self.show_badges:
            urls |= set(self.badge_urls.values())
        self.pictures = load_pictures(urls, os.path.join(cache))
        self.pills = {}

        self.pad_x, self.gap = round(F * 0.6), round(F * 0.3)
        self.content_w = max(20, W - 2 * self.pad_x)
        self.line_gap = F * 0.06
        self.laid = {}

    # -- pieces
    def pill(self, set_id):
        if set_id in self.pills:
            return self.pills[set_id]
        if set_id not in PILLS:
            return None
        label, color = PILLS[set_id]
        side = max(4, round(self.F * 0.95 * 2))
        img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, side - 1, side - 1), radius=side * 0.22, fill=color + (255,))
        face = self.fonts.bold if self.fonts.bold.has(label) else \
            next((f for f in self.fonts.fallbacks if f.has(label)), self.fonts.bold)
        try:
            font = ImageFont.truetype(face.path, round(side * 0.62), index=face.index) if face.path else face.font
        except OSError:
            font = face.font
        d.text((side / 2, side / 2), label, font=font, fill=(255, 255, 255, 255), anchor="mm")
        p = Picture(img)
        self.pills[set_id] = p
        return p

    # -- layout: a message becomes a list of positioned items, drawn once into a cached layer
    def layout(self, i):
        if i in self.laid:
            return self.laid[i]
        m, F, fonts = self.msgs[i], self.F, self.fonts
        atoms = []  # ("text", str, face, rgba) | ("img", Picture, height, baseline_offset)
        if self.timestamps and m.get("ts"):
            atoms.append(("text", m["ts"] + " ", fonts.mono, self.text_color + (140,)))
        if self.show_badges:
            for b in m.get("badges") or []:
                set_id = b.split("/")[0]
                url = self.badge_urls.get(b) or self.badge_urls.get(set_id + "/1")
                pic = self.pictures.get(url) if url else None
                pic = pic or self.pill(set_id)
                if not pic:
                    continue
                atoms.append(("img", pic, round(F * 0.95), -F * 0.14))
                atoms.append(("text", " ", fonts.regular, self.text_color + (255,)))
        atoms.append(("text", m["user"], fonts.bold, user_color(m) + (255,)))
        atoms.append(("text", ": ", fonts.regular, self.text_color + (255,)))
        for p in m["parts"]:
            pic = self.pictures.get(p.get("url")) if self.show_emotes and p.get("url") else None
            if pic:
                atoms.append(("img", pic, round(F * 1.45), -F * 0.38))
            else:
                atoms.append(("text", p.get("text") or p.get("emote") or "", fonts.regular, self.text_color + (255,)))

        # Break text into words (keeping their trailing spaces) and each word into font runs.
        pieces = []  # (kind, payload, width, ascent, descent, color)
        for a in atoms:
            if a[0] == "img":
                pic, h, base = a[1], a[2], a[3]
                pieces.append(("img", (pic, h), pic.width_for(h), h + base, -base, None))
                continue
            _, text, face, color = a
            for word in re.findall(r"\S+\s*|\s+", text):
                runs = []
                for f, s in split_runs(word, face, fonts):
                    runs.append((f, s, self.run_width(f, s)))
                pieces.append(("text", runs, sum(r[2] for r in runs), 0, 0, color))

        asc, desc = self.fonts.regular.font.getmetrics()
        lines, line, x = [], [], 0.0
        for pc in pieces:
            w = pc[2]
            if pc[0] == "text" and w > self.content_w:  # a single word wider than the panel: break by character
                if line:
                    lines.append(line)
                    line, x = [], 0.0
                for f, s, _ in pc[1]:
                    for ch in s:
                        cw = self.run_width(f, ch)
                        if x + cw > self.content_w and line:
                            lines.append(line)
                            line, x = [], 0.0
                        line.append(("text", [(f, ch, cw)], cw, 0, 0, pc[5]))
                        x += cw
                continue
            fits = x + (w - self.trailing_space(pc)) <= self.content_w
            if not fits and line:
                lines.append(line)
                line, x = [], 0.0
            line.append(pc)
            x += w
        if line:
            lines.append(line)

        # Measure lines, then draw.
        metrics, y = [], 0.0
        for ln in lines:
            a = max([asc] + [p[3] for p in ln if p[0] == "img"])
            d = max([desc] + [p[4] for p in ln if p[0] == "img"])
            metrics.append((y + a, a, d))
            y += a + d + self.line_gap
        height = max(1, math.ceil(y - self.line_gap))
        layer = Image.new("RGBA", (self.content_w, height), (0, 0, 0, 0))
        text_mask_layer = Image.new("RGBA", layer.size, (0, 0, 0, 0)) if self.shadow else None
        animated = []
        for ln, (baseline, _, _) in zip(lines, metrics):
            x = 0.0
            for p in ln:
                if p[0] == "img":
                    pic, h = p[1]
                    top = baseline - p[3]
                    if self.animate and pic.animated:
                        animated.append((pic, h, round(x), round(top)))
                    else:
                        fr = pic.frame(0, h, False)
                        layer.alpha_composite(fr, (max(0, round(x)), max(0, round(top))))
                else:
                    for f, s, w in p[1]:
                        self.draw_run(layer, text_mask_layer, f, s, x, baseline, p[5])
                        x += w
                    continue
                x += p[2]
        if self.shadow:
            layer = self.with_shadow(layer, text_mask_layer)
        out = (layer, height, animated)
        self.laid[i] = out
        if len(self.laid) > 3000:  # bound memory on huge chats
            self.laid.clear()
            self.laid[i] = out
        return out

    def run_width(self, face, s):
        if face.bitmap_size:
            return face.font.getlength(s) * self.F / face.bitmap_size
        return face.font.getlength(s)

    def trailing_space(self, pc):
        if pc[0] != "text" or not pc[1]:
            return 0.0
        f, s, _ = pc[1][-1]
        stripped = s.rstrip()
        return self.run_width(f, s) - self.run_width(f, stripped) if stripped != s else 0.0

    def draw_run(self, layer, shadow_layer, face, s, x, baseline, color):
        if not s.strip():
            return
        if face.bitmap_size or (face in self.fonts.emoji):
            # Colour emoji: draw at the font's own size, then scale to the line.
            size = face.bitmap_size or face.size
            font = face.font
            w = max(1, math.ceil(font.getlength(s)))
            tmp = Image.new("RGBA", (w + 4, round(size * 1.4)), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((2, size * 1.1), s, font=font, embedded_color=True, anchor="ls")
            scale = self.F / size if face.bitmap_size else 1.0
            if scale != 1.0:
                tmp = tmp.resize((max(1, round(tmp.width * scale)), max(1, round(tmp.height * scale))), Image.LANCZOS)
            layer.alpha_composite(tmp, (max(0, round(x - 2 * scale)), max(0, round(baseline - size * 1.1 * scale))))
            return
        mask = Image.new("L", layer.size, 0)
        stroke = max(1, round(self.F / 28)) if face.fake_bold else 0
        ImageDraw.Draw(mask).text((x, baseline), s, font=face.font, fill=255, anchor="ls", stroke_width=stroke,
                                  stroke_fill=255)
        if color[3] < 255:
            mask = mask.point(lambda v, a=color[3]: v * a // 255)
        solid = Image.new("RGBA", layer.size, color[:3] + (0,))
        solid.putalpha(mask)
        layer.alpha_composite(solid)
        if shadow_layer is not None:
            shadow_layer.alpha_composite(solid)

    def with_shadow(self, layer, text_layer):
        F = self.F
        blur, dy = max(1.5, F * 0.14), max(1, round(F * 0.05))
        pad = math.ceil(blur * 2) + dy
        alpha = text_layer.getchannel("A")
        sh = Image.new("L", (layer.width + 2 * pad, layer.height + 2 * pad), 0)
        sh.paste(alpha, (pad, pad + dy))
        sh = sh.filter(ImageFilter.GaussianBlur(blur / 2)).point(lambda v: min(255, v * 0.95 * 1.6))
        out = Image.new("RGBA", sh.size, (0, 0, 0, 0))
        out.putalpha(sh)
        out.alpha_composite(layer, (pad, pad))
        return out.crop((pad, pad, pad + layer.width, pad + layer.height))

    # -- frames
    def last_index(self, t):
        lo, hi = 0, len(self.times)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.times[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    def render(self, k, t):
        """Chat state at time t; returns (RGBA image, any visible emote animated)."""
        W, H, F = self.W, self.H, self.F
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if self.bg[3] > 0:
            ImageDraw.Draw(canvas).rounded_rectangle((0, 0, W - 1, H - 1), radius=self.radius, fill=self.bg) \
                if self.radius > 0 else canvas.paste(self.bg, (0, 0, W, H))
        clip = round(min(self.radius * 0.4, F * 0.4))
        content = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bottom = H - round(F * 0.5)
        any_anim = False
        i = k
        while i >= 0 and bottom > 0:
            layer, h, animated = self.layout(i)
            top = bottom - h
            if top < round(F * 0.3) and i != k:
                break  # don't show a message cut off at the top edge
            paste(content, layer, self.pad_x, top)
            for pic, ph, ax, ay in animated:
                paste(content, pic.frame(t, ph, True), self.pad_x + ax, top + ay)
            any_anim = any_anim or bool(animated)
            bottom = top - self.gap
            i -= 1
        if clip:
            content = content.crop((0, clip, W, H - clip))
            canvas.alpha_composite(content, (0, clip))
        else:
            canvas.alpha_composite(content)
        return canvas, any_anim


def paste(dst, src, x, y):
    """alpha_composite that tolerates negative / out-of-bounds positions."""
    x, y = round(x), round(y)
    sx, sy = max(0, -x), max(0, -y)
    if sx >= src.width or sy >= src.height or x >= dst.width or y >= dst.height:
        return
    dst.alpha_composite(src, (max(0, x), max(0, y)), (sx, sy))


def to_bgra_premultiplied(img):
    r, g, b, a = img.convert("RGBa").split()
    return Image.merge("RGBa", (b, g, r, a)).tobytes()


def main():
    opts = parse_args(sys.argv[1:])
    cache = opts.get("cache") or os.path.join(tempfile.gettempdir(), "streamgrab-emotes")
    if "list-fonts" in opts:
        fams = sorted({f[0] for f in font_index(cache)})
        print(json.dumps(fams))
        return 0
    if not opts.get("input"):
        sys.stderr.write("usage: chatrender.py --input render.json --width W --height H "
                         "(--duration S | --preview-at T --out f.png)\n")
        return 2
    W, H = int(float(opts.get("width", 400))), int(float(opts.get("height", 1080)))
    fps, duration = float(opts.get("fps", 15)), float(opts.get("duration", 60))
    with open(opts["input"], encoding="utf-8") as fh:
        data = json.load(fh)
    r = Renderer(data, W, H, cache)

    if opts.get("preview-at"):
        # --preview-frames N --preview-fps F: if an animated emote is visible, also write N-1 more frames
        # (out-1.png, out-2.png, ...) so the style editor can play the animation.
        t = float(opts["preview-at"])
        k = r.last_index(t)
        img, moving = r.render(k, t)
        img.save(opts["out"], "PNG")
        extra = int(opts.get("preview-frames", 1)) - 1 if moving else 0
        pfps = float(opts.get("preview-fps", 12))
        base = os.path.splitext(opts["out"])[0]
        for i in range(1, extra + 1):
            img, _ = r.render(k, t + i / pfps)
            img.save(f"{base}-{i}.png", "PNG")
        return 0

    out = sys.stdout.buffer
    frames = math.ceil(duration * fps)
    shown, animating, buf = None, False, None
    step = max(1, int(fps * 10))
    try:
        for f in range(frames):
            t = f / fps
            k = r.last_index(t)
            if k != shown or animating or buf is None:
                img, animating = r.render(k, t)
                buf = to_bgra_premultiplied(img)
                shown = k
            out.write(buf)
            if f % step == 0:
                sys.stderr.write(f"frame {f}/{frames}\n")
        out.flush()
    except (BrokenPipeError, OSError):
        pass  # ffmpeg closed the pipe (finished or cancelled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
