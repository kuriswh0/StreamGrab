"""YouTube captions as .srt: picks the best track, downloads it, cleans it up and trims it to a section.

Uploaded captions are used when the video has them, otherwise YouTube's automatic captions in the video's
own language. Automatic captions come as TTML (one clean line per caption; the VTT version repeats every
line as the text rolls on), with overlapping times and style tags that are cleaned up here.
"""
import glob
import os
import re
import subprocess
import tempfile

TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})")


def pick_track(info):
    """("en", False) = uploaded English captions, ("en-orig", True) = automatic ones, or None."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    lang = (info.get("language") or "").split("-")[0].lower()
    for want in ([lang] if lang else []) + ["en"]:
        for k in manual:
            if k.split("-")[0].lower() == want:
                return k, False
    if manual:
        return next(iter(manual)), False
    for want in ([f"{lang}-orig", lang] if lang else []) + ["en-orig", "en"]:
        if want in auto:
            return want, True
    return None


def describe(track):
    if not track:
        return ""
    lang, auto = track
    return f"{lang.replace('-orig', '')} (automatic)" if auto else lang


def _sec(t):
    h, m, s, ms = (int(x) for x in TIME.match(t).groups())
    return h * 3600 + m * 60 + s + ms / 1000


def _ts(x):
    x = max(0.0, x)
    ms = int(round(x * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def parse(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.split("\n")
        i = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if i is None:
            continue
        a, b = (p.strip().split(" ")[0] for p in lines[i].split("-->"))
        if not (TIME.match(a) and TIME.match(b)):
            continue
        body = re.sub(r"<[^>]+>", "", "\n".join(lines[i + 1:]))
        body = "\n".join(l.strip() for l in body.split("\n") if l.strip())
        if body:
            cues.append([_sec(a), _sec(b), body])
    return cues


def clean(cues):
    """Drops repeats and makes each caption end when the next one starts (no overlaps)."""
    out = []
    for c in sorted(cues, key=lambda c: c[0]):
        if out and c[2] == out[-1][2]:
            out[-1][1] = max(out[-1][1], c[1])
            continue
        out.append(list(c))
    for a, b in zip(out, out[1:]):
        a[1] = min(a[1], b[0]) if b[0] > a[0] else a[1]
    return [c for c in out if c[1] - c[0] > 0.05]


def trim(cues, start=None, end=None):
    """Keeps start..end and shifts it to begin at 0:00 (for downloaded sections)."""
    s = start or 0
    out = []
    for a, b, t in cues:
        if b <= s or (end is not None and a >= end):
            continue
        a2, b2 = max(a, s) - s, (min(b, end) if end is not None else b) - s
        out.append([a2, b2, t])
    return out


def write(path, cues):
    with open(path, "w", encoding="utf-8") as fh:
        for n, (a, b, t) in enumerate(cues, 1):
            fh.write(f"{n}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n")


def download(url, track, out_path, start=None, end=None, cancel=None):
    """Downloads `track` (from pick_track) as a clean .srt at out_path; returns the number of captions."""
    lang, auto = track
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--skip-download", "--sub-langs", lang,
               "--write-auto-subs" if auto else "--write-subs", "--sub-format", "ttml/vtt/best",
               "--convert-subs", "srt", "-o", os.path.join(tmp, "c.%(ext)s"), url]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        files = glob.glob(os.path.join(tmp, "*.srt"))
        if not files:
            raise RuntimeError("Captions couldn't be downloaded: " + ((p.stderr or p.stdout or "").strip().splitlines() or ["no file"])[-1][:200])
        with open(files[0], encoding="utf-8", errors="replace") as fh:
            cues = clean(parse(fh.read()))
    if start is not None or end is not None:
        cues = trim(cues, start, end)
    write(out_path, cues)
    return len(cues)
