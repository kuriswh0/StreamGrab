"""Chat burn-in: style + layout, emote/badge lookup, preview frames and ffmpeg compositing.

The chat panel itself is drawn by a renderer that streams raw frames into ffmpeg, which overlays
them on the video: the Swift `chatrender` tool on macOS (macos/chatrender.swift), and the
Pillow port `chatrender.py` everywhere else.
"""
import base64
import json
import os
import re
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

import chat
import compat

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = compat.cache_dir("emotes")

DEFAULT_STYLE = {
    "layout": "side-right",     # side-right | side-left | overlay
    "width": 24.0,              # panel width, % of video width
    "height": 55.0,             # overlay panel height, % of video height
    "x": 98.0, "y": 96.0,       # overlay position, % of free space (0 = left/top, 100 = right/bottom)
    "font_family": "System",
    "font_size": 24,            # px at 1080p (scales with the video)
    "text_color": "#efeff1",
    "bg_color": "#18181b",
    "bg_opacity": 100,          # %
    "radius": 0,                # corner radius, px at 1080p
    "show_badges": True,
    "show_emotes": True,
    "animate": True,            # animated GIFs: BTTV / FFZ / 7TV emotes move
    "animate_twitch": True,     # animated emotes: Twitch's own animated emotes move
    "third_party": True,        # BTTV / FFZ / 7TV emotes
    "timestamps": False,
    "shadow": False,
}


# Starter presets, added once on first run (users can overwrite or delete them).
STARTER_PRESETS = {
    "Side panel (right)": {"layout": "side-right", "width": 24.0},
    "Overlay, bottom right": {"layout": "overlay", "width": 24.0, "height": 45.0, "x": 98.0, "y": 96.0,
                              "bg_opacity": 55, "radius": 10, "shadow": True},
    "Overlay, top left, see-through": {"layout": "overlay", "width": 22.0, "height": 40.0, "x": 2.0, "y": 4.0,
                                       "bg_opacity": 0, "shadow": True, "font_size": 22},
}


def clean_presets(presets):
    out = {}
    for name, style in (presets or {}).items():
        name = str(name).strip()[:60]
        if name and isinstance(style, dict):
            out[name] = clean_style(style)
    return out


def clean_style(style):
    s = dict(DEFAULT_STYLE)
    for k, v in (style or {}).items():
        if k in DEFAULT_STYLE:
            d = DEFAULT_STYLE[k]
            try:
                s[k] = v if isinstance(d, str) else type(d)(v)
            except (TypeError, ValueError):
                pass
    if s["layout"] not in ("side-right", "side-left", "overlay"):
        s["layout"] = "side-right"
    return s


def even(x):
    return max(2, int(round(x / 2)) * 2)


def geometry(style, vw, vh):
    """Pixel layout of the output video and chat panel for a vw x vh source."""
    s = clean_style(style)
    scale = vh / 1080
    g = {"video_w": vw, "video_h": vh, "font": max(9, round(s["font_size"] * scale)),
         "radius": round(s["radius"] * scale)}
    pw = even(vw * s["width"] / 100)
    if s["layout"] == "overlay":
        ph = even(vh * s["height"] / 100)
        pw = min(pw, even(vw))
        g.update(out_w=vw, out_h=vh, video_x=0, panel_w=pw, panel_h=ph,
                 panel_x=round((vw - pw) * s["x"] / 100), panel_y=round((vh - ph) * s["y"] / 100))
    else:
        left = s["layout"] == "side-left"
        g.update(out_w=even(vw + pw), out_h=even(vh), video_x=pw if left else 0, panel_w=pw, panel_h=even(vh),
                 panel_x=0 if left else vw, panel_y=0)
    return g


# ------------------------------------------------------------------ emotes & badges

def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "StreamGrab"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:  # noqa: BLE001 - emote/badge services are best-effort
        val = hit[1] if hit else {}
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def twitch_user_id(login):
    if not login:
        return None

    def fetch():
        d = chat.twitch_gql({"query": "query($l:String!){user(login:$l){id}}", "variables": {"l": login.lower()}})
        return ((d.get("data") or {}).get("user") or {}).get("id")
    return _cached(("uid", login.lower()), 86400, fetch)


