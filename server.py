#!/usr/bin/env python3
"""StreamGrab - local Twitch / YouTube VOD + chat downloader.

Runs a small web server on 127.0.0.1 and opens the UI in your browser.
Requires yt-dlp and ffmpeg on PATH (brew install yt-dlp ffmpeg).
"""
import argparse
import base64
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import compat  # first: patches subprocess on Windows

# The self-contained Mac app's ffmpeg is built with OpenSSL but has no certificate list, so it can't open
# https streams (chat style preview frames, trimmed YouTube downloads). Point it at the one macOS ships.
# (The Windows build uses Schannel, which reads Windows' own certificate store.)
if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE") and os.path.exists("/etc/ssl/cert.pem"):
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
import chat
import convert
import fastdl
import live
import captions
import render
import updater

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
try:
    with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as _fh:
        APP_VERSION = _fh.read().strip()
except OSError:
    APP_VERSION = "dev"
# Releases are published here (see .github/workflows/release.yml); the app checks it for updates.
UPDATE_REPO = "kuriswh0/StreamGrab"
SETTINGS_PATH = os.path.expanduser("~/.streamgrab.json")
DEFAULT_SETTINGS = {
    "output_dir": os.path.expanduser("~/Downloads/StreamGrab"),
    "max_parallel": 2,
    "fragments": 8,
    "chat_workers": 8,
    "subfolder_per_channel": True,
    "chat_style": dict(render.DEFAULT_STYLE),
    "chat_presets": {},          # name -> chat style
    "bake_encoder": "gpu",       # gpu | balanced | max (see render.ENCODERS); each download can override it
    "download_connections": 12,  # parallel segment downloads for trimmed Twitch VODs
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            s.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    s["chat_style"] = render.clean_style(s.get("chat_style"))
    if "presets_seeded" not in s:  # first run: offer a few starting points
        s["chat_presets"] = {**render.clean_presets(render.STARTER_PRESETS), **(s.get("chat_presets") or {})}
        s["presets_seeded"] = True
    s["chat_presets"] = render.clean_presets(s.get("chat_presets"))
    # Until 1.0.4 the default was "fast" (all CPU cores), which could pin the CPU at 100%: start on the GPU.
    if not s.get("encoder_v2"):
        s["bake_encoder"], s["encoder_v2"] = "gpu" if s.get("bake_encoder") in (None, "fast", "hardware") else s["bake_encoder"], True
    s["bake_encoder"] = render.encoder_mode(s.get("bake_encoder"))
    return s


SETTINGS = load_settings()
PLATFORM = "windows" if compat.WINDOWS else "mac" if compat.MAC else "linux"
LAST_SEEN = 0.0  # last request from the UI


def save_settings():
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(SETTINGS, fh, indent=2)


# ------------------------------------------------------------------ helpers

def sanitize(name, limit=150):
    if compat.WINDOWS:
        limit = min(limit, 110)  # Windows paths stop at 260 characters unless long paths are enabled
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:limit] or "untitled"


