"""Parallel HLS segment downloader for trimmed Twitch VODs.

yt-dlp hands trimmed sections to ffmpeg, which fetches segments one at a time and
reports no progress. Here only the segments covering [start, end] are fetched on
several connections at once, joined, and trimmed with a stream copy (no re-encode).
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import chat

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


class Unsupported(Exception):
    """This video/format can't use the fast path; fall back to yt-dlp."""


def parse_playlist(text):
    segs, dur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP") or line.startswith("#EXT-X-KEY"):
            raise Unsupported("playlist uses init segments or encryption")
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#"):
            segs.append((dur or 0.0, line))
            dur = None
    return segs


def _fetch(url, path, cancel, retries=5):
    candidates = [url]
    if url.endswith(".ts") and "-muted" not in url:
        candidates.append(url[:-3] + "-muted.ts")  # Twitch renames segments it mutes for copyright
    last = None
    for attempt in range(retries):
        for u in candidates:
            if cancel and cancel.is_set():
                raise chat.Cancelled()
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r, open(path + ".tmp", "wb") as fh:
                    shutil.copyfileobj(r, fh, 1 << 20)
                os.replace(path + ".tmp", path)
                return os.path.getsize(path)
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (403, 404):
                    break  # retry same candidate after backoff
            except OSError as e:
                last = e
                break
        time.sleep(min(8, 0.5 * 2 ** attempt))
    raise RuntimeError(f"Segment download failed: {last}")


def keyframe_at_or_before(path, t):
    """Time (seconds from the start of `path`) of the last video keyframe at or before t.
    A cut without re-encoding has to begin on a keyframe: begun anywhere else, the video track only
    starts at the next keyframe (up to a few seconds later), so players freeze the first frame and the
    sound runs ahead of the picture."""
    def probe(args):
        p = subprocess.run(["ffprobe", "-v", "error", *args, path], capture_output=True, text=True, timeout=120)
        return [float(x.split(",")[0]) for x in p.stdout.split() if x.split(",")[0].replace(".", "", 1).isdigit()]
    first = probe(["-show_entries", "packet=pts_time", "-of", "csv=p=0", "-read_intervals", "%+#1"])
    base = min(first) if first else 0.0
    keys = probe(["-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
                  "-of", "csv=p=0", "-read_intervals", f"{max(0.0, base + t - 30):.3f}%{base + t + 0.05:.3f}"])
    before = [k - base for k in keys if k - base <= t + 0.001]
    return max(before) if before else 0.0


def download_section(url, fmt, out_path, start, end, workers=12, progress=None, cancel=None):
    """Downloads [start, end] seconds of an HLS VOD to out_path (.mp4).
    Returns (out_path, actual_start): the clip begins on the keyframe at or just before `start`."""
    p = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--no-playlist", "-f", fmt, url],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise Unsupported("format lookup failed")
    if cancel and cancel.is_set():
        raise chat.Cancelled()
    info = json.loads(p.stdout)
    if info.get("requested_formats") or not str(info.get("protocol", "")).startswith("m3u8"):
        raise Unsupported("not a single HLS stream")
    m3u8 = info["url"]
    req = urllib.request.Request(m3u8, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        segs = parse_playlist(r.read().decode("utf-8", "replace"))
    if not segs:
        raise Unsupported("empty playlist")

    total = sum(d for d, _ in segs)
    start = max(0.0, start or 0.0)
    end = total if end is None else min(end, total)
    picked, t, first_start = [], 0.0, None
    for d, uri in segs:
        if t + d > start and t < end:
            if first_start is None:
                first_start = t
            picked.append(uri)
        t += d
    if not picked:
        raise RuntimeError("Trim range is outside the video")

    base = m3u8.rsplit("/", 1)[0] + "/"
    work = out_path + ".segments"
    os.makedirs(work, exist_ok=True)
    done_bytes, done_count, lock = [0], [0], threading.Lock()
    t0 = time.time()

    def job(i, uri):
        path = os.path.join(work, f"{i:06d}.ts")
        n = os.path.getsize(path) if os.path.exists(path) else _fetch(uri if uri.startswith("http") else base + uri, path, cancel)
        with lock:
            done_bytes[0] += n
            done_count[0] += 1
            if progress:
                frac = done_count[0] / len(picked)
                speed = done_bytes[0] / max(0.1, time.time() - t0)
                eta = (time.time() - t0) / frac * (1 - frac) if frac else 0
                progress(frac, speed, eta, done_bytes[0])

    try:
        with ThreadPoolExecutor(workers) as ex:
            for f in [ex.submit(job, i, u) for i, u in enumerate(picked)]:
                f.result()
        # MPEG-TS segments can be joined byte for byte; ffmpeg then trims and remuxes to MP4.
        joined = os.path.join(work, "joined.ts")
        with open(joined, "wb") as out:
            for i in range(len(picked)):
                seg = os.path.join(work, f"{i:06d}.ts")
                with open(seg, "rb") as fh:
                    shutil.copyfileobj(fh, out, 1 << 22)
                os.remove(seg)
        cut = keyframe_at_or_before(joined, start - first_start)
        actual_start = first_start + cut
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{cut:.3f}", "-i", joined,
               "-t", f"{end - actual_start:.3f}", "-map", "0", "-c", "copy", "-bsf:a", "aac_adtstoasc",
               "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Joining segments failed:\n" + r.stderr[-600:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_path, actual_start