def third_party_emotes(channel_id=None):
    """{code: image_url} from BTTV, FFZ and 7TV (global + channel). Later sources win."""
    def fetch():
        out = {}

        def bttv(items):
            for e in items:
                out[e["code"]] = f"https://cdn.betterttv.net/emote/{e['id']}/2x"

        def ffz(items):
            for e in items:
                imgs = e.get("images") or {}
                url = imgs.get("2x") or imgs.get("1x")
                if url:
                    out[e["code"]] = url

        def seventv(items):
            for e in items:
                data = e.get("data") or {}
                host = (data.get("host") or {}).get("url")
                if host:
                    out[e["name"]] = f"https:{host}/2x.{'gif' if data.get('animated') else 'png'}"

        steps = [
            lambda: bttv(_get_json("https://api.betterttv.net/3/cached/emotes/global")),
            lambda: ffz(_get_json("https://api.betterttv.net/3/cached/frankerfacez/emotes/global")),
            lambda: seventv(_get_json("https://7tv.io/v3/emote-sets/global").get("emotes") or []),
        ]
        if channel_id:
            steps += [
                lambda: (lambda d: bttv((d.get("channelEmotes") or []) + (d.get("sharedEmotes") or [])))(
                    _get_json(f"https://api.betterttv.net/3/cached/users/twitch/{channel_id}")),
                lambda: ffz(_get_json(f"https://api.betterttv.net/3/cached/frankerfacez/users/twitch/{channel_id}")),
                lambda: seventv(((_get_json(f"https://7tv.io/v3/users/twitch/{channel_id}").get("emote_set") or {})
                                 .get("emotes")) or []),
            ]
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - one provider failing shouldn't block the rest
                pass
        return out
    return _cached(("emotes", channel_id), 3600, fetch)


def twitch_badges(login=None):
    """{"setID/version": image_url} for global badges plus the channel's own (sub tiers, bits)."""
    def fetch():
        q = "{badges{setID version imageURL(size:DOUBLE)}}"
        out = {f"{b['setID']}/{b['version']}": b["imageURL"]
               for b in (chat.twitch_gql({"query": q}).get("data") or {}).get("badges") or []}
        if login:
            q = "query($l:String!){user(login:$l){broadcastBadges{setID version imageURL(size:DOUBLE)}}}"
            d = chat.twitch_gql({"query": q, "variables": {"l": login.lower()}})
            for b in (((d.get("data") or {}).get("user") or {}).get("broadcastBadges") or []):
                out[f"{b['setID']}/{b['version']}"] = b["imageURL"]
        return out
    return _cached(("badges", (login or "").lower()), 3600, fetch)


def apply_third_party(messages, emote_map):
    """Turns words matching BTTV/FFZ/7TV codes into emote parts."""
    if not emote_map:
        return messages
    out = []
    for m in messages:
        parts = []
        for p in m["parts"]:
            if "text" not in p or not p["text"]:
                parts.append(p)
                continue
            buf = ""
            for tok in re.split(r"(\s+)", p["text"]):
                url = emote_map.get(tok)
                if url:
                    if buf:
                        parts.append({"text": buf})
                        buf = ""
                    parts.append({"emote": tok, "url": url})
                else:
                    buf += tok
            if buf:
                parts.append({"text": buf})
        out.append({**m, "parts": parts})
    return out


# ------------------------------------------------------------------ renderer binary

_build_lock = threading.Lock()


def renderer_cmd():
    """Command that runs the chat renderer (same CLI on every platform)."""
    env = os.environ.get("STREAMGRAB_RENDERER")
    if env and os.path.exists(env):
        return [sys.executable, env] if env.endswith(".py") else [env]
    if not compat.MAC:
        py = sys.executable
        console = os.path.join(os.path.dirname(py), "python.exe")
        if py.lower().endswith("pythonw.exe") and os.path.exists(console):
            py = console  # pythonw has no reliable stdout; frames are piped to ffmpeg
        return [py, os.path.join(HERE, "chatrender.py")]
    return [renderer_path()]