def parse_time(value):
    """'1:02:03', '62:03', '3723' or '' -> seconds (or None)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    secs = 0.0
    for part in str(value).strip().split(":"):
        secs = secs * 60 + float(part or 0)
    return secs


def platform_of(info):
    ex = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if ex.startswith("twitch"):
        return "twitch"
    if ex.startswith("youtube"):
        return "youtube"
    return "other"


def twitch_vod_id(info, url):
    if (info.get("extractor_key") or "").lower() != "twitchvod":
        return None
    m = re.search(r"videos/(\d+)", url) or re.match(r"v?(\d+)$", str(info.get("id", "")))
    return m.group(1) if m else None


def run_json(cmd, timeout=90):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        err = [l for l in p.stderr.splitlines() if "ERROR" in l] or p.stderr.splitlines()[-3:]
        raise RuntimeError("\n".join(err) or "yt-dlp failed")
    return json.loads(p.stdout)


INFO_CACHE = {}
LIVE_STAMP = re.compile(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def clean_title(info):
    """yt-dlp appends ' YYYY-MM-DD HH:MM' to the titles of anything still live (including Twitch
    VODs that are still recording). Our filenames already start with the date, so drop it."""
    title = info.get("title") or info.get("id") or ""
    if info.get("is_live"):
        full = info.get("fulltitle")
        title = full if full and title.startswith(full) else LIVE_STAMP.sub("", title)
    return title


def get_info(url):
    if url in INFO_CACHE:
        return INFO_CACHE[url]
    info = run_json(["yt-dlp", "-J", "--no-playlist", "--no-warnings", url])
    plat = platform_of(info)
    qualities = {}
    for f in info.get("formats") or []:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        fps = round(f.get("fps") or 30)
        label = f"{h}p" + (str(fps) if fps > 30 else "")
        if label not in qualities:
            qualities[label] = {"height": h, "fps": max(fps, 30), "label": label}
    subs = info.get("subtitles") or {}
    summary = {
        "url": info.get("webpage_url") or url,
        "id": info.get("id"),
        "platform": plat,
        "kind": info.get("extractor_key"),
        "title": clean_title(info),
        "channel": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "upload_date": info.get("upload_date") or "",
        "thumbnail": info.get("thumbnail") or "",
        "is_live": bool(info.get("is_live")),
        "qualities": sorted(qualities.values(), key=lambda q: (-q["height"], -q["fps"])),
        "chat": bool(twitch_vod_id(info, url)) or (plat == "youtube" and "live_chat" in subs),
        "login": (info.get("uploader_id") or "").lower() if plat == "twitch" else "",
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
    }
    track = captions.pick_track(info) if plat == "youtube" else None
    summary["captions"] = captions.describe(track)  # e.g. "en" or "en (automatic)"; "" = none
    summary["caption_track"] = list(track) if track else None
    if plat in ("twitch", "youtube"):
        # A frame for the chat style preview, grabbed in the background so it's usually ready by the
        # time the Chat style tab is opened: the middle of the video (VODs still being recorded
        # included), or the current moment for a live channel.
        d = summary["duration"]
        summary["poster_points"] = [int(d * f) for f in POSTER_FRACTIONS] if d else [0]
        summary["poster_t"] = summary["poster_points"][len(summary["poster_points"]) // 2]
        # Grab all of them at once in the background (each is a separate ffmpeg seek), so the
        # refresh button is instant. The stream URL is looked up once first.
        def prefetch(url=summary["url"], pts=summary["poster_points"]):
            _quiet(full_frame, url, pts[len(pts) // 2])  # the one shown first
            for t in pts:
                threading.Thread(target=_quiet, args=(full_frame, url, t), daemon=True).start()
        threading.Thread(target=prefetch, daemon=True).start()
    INFO_CACHE[url] = summary
    return summary


def browse_channel(platform, query, tab="videos", limit=30):
    query = query.strip()
    if platform == "twitch":
        login = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", query).split("/")[0].lower()
        types = {"videos": None, "archive": "ARCHIVE", "highlights": "HIGHLIGHT", "uploads": "UPLOAD"}
        vtype = types.get(tab)
        q = ("query($login:String!,$first:Int!,$type:BroadcastType){user(login:$login){displayName "
             "videos(first:$first,sort:TIME,type:$type){edges{node{id title lengthSeconds createdAt "
             "viewCount broadcastType previewThumbnailURL(width:320,height:180)}}}}}")
        data = chat.twitch_gql({"query": q, "variables": {"login": login, "first": limit, "type": vtype}})
        user = (data.get("data") or {}).get("user")
        if not user:
            raise RuntimeError(f"Twitch channel '{login}' not found")
        return {"channel": user["displayName"], "items": [{
            "url": f"https://www.twitch.tv/videos/{n['id']}",
            "title": n["title"], "duration": n["lengthSeconds"],
            "date": n["createdAt"][:10], "thumbnail": n["previewThumbnailURL"],
            "views": n["viewCount"], "type": n["broadcastType"].lower(),
        } for n in (e["node"] for e in user["videos"]["edges"])]}

    # YouTube
    if query.startswith("http"):
        base = re.sub(r"/(videos|streams|shorts|featured)/?$", "", query.rstrip("/"))
    else:
        handle = query if query.startswith("@") else "@" + query
        base = f"https://www.youtube.com/{handle}"
    tab = tab if tab in ("videos", "streams", "shorts") else "videos"
    info = run_json(["yt-dlp", "-J", "--flat-playlist", "--playlist-end", str(limit),
                     "--no-warnings", f"{base}/{tab}"])
    items = []
    for e in info.get("entries") or []:
        thumbs = e.get("thumbnails") or [{}]
        items.append({
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title"), "duration": e.get("duration") or 0,
            "date": "", "thumbnail": thumbs[-1].get("url", ""),
            "views": e.get("view_count"), "type": tab,
        })
    return {"channel": info.get("channel") or info.get("uploader") or query, "items": items}


# ------------------------------------------------------------------ chat style preview

_SAMPLES = {}


def preview_sample(url, at):
    """Chat to show in the style preview: real chat around `at` from a Twitch VOD, or demo chat."""
    if url:
        try:
            info = get_info(url)
            vod = twitch_vod_id({"extractor_key": info["kind"], "id": info["id"]}, url)
            if vod:
                at = float(at if at not in (None, "") else min(600, (info["duration"] or 0) / 2))
                key = (vod, int(at))
                if key not in _SAMPLES:
                    page = chat._comments_page(vod, offset=max(0, at - 30))
                    _SAMPLES[key] = sorted((chat._twitch_msg(e["node"]) for e in page.get("edges") or []),
                                           key=lambda m: m["t"])
                msgs = [m for m in _SAMPLES[key] if m["t"] <= at + 1]
                if msgs:
                    return msgs, at + 1, info.get("login"), "twitch"
        except Exception:  # noqa: BLE001 - fall back to demo chat
            pass
    demo = render.demo_messages()
    return demo, demo[-1]["t"] + 0.5, None, "twitch"


# ------------------------------------------------------------------ preview frames

_STREAM_URLS, _FRAMES = {}, {}
_ACTIVITY = {}  # Twitch VOD id -> chat activity for the trim timeline
POSTER_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)  # the 5 preview frames the refresh button cycles through


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001 - best-effort prefetch
        pass


def full_frame(url, t, fresh=False):
    """A real full-resolution frame at time t (JPEG bytes), grabbed from the stream with ffmpeg.
    fresh=True skips the cache (a live stream's latest frame)."""
    key = (url, int(t))
    if key in _FRAMES and not fresh:
        return _FRAMES[key]
    if url not in _STREAM_URLS:
        p = subprocess.run(["yt-dlp", "-g", "--no-playlist", "--no-warnings", "-f",
                            "best[height<=1080][vcodec!=none]/bv*[height<=1080]/b", url],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError("Could not get the video stream")
        _STREAM_URLS[url] = p.stdout.strip().splitlines()[0]
    seek = ["-ss", f"{t:.2f}"] if t > 0 else []  # t = 0: live stream, take the latest frame
    p = subprocess.run(["ffmpeg", "-v", "error", *seek, "-i", _STREAM_URLS[url],
                        "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "-"],
                       capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        _STREAM_URLS.pop(url, None)  # stream URLs expire; fetch a new one next time
        raise RuntimeError("Could not grab a frame")
    if len(_FRAMES) > 60:
        _FRAMES.clear()
    _FRAMES[key] = p.stdout
    return p.stdout


# ------------------------------------------------------------------ update check

_UPDATE = {}
GPU = {}  # {"name": "NVIDIA" / None} once checked (a quick ffmpeg test at startup)


def _check_gpu():
    try:
        GPU["name"] = render.gpu_name()
    except Exception:  # noqa: BLE001 - no GPU encoder: the app falls back to the CPU
        GPU["name"] = None


threading.Thread(target=_check_gpu, daemon=True).start()


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def update_status(force=False):
    """Latest GitHub release vs this version. Cached for 6 hours (force: ask GitHub now); never raises."""
    if not force and _UPDATE.get("t", 0) > time.time() - 6 * 3600:
        return _UPDATE["data"]
    data = {"current": APP_VERSION, "newer": False}
    try:
        headers = {"User-Agent": "StreamGrab", "Accept": "application/vnd.github+json"}
        if os.environ.get("STREAMGRAB_GITHUB_TOKEN"):  # only the CI tests set this (shared IPs hit GitHub's anonymous limit)
            headers["Authorization"] = "Bearer " + os.environ["STREAMGRAB_GITHUB_TOKEN"]
        req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.load(r)
        latest = str(rel.get("tag_name") or "").lstrip("v")
        url = rel.get("html_url") or ""
        data.update(latest=latest, url=url if url.startswith(f"https://github.com/{UPDATE_REPO}/") else "",
                    newer=APP_VERSION != "dev" and _version_tuple(latest) > _version_tuple(APP_VERSION))
        asset = updater.pick_asset(rel.get("assets"))
        data["auto"] = bool(asset)  # this copy can download + install it itself
        _UPDATE["asset"] = asset
    except Exception as e:  # noqa: BLE001 - offline, rate-limited or no releases yet: just no notice
        data["error"] = str(e)[:200]
    _UPDATE.update(t=time.time(), data=data)
    return data


# ------------------------------------------------------------------ jobs

JOBS = {}
JOBS_LOCK = threading.Lock()
SLOTS = threading.Semaphore(SETTINGS["max_parallel"])
CONVERT_SLOTS = threading.Semaphore(max(2, min(6, (os.cpu_count() or 4) // 2)))  # audio jobs don't block downloads
UPLOADS = compat.cache_dir("uploads")  # files dropped onto the converter
PROG_RE = re.compile(r"^@@P (.*)$")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}" if unit == "MB" else f"{n:.2f} {unit}"
        n /= 1024


signal_group = compat.signal_group
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)  # Windows has no SIGKILL


_BASES, _BASES_LOCK = set(), threading.Lock()


def reserve_base(base_path, exts):
    """A file name stem that no running job is using and that none of this job's outputs (exts) exist for.
    Without this, two downloads of the same video share yt-dlp's temp files and corrupt each other,
    and a second download of a finished video is skipped by yt-dlp ("already downloaded")."""
    with _BASES_LOCK:
        n = 1
        while True:
            cand = base_path if n == 1 else f"{base_path} ({n})"
            if cand not in _BASES and not any(os.path.exists(cand + e) for e in exts):
                _BASES.add(cand)
                return cand
            n += 1


def release_base(base_path):
    with _BASES_LOCK:
        _BASES.discard(base_path)


def unique_path(path):
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(path):
        path = f"{root} ({i}){ext}"
        i += 1
    return path


FORBIDDEN_RE = re.compile(r"HTTP Error 403|403: Forbidden")
# Audio-only downloads: format -> (yt-dlp --audio-format, --audio-quality)
AUDIO_FORMATS = {"m4a": ("m4a", None), "mp3": ("mp3", "320K"), "mp3-192": ("mp3", "192K"),
                 "wav": ("wav", None), "flac": ("flac", None), "opus": ("opus", None)}


def output_dir(channel, root=None):
    """root: a folder chosen for this job on the Download page; defaults to the folder in Settings."""
    outdir = os.path.expanduser(root or SETTINGS["output_dir"])
    if SETTINGS.get("subfolder_per_channel") and channel:
        outdir = os.path.join(outdir, sanitize(channel, 80))
    os.makedirs(outdir, exist_ok=True)
    return outdir


class Job:
    """kind: "vod" (download), "live" (record a live stream), "burn" (re-render chat onto a download)
    or "convert" (audio file conversion)."""

    def __init__(self, url, opts):
        self.id = uuid.uuid4().hex[:10]
        self.url = url
        self.opts = opts
        self.kind = ("live" if opts.get("live") else "burn" if opts.get("burn_from")
                     else "convert" if opts.get("convert") else "vod")
        self.status = "queued"
        self.stage = "Waiting"
        self.progress = 0.0
        self.detail = ""
        self.files = []
        self.error = ""
        self.info = {}
        self.created = time.time()
        self.cancel = threading.Event()
        self.stop_req = threading.Event()  # live: stop recording gracefully and keep the file
        self.proc = None
        self.procs = []
        self.messages = None   # chat (normalized) once downloaded
        self.chat_offset = 0   # seconds into the chat timeline where the video starts

    def public(self):
        d = {k: getattr(self, k) for k in ("id", "url", "kind", "status", "stage", "progress", "detail",
                                            "files", "error", "info", "opts", "created")}
        d["can_burn"] = (self.status == "done" and any(f.endswith(".chat.json") for f in self.files)
                         and any(f.endswith(".mp4") for f in self.files))
        d["stopping"] = self.stop_req.is_set()
        return d

    # -- execution
    def run(self):
        if self.kind == "live":  # recordings don't take a download slot (they can wait for hours)
            return self._guarded(self._run_live)
        if self.kind == "convert":
            with CONVERT_SLOTS:
                return self._guarded(self._run_convert)
        with SLOTS:
            self._guarded(self._run_burn if self.kind == "burn" else self._run_vod)

    def _guarded(self, fn):
        if self.cancel.is_set():
            self.status = "cancelled"
            return
        self.status = "running"
        try:
            fn()
            self.status = "cancelled" if self.cancel.is_set() else "done"
            if self.status == "done":
                self.stage, self.progress = "Finished", 1.0
                if "ETA" in self.detail:
                    self.detail = ""
        except chat.Cancelled:
            self.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            self.status = "cancelled" if self.cancel.is_set() else "error"
            self.error = str(e)

    def _run_vod(self):
        o = self.opts
        self.stage = "Fetching info"
        self.info = info = get_info(self.url)
        start, end = parse_time(o.get("start")), parse_time(o.get("end"))
        trimmed = start is not None or end is not None
        self.clip_start = None  # where a trimmed download really starts (set by the cutting step)

        d = info["upload_date"]
        date = f"{d[:4]}-{d[4:6]}-{d[6:]} " if len(d) == 8 else ""
        base = sanitize(f"{date}{info['title'][:120]} [{info['id']}]", 200)
        if trimmed:
            tag = f"{chat.fmt_ts(start or 0)}-{chat.fmt_ts(end) if end else 'end'}".replace(":", ".")
            base += f" ({tag})"
        exts = []  # what this job will write, to check for existing files
        if o.get("video", True):
            fmt = o.get("audio_format") if o.get("audio_format") in AUDIO_FORMATS else "m4a"
            exts.append("." + AUDIO_FORMATS[fmt][0] if o.get("audio_only") else ".mp4")
        if o.get("chat"):
            exts += [".chat.json", ".chat.txt", ".chat.html"]
        if o.get("captions"):
            exts.append(".srt")
        base_path = reserve_base(os.path.join(output_dir(info["channel"], o.get("output_dir")), base), exts)
        try:
            video_file = None
            if o.get("video", True):
                video_file = self._download_video(base_path, start, end)
            if self.cancel.is_set():
                return
            # A cut without re-encoding starts on a keyframe, slightly before the chosen start: chat,
            # captions and the baked-in chat use that real start so they stay in sync with the video.
            if trimmed and getattr(self, "clip_start", None) is not None:
                start = self.clip_start
            if o.get("captions") and info.get("caption_track"):
                self._download_captions(base_path, start, end)
            if self.cancel.is_set():
                return
            if o.get("chat") and info["chat"]:
                self._download_chat(base_path, start, end, video_file)
                self.chat_offset = start or 0
                if o.get("burn") and video_file and not o.get("audio_only") and self.messages is not None:
                    self._burn(video_file, base_path)
        finally:
            release_base(base_path)

    def _download_video(self, base_path, start, end):
        o = self.opts
        audio_only = o.get("audio_only")
        self.stage = "Downloading audio" if audio_only else "Downloading video"
        youtube = self.info.get("platform") == "youtube"
        trimmed = start is not None or end is not None
        # YouTube sends each video as one big file over one (often throttled) connection. "dashy" makes
        # yt-dlp fetch it in chunks, -N at a time: ~11x faster in testing (101 MB in 3.9 s instead of 45 s).
        # A section of a video up to an hour long is quicker to get whole this way and cut here than to
        # stream through ffmpeg (which runs at about 2x real time).
        cut_here = youtube and trimmed and 0 < (self.info.get("duration") or 0) <= 3600
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--newline", "--progress",
               "-N", str(SETTINGS["fragments"]), "-f", "ba/b" if audio_only else self._format(),
               "-o", base_path + (".full" if cut_here else "") + ".%(ext)s",
               "--progress-template",
               "download:@@P %(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
               "%(progress.total_bytes_estimate)s|%(progress._speed_str)s|%(progress._eta_str)s",
               "--print", "after_move:@@F %(filepath)s"]
        if audio_only:
            # audio_format: "m4a" keeps the original AAC (no re-encode); the others convert with ffmpeg.
            fmt = o.get("audio_format") if o.get("audio_format") in AUDIO_FORMATS else "m4a"
            cmd += ["-x", "--audio-format", AUDIO_FORMATS[fmt][0], "--embed-metadata"]
            if AUDIO_FORMATS[fmt][1]:
                cmd += ["--audio-quality", AUDIO_FORMATS[fmt][1]]
            if fmt in ("mp3", "mp3-192", "m4a"):
                cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
        else:
            cmd += ["--merge-output-format", "mp4"]
        if trimmed and not cut_here:
            if not audio_only and self.info.get("kind") == "TwitchVod":
                out = self._fast_section(base_path, start, end)
                if out:
                    return out
            cmd += ["--download-sections", f"*{start or 0}-{end if end is not None else 'inf'}"]
        cmd.append(self.url)

        # YouTube sometimes refuses a download link with "HTTP Error 403: Forbidden". A new attempt gets fresh
        # links, and the last one asks as YouTube's embedded player does, which still gets full-quality formats
        # (other client types tested in Nov 2026 only offered 360p).
        attempts = [["formats=dashy"], ["formats=dashy"], ["formats=dashy", "player_client=web_embedded"]] if youtube else [[]]
        first_errs = None
        for n, ea in enumerate(attempts):
            extra = ["--extractor-args", "youtube:" + ";".join(ea)] if ea else []
            final, errs = self._run_ytdlp(cmd[:-1] + extra + cmd[-1:], base_path, None if cut_here else start,
                                          None if cut_here else end)
            if self.cancel.is_set():
                return None
            if errs is None:
                break
            forbidden = FORBIDDEN_RE.search("\n".join(errs))
            if n == 0 and not forbidden:
                raise RuntimeError("\n".join(errs))
            first_errs = first_errs or errs
            if n == len(attempts) - 1 or (n > 0 and not forbidden and len(ea) < 2):
                raise RuntimeError("\n".join(first_errs))  # report the 403, not whatever the fallback hit
            self.stage, self.progress = "Retrying", 0.0
            self.detail = f"YouTube refused the download (403), trying again ({n + 2} of {len(attempts)})"
            if self.cancel.wait(3 * (n + 1)):
                return None
            self.stage = "Downloading audio" if self.opts.get("audio_only") else "Downloading video"
        if final and cut_here:
            final = self._cut(final, base_path, start, end)
        if final:
            self.files.append(final)
        return final

    def _download_captions(self, base_path, start, end):
        """YouTube captions as an .srt next to the video (same name, so players load it automatically).
        A failure doesn't fail the download: the job just says so."""
        self.stage, self.detail = "Downloading captions", ""
        try:
            n = captions.download(self.url, tuple(self.info["caption_track"]), base_path + ".srt", start, end)
            self.files.append(base_path + ".srt")
            if not n:
                self.detail = "No captions in this section"
        except Exception as e:  # noqa: BLE001
            self.detail = f"Captions failed: {str(e)[:150]}"

    def _cut(self, full, base_path, start, end):
        """Cuts start-end out of a whole download (no re-encoding, like yt-dlp's own sections)."""
        self.stage, self.detail = "Cutting the section", ""
        out = base_path + os.path.splitext(full)[1]
        # Start on a keyframe (see fastdl.keyframe_at_or_before), so picture and sound both start at 0.
        audio_only = os.path.splitext(full)[1].lower() in (".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg")
        start = (start or 0) if audio_only else fastdl.keyframe_at_or_before(full, start or 0)
        self.clip_start = start
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-t", f"{end - start:.3f}"]
        cmd += ["-i", full, "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
        if out.endswith((".mp4", ".m4a")):
            cmd += ["-movflags", "+faststart"]
        p = subprocess.run(cmd + [out], capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("Cutting the section failed:\n" + (p.stderr or "")[-400:])
        try:
            os.remove(full)
        except OSError:
            pass
        return out

    def _run_ytdlp(self, cmd, base_path, start, end):
        """Runs one yt-dlp attempt. Returns (final_path, None) on success, or (None, error_lines)."""
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, start_new_session=True)
        if start is not None or end is not None:
            # Trimmed sections are fetched by ffmpeg, which reports no progress: show size + speed.
            threading.Thread(target=self._watch_size, args=(base_path, self.proc, (end or self.info.get("duration") or 0) - (start or 0)),
                             daemon=True).start()
        tail, final = [], None
        for line in self.proc.stdout:
            line = line.rstrip()
            m = PROG_RE.match(line)
            if m:
                done, total, est, speed, eta = (m.group(1).split("|") + [""] * 5)[:5]
                try:
                    tot = float(total if total not in ("NA", "None") else est)
                    self.progress = min(1.0, float(done) / tot) if tot else self.progress
                except ValueError:
                    pass
                self.detail = f"{speed.strip()}  ETA {eta.strip()}"
            elif line.startswith("@@F "):
                final = line[4:]
            else:
                tail = (tail + [line])[-20:]
                if "[Merger]" in line or "[ExtractAudio]" in line or "[FixupM3u8]" in line:
                    self.stage, self.detail = "Post-processing", ""
        self.proc.wait()
        if self.proc.returncode != 0 and not self.cancel.is_set():
            return None, [l for l in tail if "ERROR" in l] or tail[-4:]
        return final, None

    def _fast_section(self, base_path, start, end):
        """Trimmed Twitch VODs: fetch only the needed segments, many at once (see fastdl.py)."""
        out = base_path + ".mp4"

        def prog(frac, speed, eta, done):
            self.progress = frac
            self.detail = f"{human_size(done)}  ·  {human_size(speed)}/s  ETA {chat.fmt_ts(eta)}"
        try:
            _, self.clip_start = fastdl.download_section(self.url, self._format(), out, start, end,
                                                         workers=int(SETTINGS.get("download_connections") or 12),
                                                         progress=prog, cancel=self.cancel)
        except fastdl.Unsupported:
            return None  # fall back to yt-dlp
        self.files.append(out)
        return out

    def _watch_size(self, base_path, proc, seconds):
        prefix, prev, prev_t = glob.escape(base_path), 0, time.time()
        while proc.poll() is None and not self.cancel.is_set():
            time.sleep(2)
            size = sum(os.path.getsize(f) for f in glob.glob(prefix + ".*")
                       if os.path.exists(f) and not f.endswith((".json", ".html", ".txt")))
            now = time.time()
            speed = (size - prev) / max(0.1, now - prev_t)
            prev, prev_t = size, now
            if self.stage.startswith("Downloading"):
                self.detail = f"{human_size(size)} downloaded  ·  {human_size(max(0, speed))}/s"

    def _format(self):
        h, fps = self.opts.get("height"), self.opts.get("fps")
        if h:
            lim = f"[height<={int(h)}]" + (f"[fps<={int(fps)}]" if fps else "")
            return f"bv*{lim}+ba/b{lim}/bv*+ba/b"
        return "bv*+ba/b"

    def _download_chat(self, base_path, start, end, video_file):
        info = self.info
        self.stage, self.progress, self.detail = "Downloading chat", 0.0, ""

        def prog(frac, count):
            if frac is not None:
                self.progress = frac
            if count is not None:
                self.detail = f"{count:,} messages"

        if info["platform"] == "twitch":
            vod_id = twitch_vod_id({"extractor_key": info["kind"], "id": info["id"]}, self.url)
            msgs = chat.download_twitch_chat(vod_id, info["duration"], start or 0, end,
                                             workers=SETTINGS["chat_workers"],
                                             progress=prog, cancel=self.cancel)
        else:
            self.detail = "fetching live chat replay..."
            with tempfile.TemporaryDirectory() as tmp:
                msgs = chat.download_youtube_chat(self.url, tmp, start or 0, end,
                                                  cancel=self.cancel, progress=prog)
            prog(1.0, len(msgs))
        meta = {k: info.get(k) for k in ("id", "title", "channel", "login", "duration", "platform", "url", "upload_date")}
        meta.update({"start": start or 0, "end": end})
        self._write_chat(base_path, meta, msgs, video_file, start or 0)

    def _write_chat(self, base_path, meta, msgs, video_file, offset):
        self.messages = msgs
        formats = self.opts.get("chat_formats")
        if formats is None:  # not specified (older clients); an empty list means "no chat files"
            formats = ["json", "html"]
        if self.opts.get("burn") and "json" not in formats:
            formats = formats + ["json"]  # keep chat data so the video can be re-rendered later
        if "json" in formats:
            chat.write_json(base_path + ".chat.json", meta, msgs)
            self.files.append(base_path + ".chat.json")
        if "txt" in formats:
            chat.write_txt(base_path + ".chat.txt", msgs)
            self.files.append(base_path + ".chat.txt")
        if "html" in formats:
            chat.write_html(base_path + ".chat.html", meta, msgs, video_file, offset=offset)
            self.files.append(base_path + ".chat.html")
        self.detail = f"{len(msgs):,} messages"

    def _burn(self, video_file, base_path, style=None):
        o = self.opts
        self.stage, self.progress, self.detail = "Rendering chat into video", 0.0, ""
        started = time.time()

        def prog(p):
            self.progress = p
            el = time.time() - started
            if p > 0.01:
                self.detail = f"ETA {chat.fmt_ts(el / p - el)}"

        out = unique_path(base_path + " (with chat).mp4")
        render.burn(video_file, self.messages, out, style or o.get("chat_style") or SETTINGS["chat_style"],
                    offset=self.chat_offset, login=self.info.get("login"), platform=self.info.get("platform"),
                    progress=prog, cancel=self.cancel, on_procs=lambda ps: setattr(self, "procs", ps),
                    encoder=o.get("encoder") or SETTINGS.get("bake_encoder"))
        self.procs = []
        self.files.insert(0, out)
        if not o.get("keep_original", False) and self.kind != "burn":
            # The combined video replaces the plain one (chat-replay HTML falls back to text mode).
            try:
                os.remove(video_file)
                self.files.remove(video_file)
            except (OSError, ValueError):
                pass
        self.detail = ""

    # -- re-render chat onto an existing download (new style)
    def _run_burn(self):
        src = self.opts["burn_from"]
        self.info = dict(src.get("info") or {})
        video = next(f for f in src["files"] if f.endswith(".mp4") and "(with chat)" not in f) \
            if any(f.endswith(".mp4") and "(with chat)" not in f for f in src["files"]) \
            else next(f for f in src["files"] if f.endswith(".mp4"))
        chat_json = next(f for f in src["files"] if f.endswith(".chat.json"))
        with open(chat_json, encoding="utf-8") as fh:
            data = json.load(fh)
        meta = data.get("video") or {}
        self.messages = data.get("comments") or []
        self.chat_offset = meta.get("start") or 0
        self.info.setdefault("login", meta.get("login"))
        if "(with chat)" in video:
            raise RuntimeError("The original video was replaced by the chat version. "
                               "Enable 'Keep original video' to re-render later.")
        base_path = os.path.splitext(video)[0]
        self._burn(video, base_path)

    # -- live recording
    def _run_live(self):
        o = self.opts
        platform, query = o.get("platform", "twitch"), o.get("channel") or self.url
        self.stage = "Checking stream"
        st = live.status(platform, query)
        self._set_live_info(st)
        last_check = time.time()
        while not st["live"]:
            if not o.get("wait"):
                raise RuntimeError(f"{st['channel']} is not live right now." + (f" ({st['note']})" if st.get("note") else ""))
            self.stage = "Waiting for stream to go live"
            interval = 30 if platform == "twitch" else 90
            nxt = last_check + interval
            while time.time() < nxt:
                if self.cancel.is_set() or self.stop_req.is_set():
                    self.cancel.set()
                    return
                self.detail = f"checking again in {int(nxt - time.time())}s"
                time.sleep(1)
            try:
                st = live.status(platform, query)
                self._set_live_info(st)
            except Exception as e:  # noqa: BLE001 - keep waiting through network hiccups
                self.detail = f"check failed: {e}"
            last_check = time.time()

        stamp = time.strftime("%Y-%m-%d %H.%M")
        # yt-dlp appends the date and time to live titles ("... 2026-11-20 17:09"); the stamp already has it.
        title = re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", st["title"])
        title = re.sub(r"[\[\]]", "", title).strip()[:100]
        base = sanitize(f"{stamp} {st['channel']} LIVE - {title} [{st.get('stream_id') or 'live'}]", 200)
        base_path = os.path.join(output_dir(st["channel"], self.opts.get("output_dir")), base)

        recorder = None
        if platform == "twitch" and o.get("chat", True):
            recorder = live.TwitchChatRecorder(st["login"])
            recorder.start()

        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--newline", "-f", self._format(),
               "-o", base_path + ".%(ext)s", "--print", "after_move:@@F %(filepath)s"]
        if platform == "youtube" and o.get("from_start"):
            cmd.append("--live-from-start")
        cmd.append(st["url"])
        self.stage = "Recording"
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True)
        tail, final = [], [None]

        def reader():
            for line in self.proc.stdout:
                line = line.rstrip()
                if line.startswith("@@F "):
                    final[0] = line[4:]
                else:
                    tail.append(line)
                    del tail[:-20]
        threading.Thread(target=reader, daemon=True).start()

        t0 = t_end = None
        prefix = glob.escape(base_path)
        while self.proc.poll() is None:
            if self.stop_req.is_set() or self.cancel.is_set():
                # yt-dlp stops writing as soon as it gets SIGINT; the .part file is remuxed below.
                t_end = time.time()
                if recorder:
                    recorder.stop()
                self.stage = "Stopping"
                signal_group(self.proc, signal.SIGINT)
                # yt-dlp now closes the file and runs its own fix-up remux, which takes minutes on a
                # multi-hour recording. Let it finish; only kill it if the files stop changing.
                last, still = None, time.time()
                while self.proc.poll() is None and time.time() - still < 60:
                    sizes = sorted((f, os.path.getsize(f)) for f in glob.glob(prefix + ".*") if os.path.exists(f))
                    if sizes != last:
                        last, still = sizes, time.time()
                    self.detail = "finishing file…  " + human_size(sum(s for _, s in sizes))
                    time.sleep(1)
                signal_group(self.proc, SIGKILL)  # also ends ffmpeg if yt-dlp left it running
                break
            size = sum(os.path.getsize(f) for f in glob.glob(prefix + ".*") if not f.endswith((".json", ".html", ".txt")))
            if size and t0 is None:
                t0 = time.time()
            parts = [f"● {chat.fmt_ts(time.time() - t0)}" if t0 else "starting…", human_size(size)]
            if recorder:
                parts.append(f"{len(recorder.messages):,} chat messages")
            self.detail = "  ·  ".join(parts)
            time.sleep(1)
        self.proc.wait()
        signal_group(self.proc, SIGKILL)
        t_end = t_end or time.time()
        if recorder:
            recorder.stop()

        self.stage, self.detail = "Saving recording", ""
        try:
            video = self._finalize_recording(base_path, final[0], tail)
        except Exception:
            if recorder and t0 and recorder.messages:
                # Never lose the chat: save it on the recording's clock (backlog unknown, so ~seconds early).
                msgs = [{**{k: v for k, v in m.items() if k != "wall"}, "t": round(m["wall"] - t0, 3)}
                        for m in recorder.messages if m["wall"] <= t_end]
                chat.write_json(base_path + ".chat.json", {"title": st["title"], "channel": st["channel"],
                                "login": st["login"], "platform": platform, "live": True, "recorded": stamp,
                                "start": 0, "end": None}, msgs)
                self.files.append(base_path + ".chat.json")
            raise
        self.files.append(video)
        if recorder and t0:
            # The file also holds the playlist's backlog from before t0; align chat to the video's clock.
            backlog = max(0.0, render.probe(video)["duration"] - (t_end - t0))
            msgs = [{**{k: v for k, v in m.items() if k != "wall"}, "t": round(m["wall"] - t0 + backlog, 3)}
                    for m in recorder.messages if m["wall"] <= t_end]
            meta = {"id": st.get("stream_id"), "title": st["title"], "channel": st["channel"], "login": st["login"],
                    "platform": platform, "url": st["url"], "live": True, "recorded": stamp, "start": 0, "end": None}
            self.chat_offset = 0
            self._write_chat(base_path, meta, msgs, video, 0)
            if o.get("burn") and msgs:
                self._burn(video, base_path)
        if self.cancel.is_set() and not self.stop_req.is_set():
            self.cancel.clear()  # a recording is always kept; treat cancel like stop

    def _run_convert(self):
        o = self.opts
        src, fmt = o["src"], o["convert"]
        if not os.path.isfile(src):
            raise RuntimeError(f"File not found: {src}")
        uploaded = os.path.abspath(src).startswith(os.path.abspath(UPLOADS) + os.sep)
        if o.get("dest") == "custom":
            dest = os.path.abspath(os.path.expanduser((o.get("folder") or "").strip()))
            if not (o.get("folder") or "").strip():
                raise RuntimeError("Choose a folder for the converted files")
        elif uploaded or o.get("dest") == "folder":
            dest = os.path.join(os.path.expanduser(SETTINGS["output_dir"]), "Converted")
        else:
            dest = os.path.dirname(os.path.abspath(src))
        self.stage, self.detail = "Converting", ""
        started = time.time()

        def prog(p):
            self.progress = p
            el = time.time() - started
            if p > 0.02:
                self.detail = f"ETA {chat.fmt_ts(el / p - el)}"

        out = convert.convert(src, fmt, dest, o, unique_path, progress=prog, cancel=self.cancel,
                              on_proc=lambda p: setattr(self, "proc", p))
        self.proc = None
        self.files.append(out)
        self.detail = human_size(os.path.getsize(out))
        if uploaded:  # the dropped copy is no longer needed
            shutil.rmtree(os.path.dirname(src), ignore_errors=True)

    def _set_live_info(self, st):
        self.info = {"title": st["title"] or st["channel"], "channel": st["channel"], "login": st.get("login"),
                     "platform": st["platform"], "thumbnail": st.get("thumbnail") or st.get("avatar"),
                     "is_live": st["live"], "id": st.get("stream_id"), "game": st.get("game")}

    def _finalize_recording(self, base_path, final, tail):
        prefix = glob.escape(base_path)
        # An interrupted yt-dlp fix-up leaves a partial "<name>.temp.mp4" behind; it is never the recording.
        for f in glob.glob(prefix + ".temp.*") + glob.glob(prefix + ".*.temp.*"):
            if f != final:
                os.remove(f)
        if final and os.path.exists(final):
            return self._ensure_mp4(final)
        done = [f for f in glob.glob(prefix + ".*") if f.endswith((".mp4", ".mkv", ".ts", ".webm"))]
        if done:
            return self._ensure_mp4(max(done, key=os.path.getsize))
        parts = sorted(f for f in glob.glob(prefix + "*.part") if os.path.getsize(f) > 0)
        if not parts:
            errs = [l for l in tail if "ERROR" in l] or tail[-4:]
            raise RuntimeError("Nothing was recorded.\n" + "\n".join(errs))
        out = base_path + ".mp4"
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for p in parts:
            cmd += ["-i", p]
        for i in range(len(parts)):
            cmd += ["-map", str(i)]
        cmd += ["-c", "copy", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            # Keep the raw stream rather than losing it.
            raw = base_path + ".ts"
            os.replace(parts[0], raw)
            return raw
        for p in parts:
            os.remove(p)
        return out

    def _ensure_mp4(self, path):
        """yt-dlp writes live HLS as raw MPEG-TS; if its fix-up didn't run, remux to a real MP4 here."""
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        if "mpegts" not in p.stdout:
            return path
        self.detail = "converting to MP4…"
        root, _ = os.path.splitext(path)
        tmp = root + ".remux.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy",
                            "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", tmp], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
            if path.endswith(".mp4"):  # keep the stream, but name it for what it is
                os.replace(path, root + ".ts")
                return root + ".ts"
            return path
        os.replace(tmp, root + ".mp4")
        if path != root + ".mp4":
            os.remove(path)
        self.detail = ""
        return root + ".mp4"

    # -- control
    def stop(self):
        """Live: stop recording and save. Others: cancel."""
        if self.kind == "live":
            self.stop_req.set()
        else:
            self.cancel_now()

    def cancel_now(self):
        if self.kind == "live" and self.status == "running" and self.stage in ("Recording", "Stopping"):
            return self.stop()  # never throw away a recording in progress
        self.cancel.set()
        signal_group(self.proc, signal.SIGTERM)
        for p in list(self.procs):
            if p and p.poll() is None:
                p.terminate()


def submit_conversions(body):
    fmt = body.get("format")
    if fmt not in convert.FORMATS:
        raise RuntimeError("Choose an output format")
    opts = {k: body.get(k) for k in ("quality", "depth", "sample_rate", "channels", "dest", "folder", "cover")}
    jobs = []
    for path in body.get("paths") or []:
        try:
            src = convert.probe(path)
            desc = convert.describe(src, fmt, opts)
            frm = src["codec"].split("_")[0].upper()
        except Exception:  # noqa: BLE001 - the job itself reports the problem
            desc, frm = convert.FORMATS[fmt]["label"], "?"
        job = Job(path, {**opts, "convert": fmt, "src": path, "what": f"{frm} → {desc}"})
        job.info = {"title": os.path.basename(path), "platform": "audio"}
        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(target=job.run, daemon=True).start()
        jobs.append(job.public())
    return jobs


def submit(url, opts):
    job = Job(url, opts)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _local_only(self):
        # Block requests from other websites (DNS rebinding / CSRF to localhost).
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        ok = origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")
        if ok:
            global LAST_SEEN
            LAST_SEEN = time.time()  # the Windows launcher quits once the UI window is gone
        return ok

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path in ("/icon.png", "/favicon.ico"):  # window / taskbar icon (made by windows/make_ico.sh)
            ico = path == "/favicon.ico"
            with open(os.path.join(STATIC, "favicon.ico" if ico else "icon.png"), "rb") as fh:
                return self._send(200, fh.read(), "image/x-icon" if ico else "image/png")
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted((j.public() for j in JOBS.values()), key=lambda j: -j["created"])
            return self._send(200, jobs)
        if path == "/api/settings":
            return self._send(200, {**SETTINGS, "platform": PLATFORM, "version": APP_VERSION, "gpu": GPU.get("name"),
                                    "gpu_checked": "name" in GPU})
        if path == "/api/update/progress":
            return self._send(200, updater.STATE)
        if path == "/api/update":
            return self._send(200, update_status(force="force=1" in urlparse(self.path).query))
        if path == "/api/audio/formats":
            return self._send(200, {"formats": convert.formats(), "inputs": sorted(convert.INPUT_EXTS)})
        if path == "/api/fonts":
            try:
                return self._send(200, render.list_fonts())
            except Exception:  # noqa: BLE001 - renderer missing: offer the default only
                return self._send(200, ["System"])
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        """Streams a file dropped onto the converter into a private folder; returns its path."""
        if not self._local_only() or urlparse(self.path).path != "/api/upload":
            return self._send(403, {"error": "forbidden"})
        from urllib.parse import parse_qs
        name = sanitize(os.path.basename((parse_qs(urlparse(self.path).query).get("name") or ["audio"])[0]), 150)
        folder = os.path.join(UPLOADS, uuid.uuid4().hex[:12])
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, name)
        left = int(self.headers.get("Content-Length") or 0)
        try:
            with open(dest, "wb") as fh:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    fh.write(chunk)
                    left -= len(chunk)
        except OSError as e:
            shutil.rmtree(folder, ignore_errors=True)
            return self._send(500, {"error": str(e)})
        if left:
            shutil.rmtree(folder, ignore_errors=True)
            return self._send(400, {"error": "upload interrupted"})
        return self._send(200, {"path": dest})

    def do_POST(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/info":
                return self._send(200, get_info(body["url"].strip()))
            if path == "/api/audio/probe":
                out = []
                for p in body.get("paths") or []:
                    try:
                        out.append(convert.probe(p))
                    except Exception as e:  # noqa: BLE001
                        out.append({"path": p, "name": os.path.basename(p), "error": str(e)})
                return self._send(200, out)
            if path == "/api/convert":
                return self._send(200, submit_conversions(body))
            if path == "/api/pickfiles":
                return self._send(200, {"paths": compat.pick_files()})
            if path == "/api/pickfolder":
                return self._send(200, {"path": compat.pick_folder(body.get("current") or SETTINGS["output_dir"])})
            if path == "/api/channel":
                return self._send(200, browse_channel(body["platform"], body["query"], body.get("tab", "videos")))
            if path == "/api/jobs":
                urls = body["urls"] if "urls" in body else [body["url"]]
                return self._send(200, [submit(u.strip(), body.get("opts", {})).public() for u in urls if u.strip()])
            m = re.match(r"^/api/jobs/(\w+)/(cancel|stop|remove|retry|burn)$", path)
            if m:
                job = JOBS.get(m.group(1))
                if not job:
                    return self._send(404, {"error": "no such job"})
                act = m.group(2)
                if act == "cancel":
                    job.cancel_now()
                elif act == "stop":
                    job.stop()
                elif act == "remove":
                    job.cancel_now()
                    with JOBS_LOCK:
                        JOBS.pop(job.id, None)
                elif act == "burn":
                    src = job.public()
                    src.pop("opts", None)
                    opts = {"burn_from": src, "chat_style": render.clean_style(body.get("style") or SETTINGS["chat_style"])}
                    return self._send(200, submit(job.url, opts).public())
                else:
                    new = submit(job.url, job.opts)
                    if job.kind == "convert":
                        new.info = dict(job.info)
                    return self._send(200, new.public())
                return self._send(200, {"ok": True})
            if path == "/api/live/status":
                return self._send(200, live.status(body.get("platform", "twitch"), body["query"]))
            if path == "/api/frame":
                jpg = full_frame(body["url"], float(body["t"]), bool(body.get("fresh")))
                return self._send(200, {"jpg": base64.b64encode(jpg).decode()})
            if path == "/api/chat/preview":
                style = render.clean_style(body.get("style"))
                msgs, at, login, plat = preview_sample(body.get("url"), body.get("at"))
                res = render.preview(style, msgs, at, login=login, platform=plat)
                res["sample"] = "vod" if body.get("url") and login else "demo"
                return self._send(200, res)
            if path == "/api/chat/activity":
                info = get_info(body["url"].strip())
                vod = twitch_vod_id({"extractor_key": info["kind"], "id": info["id"]}, info.get("url") or body["url"])
                if not vod or not info.get("duration"):
                    return self._send(200, {"per_minute": []})
                if vod not in _ACTIVITY:
                    _ACTIVITY[vod] = chat.twitch_activity(vod, info["duration"])
                return self._send(200, _ACTIVITY[vod])
            if path == "/api/disk":
                try:
                    d = os.path.expanduser(SETTINGS["output_dir"])  # a "~/..." path must not become ./~
                    os.makedirs(d, exist_ok=True)
                    u = shutil.disk_usage(d)
                    return self._send(200, {"path": d, "free": u.free, "total": u.total})
                except OSError as e:
                    return self._send(200, {"path": SETTINGS["output_dir"], "error": str(e)})
            if path == "/api/update/install":
                st = update_status(force=True)
                asset = _UPDATE.get("asset")
                if not (st.get("newer") and asset):
                    return self._send(400, {"error": "No update to install."})
                return self._send(200, updater.start(asset, lambda: any(
                    j.status in ("running", "queued") for j in list(JOBS.values()))))
            if path == "/api/jobs/clear":
                with JOBS_LOCK:
                    for k in [k for k, j in JOBS.items() if j.status in ("done", "error", "cancelled")]:
                        del JOBS[k]
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                for k in DEFAULT_SETTINGS:
                    if k in body:
                        SETTINGS[k] = (render.clean_style(body[k]) if k == "chat_style"
                                       else render.clean_presets(body[k]) if k == "chat_presets"
                                       else type(DEFAULT_SETTINGS[k])(body[k]))
                SETTINGS["bake_encoder"] = render.encoder_mode(SETTINGS.get("bake_encoder"))
                if "output_dir" in body:  # store "~/x" as a full path, and never an empty one
                    SETTINGS["output_dir"] = os.path.expanduser(SETTINGS["output_dir"].strip()) or DEFAULT_SETTINGS["output_dir"]
                save_settings()
                return self._send(200, {**SETTINGS, "platform": PLATFORM})
            if path in ("/api/reveal", "/api/open") and body.get("path"):
                with JOBS_LOCK:
                    known = {f for j in JOBS.values() for f in j.files}
                if body["path"] not in known:
                    return self._send(403, {"error": "unknown file"})
            if path == "/api/reveal":
                target = body.get("path") or os.path.expanduser(SETTINGS["output_dir"])
                if not body.get("path"):
                    os.makedirs(target, exist_ok=True)
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", target] if body.get("path") else ["open", target])
                elif os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", target] if body.get("path") else ["explorer", target])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(target) if body.get("path") else target])
                return self._send(200, {"ok": True})
            if path == "/api/open":
                target = body["path"]
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                if os.name == "nt":
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([opener, target])
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})


def check_tools():
    missing = [t for t in ("yt-dlp", "ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        hint = "They ship inside the Windows app." if compat.WINDOWS else "Install them first: brew install yt-dlp ffmpeg"
        raise RuntimeError(f"Missing {', '.join(missing)} on PATH. {hint}")


def make_server(port=0):
    shutil.rmtree(UPLOADS, ignore_errors=True)  # leftovers from a previous session
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def shutdown_jobs(timeout=12):
    """Save in-progress recordings (stop + remux), cancel everything else."""
    jobs = list(JOBS.values())
    for j in jobs:
        j.stop() if j.kind == "live" else j.cancel_now()
    deadline = time.time() + timeout
    while time.time() < deadline and any(j.kind == "live" and j.status == "running"
                                         and j.stage in ("Recording", "Stopping", "Saving recording")
                                         for j in jobs):
        time.sleep(0.2)


def active_jobs():
    return [j for j in JOBS.values() if j.status in ("queued", "running")]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        check_tools()
    except RuntimeError as e:
        sys.exit(str(e))
    srv = make_server(args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    # The macOS app shell reads this line to learn the port (use --port 0 for a free one).
    print(f"StreamGrab running at {url}  (Ctrl+C to quit)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)  # app shell quits us with SIGTERM
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        shutdown_jobs()


if __name__ == "__main__":
    main()