def renderer_path():
    binary = os.path.join(HERE, "bin", "chatrender")
    source = os.path.join(HERE, "macos", "chatrender.swift")
    with _build_lock:
        stale = os.path.exists(source) and (not os.path.exists(binary)
                                            or os.path.getmtime(source) > os.path.getmtime(binary))
        if stale:
            if not shutil.which("swiftc"):
                raise RuntimeError("Chat burn-in needs the chat renderer. Install Xcode command line tools "
                                   "(xcode-select --install) or run macos/build.sh.")
            os.makedirs(os.path.dirname(binary), exist_ok=True)
            p = subprocess.run(["swiftc", "-O", source, "-o", binary], capture_output=True, text=True)
            if p.returncode != 0:
                raise RuntimeError("Building the chat renderer failed:\n" + p.stderr[-800:])
    if not os.path.exists(binary):
        raise RuntimeError("Chat renderer not found (bin/chatrender). Run macos/build.sh.")
    return binary


def list_fonts():
    def fetch():
        p = subprocess.run([*renderer_cmd(), "--list-fonts", "--cache", CACHE_DIR], capture_output=True, text=True, timeout=300)
        return sorted(set(json.loads(p.stdout)))
    fonts = _cached(("fonts",), 86400, fetch)
    return ["System"] + (fonts or [])


# ------------------------------------------------------------------ render input

def render_input(messages, style, font_px, offset=0, login=None, platform="twitch"):
    """Builds the renderer's JSON: times relative to the video start, emotes and badges resolved."""
    s = clean_style(style)
    msgs = messages
    if s["show_emotes"] and s["third_party"] and platform == "twitch":
        msgs = apply_third_party(msgs, third_party_emotes(twitch_user_id(login)))
    badges = twitch_badges(login) if s["show_badges"] and platform == "twitch" else {}

    # Twitch VOD chat has whole-second timestamps: spread each second's messages across it.
    integral = all(float(m["t"]).is_integer() for m in msgs)
    out, i = [], 0
    while i < len(msgs):
        j = i
        while integral and j + 1 < len(msgs) and msgs[j + 1]["t"] == msgs[i]["t"]:
            j += 1
        n = j - i + 1
        for k in range(i, j + 1):
            m = msgs[k]
            t = m["t"] - offset + ((k - i) / n if integral else 0)
            out.append({"t": t, "ts": chat.fmt_ts(m["t"]), "user": m["user"], "color": m.get("color") or "",
                        "badges": m.get("badges") or [], "parts": m["parts"]})
        i = j + 1
    before = [m for m in out if m["t"] < 0][-60:]  # a little context for the first frame
    out = before + [m for m in out if m["t"] >= 0]
    out = [dict(m, parts=[_still_or_moving(p, s) for p in m["parts"]]) for m in out]
    rs = {k: s[k] for k in ("font_family", "text_color", "bg_color", "bg_opacity", "show_badges",
                            "show_emotes", "timestamps", "shadow")}
    rs["animate"] = s["animate"] or s["animate_twitch"]  # per-emote choice is made in the URLs below
    rs["font_size"] = font_px
    return {"style": rs, "badges": badges, "messages": out}


def _still_or_moving(part, s):
    """Twitch's own animated emotes and third-party GIFs (BTTV/FFZ/7TV) are switched separately.
    Twitch serves a still version at .../static/...; any other image is frozen on its first frame by
    the renderers when its URL ends in #still (fragments aren't sent to the server)."""
    url = part.get("url")
    if not url:
        return part
    if "static-cdn.jtvnw.net/emoticons/" in url:
        if not s["animate_twitch"]:
            return dict(part, url=url.replace("/default/", "/static/"))
    elif not s["animate"]:
        return dict(part, url=url.split("#")[0] + "#still")
    return part


# ------------------------------------------------------------------ preview

DEMO_CHAT = [
    ("Streamer", "#E91916", ["broadcaster/1"], [{"text": "welcome in everyone "}, {"emote": "HeyGuys", "url": "https://static-cdn.jtvnw.net/emoticons/v2/30259/default/dark/1.0"}]),
    ("ModSquad", "#00AD03", ["moderator/1", "subscriber/0"], [{"text": "reminder: be nice in chat"}]),
    ("pixelpanda", "#1E90FF", ["subscriber/0"], [{"text": "that play was insane "}, {"emote": "PogChamp", "url": "https://static-cdn.jtvnw.net/emoticons/v2/305954156/default/dark/1.0"}]),
    ("lurker_42", "", [], [{"text": "LUL LUL"}]),
    ("CozyViewer", "#FF69B4", ["vip/1"], [{"text": "how long have you been streaming today?"}]),
    ("speedrunner", "#9ACD32", ["premium/1"], [{"text": "Clap Clap Clap"}]),
    ("nightowl", "#8A2BE2", ["subscriber/0"], [{"text": "EZ "}, {"emote": "Kappa", "url": "https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/1.0"}]),
    ("first_timer", "", [], [{"text": "just found this stream, the vibes are great FeelsGoodMan"}]),
    ("Streamer", "#E91916", ["broadcaster/1"], [{"text": "thanks for hanging out! "}, {"emote": "BibleThump", "url": "https://static-cdn.jtvnw.net/emoticons/v2/86/default/dark/1.0"}]),
    ("catvibes", "#DAA520", ["subscriber/0"], [{"text": "catJAM catJAM"}]),
    ("ModSquad", "#00AD03", ["moderator/1", "subscriber/0"], [{"text": "!socials for links"}]),
    ("pixelpanda", "#1E90FF", ["subscriber/0"], [{"text": "GG "}, {"emote": "Kreygasm", "url": "https://static-cdn.jtvnw.net/emoticons/v2/41/default/dark/1.0"}]),
]


def demo_messages():
    out = []
    for i, (user, color, badges, parts) in enumerate(DEMO_CHAT * 2):
        out.append({"id": str(i), "t": float(i * 2), "user": user, "color": color, "badges": badges,
                    "parts": [dict(p) for p in parts]})
    return out


def preview(style, messages, at, vw=1920, vh=1080, login=None, platform="twitch"):
    """Renders one chat frame; returns geometry + PNG (base64) for the style editor."""
    g = geometry(style, vw, vh)
    data = render_input(messages, style, g["font"], 0, login, platform)
    data["style"]["radius"] = g["radius"]
    with tempfile.TemporaryDirectory() as tmp:
        inp, out = os.path.join(tmp, "in.json"), os.path.join(tmp, "out.png")
        with open(inp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        anim = s_animates(style)
        p = subprocess.run([*renderer_cmd(), "--input", inp, "--width", str(g["panel_w"]), "--height",
                            str(g["panel_h"]), "--preview-at", str(at), "--out", out, "--cache", CACHE_DIR,
                            "--preview-frames", str(PREVIEW_FRAMES if anim else 1), "--preview-fps", str(PREVIEW_FPS)],
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("Preview render failed: " + (p.stderr or "")[-400:])
        with open(out, "rb") as fh:
            png = base64.b64encode(fh.read()).decode()
        frames = []  # extra frames, only when an animated emote is on screen
        for i in range(1, PREVIEW_FRAMES):
            f = os.path.join(tmp, f"out-{i}.png")
            if not os.path.exists(f):
                break
            with open(f, "rb") as fh:
                frames.append(base64.b64encode(fh.read()).decode())
    return {"geometry": g, "png": png, "frames": frames, "fps": PREVIEW_FPS}


PREVIEW_FRAMES, PREVIEW_FPS = 24, 12  # 2 seconds of animated emotes in the style editor


def s_animates(style):
    return style.get("show_emotes", True) is not False and (
        style.get("animate", True) is not False or style.get("animate_twitch", True) is not False)


# ------------------------------------------------------------------ burn-in

def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration,bit_rate", "-of", "json", path],
                       capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    fmt = d.get("format") or {}
    if not st.get("width"):
        raise RuntimeError(f"Could not read video: {os.path.basename(path)}")
    return {"w": st["width"], "h": st["height"], "duration": float(fmt.get("duration") or 0),
            "bitrate": int(fmt.get("bit_rate") or 0)}


ENCODERS = ("fast", "hardware")


_HW = []


def hardware_encoder():
    """First H.264 hardware encoder that actually works on this machine (or None).
    macOS: VideoToolbox. Windows: NVIDIA NVENC, Intel Quick Sync, AMD AMF, in that order."""
    if not _HW:
        names = ["h264_videotoolbox"] if compat.MAC else ["h264_nvenc", "h264_qsv", "h264_amf"]
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        found = None
        for name in (n for n in names if n in listed):
            # Listed only means "compiled in"; a one-frame test tells us whether the GPU is there.
            t = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                                "-frames:v", "1", "-c:v", name, "-f", "null", "-"], capture_output=True, timeout=60)
            if t.returncode == 0:
                found = name
                break
        _HW.append(found)
    return _HW[0]


def _encoder_args(out_w, out_h, src, mode="fast"):
    """fast: x264 on all CPU cores (~2x faster on Apple Silicon).
    hardware: the GPU's video encoder (VideoToolbox / NVENC / Quick Sync / AMF); leaves the CPU free."""
    hw = hardware_encoder() if mode == "hardware" else None
    if hw:
        # Bitrate scaled from the source to the (possibly wider) output, with headroom for re-encoding.
        area = (out_w * out_h) / max(1, src["w"] * src["h"])
        base = src["bitrate"] or out_h * 7000
        rate = int(min(max(base * area * 1.3, 2_500_000), 25_000_000))
        args = ["-c:v", hw, "-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)]
        return args + (["-allow_sw", "1"] if hw == "h264_videotoolbox" else [])
    return ["-c:v", "libx264", "-preset", "superfast", "-crf", "21"]


def burn(video, messages, out_path, style, offset=0, login=None, platform="twitch",
         progress=None, cancel=None, on_procs=None, encoder="fast"):
    """Renders chat onto `video`, writing `out_path`. Returns out_path."""
    src = probe(video)
    s = clean_style(style)
    g = geometry(s, src["w"], src["h"])
    data = render_input(messages, s, g["font"], offset, login, platform)
    data["style"]["radius"] = g["radius"]
    fps = 30 if s["animate"] and s["show_emotes"] else 15
    duration = src["duration"]

    with tempfile.TemporaryDirectory() as tmp:
        inp = os.path.join(tmp, "chat.json")
        with open(inp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        rcmd = [*renderer_cmd(), "--input", inp, "--width", str(g["panel_w"]), "--height", str(g["panel_h"]),
                "--fps", str(fps), "--duration", f"{duration:.3f}", "--cache", CACHE_DIR]
        # Converting the chat frames to YUV first lets overlay blend in the video's own format
        # (instead of converting every full video frame to RGB): ~25% faster.
        ov = f"overlay={g['panel_x']}:{g['panel_y']}:alpha=premultiplied:eof_action=pass:format=yuv420"
        chat_in = "[1:v]format=yuva420p[c];"
        if s["layout"] == "overlay":
            graph = f"{chat_in}[0:v]format=yuv420p[b];[b][c]{ov}[v]"
        else:
            graph = (f"{chat_in}[0:v]format=yuv420p,pad={g['out_w']}:{g['out_h']}:{g['video_x']}:0:color=black[b];"
                     f"[b][c]{ov}[v]")
        fcmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", video,
                "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{g['panel_w']}x{g['panel_h']}",
                "-framerate", str(fps), "-i", "pipe:0",
                "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
                *_encoder_args(g["out_w"], g["out_h"], src, encoder),
                "-movflags", "+faststart", "-progress", "pipe:1", out_path]
        errlog = open(os.path.join(tmp, "ffmpeg.log"), "w+")
        rproc = subprocess.Popen(rcmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        fproc = subprocess.Popen(fcmd, stdin=rproc.stdout, stdout=subprocess.PIPE, stderr=errlog, text=True)
        rproc.stdout.close()
        if on_procs:
            on_procs([rproc, fproc])
        for line in fproc.stdout:
            if cancel and cancel.is_set():
                break
            if line.startswith("out_time_us=") and duration:
                try:
                    if progress:
                        progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
        if cancel and cancel.is_set():
            for p in (fproc, rproc):
                p.kill()
            fproc.wait()
            errlog.close()
            if os.path.exists(out_path):
                os.remove(out_path)
            raise chat.Cancelled()
        fproc.wait()
        rproc.wait()
        errlog.seek(0)
        log = errlog.read()
        errlog.close()
        if fproc.returncode != 0:
            tail = [l for l in log.splitlines() if l.strip()][-6:]
            raise RuntimeError("Chat burn-in failed:\n" + "\n".join(tail))
    return out_path
