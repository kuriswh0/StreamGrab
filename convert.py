"""Audio converter: any audio (or video) file ffmpeg can read -> MP3, AAC, ALAC, FLAC, WAV, AIFF, OGG,
Opus or WMA. Keeps tags, and cover art where the target format supports it.
"""
import json
import os
import subprocess
import threading

import chat

# Target formats. `qualities` are (value, label); the first lossy entry marked default is preselected.
FORMATS = {
    "mp3": {"label": "MP3", "ext": ".mp3", "lossless": False, "cover": True, "default": "320",
            "qualities": [["320", "320 kbps"], ["v0", "VBR V0 (~245 kbps)"], ["256", "256 kbps"],
                          ["192", "192 kbps"], ["128", "128 kbps"]]},
    "aac": {"label": "AAC (.m4a)", "ext": ".m4a", "lossless": False, "cover": True, "default": "256",
            "qualities": [["320", "320 kbps"], ["256", "256 kbps"], ["192", "192 kbps"], ["128", "128 kbps"]]},
    "ogg": {"label": "OGG Vorbis", "ext": ".ogg", "lossless": False, "cover": False, "default": "q8",
            "qualities": [["q10", "Quality 10 (~500 kbps)"], ["q8", "Quality 8 (~256 kbps)"],
                          ["q6", "Quality 6 (~192 kbps)"], ["q4", "Quality 4 (~128 kbps)"]]},
    "opus": {"label": "Opus", "ext": ".opus", "lossless": False, "cover": False, "default": "160",
             "qualities": [["256", "256 kbps"], ["192", "192 kbps"], ["160", "160 kbps"], ["128", "128 kbps"],
                           ["96", "96 kbps"], ["64", "64 kbps"]]},
    "wma": {"label": "WMA", "ext": ".wma", "lossless": False, "cover": False, "default": "192",
            "qualities": [["192", "192 kbps"], ["160", "160 kbps"], ["128", "128 kbps"]]},
    "flac": {"label": "FLAC", "ext": ".flac", "lossless": True, "cover": True},
    "alac": {"label": "ALAC (Apple Lossless .m4a)", "ext": ".m4a", "lossless": True, "cover": True},
    "wav": {"label": "WAV", "ext": ".wav", "lossless": True, "cover": False, "float": True},
    "aiff": {"label": "AIFF", "ext": ".aiff", "lossless": True, "cover": False},
}
MAX_48K = {"mp3", "aac", "wma", "ogg"}
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "ape", "tta", "mlp", "truehd", "shorten"}
INPUT_EXTS = {".mp3", ".flac", ".wav", ".wave", ".aif", ".aiff", ".aifc", ".ogg", ".oga", ".opus", ".m4a",
              ".m4b", ".aac", ".alac", ".wma", ".ape", ".wv", ".mka", ".ac3", ".eac3", ".dts", ".amr", ".caf",
              ".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".flv"}

_ENC = []


def encoders():
    if not _ENC:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        _ENC.append({line.split()[1] for line in out.splitlines() if len(line.split()) > 1})
    return _ENC[0]


def formats():
    """Format table for the UI."""
    return {k: {kk: v[kk] for kk in ("label", "ext", "lossless", "qualities", "default") if kk in v}
            for k, v in FORMATS.items()}


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration,bit_rate,format_name:stream=index,codec_type,codec_name,sample_rate,"
                        "channels,bits_per_raw_sample,bits_per_sample,sample_fmt,bit_rate:stream_disposition=attached_pic",
                        "-of", "json", path], capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    streams = d.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio:
        raise RuntimeError(f"No audio found in {os.path.basename(path)}")
    covers = [s["index"] for s in streams if s.get("codec_type") == "video"
              and (s.get("disposition") or {}).get("attached_pic") and s.get("codec_name") in ("mjpeg", "png")]
    fmt = d.get("format") or {}
    codec = audio.get("codec_name", "")
    bits = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0)
    if not bits and codec.startswith("pcm_"):
        bits = int("".join(c for c in codec.split("_")[1] if c.isdigit()) or 0)
    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    return {
        "path": path, "name": os.path.basename(path), "size": os.path.getsize(path),
        "duration": float(fmt.get("duration") or 0), "codec": codec,
        "sample_rate": int(audio.get("sample_rate") or 0), "channels": int(audio.get("channels") or 0),
        "bits": bits if lossless else 0, "float": "flt" in (audio.get("sample_fmt") or "") or codec.startswith("pcm_f"),
        "bitrate": int(audio.get("bit_rate") or fmt.get("bit_rate") or 0), "lossless": lossless,
        "audio_index": audio["index"], "cover_index": covers[0] if covers else None,
    }


def _depth(src, depth):
    """Output bit depth for lossless targets: 'auto' keeps hi-res sources at 24-bit, everything else 16."""
    if depth in ("16", "24", "32f"):
        return depth
    return "24" if src["lossless"] and src["bits"] > 16 else "16"


def build_cmd(src, out, fmt, opts):
    f = FORMATS[fmt]
    q = str(opts.get("quality") or f.get("default") or "")
    enc = encoders()
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", src["path"], "-map", f"0:{src['audio_index']}"]
    cover = f["cover"] and src["cover_index"] is not None and opts.get("cover", True)
    if cover:
        cmd += ["-map", f"0:{src['cover_index']}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    cmd += ["-map_metadata", "0", "-map_chapters", "-1"]
    depth = _depth(src, opts.get("depth", "auto"))

    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame"] + (["-q:a", "0"] if q == "v0" else ["-b:a", f"{q}k"])
        cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
    elif fmt == "aac":
        codec = "aac_at" if "aac_at" in enc else "aac"  # Apple's encoder on macOS sounds better
        cmd += ["-c:a", codec, "-b:a", f"{q}k", "-movflags", "+faststart"]
    elif fmt == "ogg":
        level = q.lstrip("q") or "8"
        if "libvorbis" in enc:
            cmd += ["-c:a", "libvorbis", "-q:a", level]
        else:  # some ffmpeg builds only have the built-in encoder
            kbps = {"10": 500, "8": 256, "6": 192, "4": 128}.get(level, 256)
            cmd += ["-c:a", "vorbis", "-strict", "-2", "-b:a", f"{kbps}k", "-ac", "2"]
    elif fmt == "opus":
        cmd += ["-c:a", "libopus" if "libopus" in enc else "opus", "-b:a", f"{q}k", "-vbr", "on"]
        if "libopus" not in enc:
            cmd += ["-strict", "-2"]
    elif fmt == "wma":
        cmd += ["-c:a", "wmav2", "-b:a", f"{q}k"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac", "-compression_level", "8"]
        cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"] if depth == "24" else ["-sample_fmt", "s16"]
    elif fmt == "alac":
        cmd += ["-c:a", "alac", "-movflags", "+faststart"]
        cmd += ["-sample_fmt", "s32p", "-bits_per_raw_sample", "24"] if depth == "24" else ["-sample_fmt", "s16p"]
    elif fmt == "wav":
        cmd += ["-c:a", {"16": "pcm_s16le", "24": "pcm_s24le", "32f": "pcm_f32le"}[depth]]
        if src["duration"] > 6 * 3600 or src["size"] > 3_500_000_000:
            cmd += ["-rf64", "auto"]  # plain WAV stops at 4 GB
    elif fmt == "aiff":
        cmd += ["-c:a", "pcm_s24be" if depth in ("24", "32f") else "pcm_s16be", "-write_id3v2", "1"]

    rate = str(opts.get("sample_rate") or "")
    want = int(rate) if rate.isdigit() else src["sample_rate"]
    if fmt in MAX_48K and want > 48000:  # these codecs top out at 48 kHz
        rate = "44100" if want % 44100 == 0 else "48000"
    if rate.isdigit():
        if fmt == "opus" and rate not in ("48000", "24000", "16000", "12000", "8000"):
            rate = "48000"  # Opus always runs at 48 kHz
        cmd += ["-ar", rate]
    if str(opts.get("channels") or "") in ("1", "2"):
        cmd += ["-ac", str(opts["channels"])]
    # Dither when reducing bit depth (lossless/float -> 16-bit), so quiet passages don't get grainy.
    if f["lossless"] and depth == "16" and (src["bits"] > 16 or src["float"]):
        cmd += ["-af", "aresample=dither_method=triangular"]
    cmd += ["-progress", "pipe:1", out]
    return cmd


def output_path(src_path, fmt, dest_dir, unique):
    base = os.path.splitext(os.path.basename(src_path))[0]
    ext = FORMATS[fmt]["ext"]
    out = os.path.join(dest_dir, base + ext)
    if os.path.abspath(out) == os.path.abspath(src_path):  # e.g. AAC .m4a -> ALAC .m4a in the same folder
        out = os.path.join(dest_dir, f"{base} ({FORMATS[fmt]['label'].split()[0]}){ext}")
    return unique(out)


def convert(src_path, fmt, dest_dir, opts, unique, progress=None, cancel=None, on_proc=None):
    """Converts one file; returns the output path."""
    if fmt not in FORMATS:
        raise RuntimeError(f"Unknown format: {fmt}")
    src = probe(src_path)
    os.makedirs(dest_dir, exist_ok=True)
    out = output_path(src_path, fmt, dest_dir, unique)
    cmd = build_cmd(src, out, fmt, opts)
    err = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if on_proc:
        on_proc(proc)
    t = threading.Thread(target=lambda: err.extend(proc.stderr.read().splitlines()[-8:]), daemon=True)
    t.start()
    for line in proc.stdout:
        if cancel and cancel.is_set():
            proc.kill()
            break
        if line.startswith("out_time_us=") and src["duration"] and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / src["duration"]))
            except ValueError:
                pass
    proc.wait()
    t.join(5)
    if cancel and cancel.is_set():
        if os.path.exists(out):
            os.remove(out)
        raise chat.Cancelled()
    if proc.returncode != 0 or not os.path.exists(out):
        if os.path.exists(out):
            os.remove(out)
        raise RuntimeError("Conversion failed:\n" + "\n".join(l for l in err if l.strip())[-600:])
    return out


def describe(src, fmt, opts):
    f = FORMATS[fmt]
    q = str(opts.get("quality") or f.get("default") or "")
    label = dict(f.get("qualities") or []).get(q, "")
    if f["lossless"]:
        d = _depth(src, opts.get("depth", "auto")) if src else opts.get("depth", "auto")
        label = {"16": "16-bit", "24": "24-bit", "32f": "32-bit float"}.get(d, "")
    return f"{f['label'].split(' (')[0]}" + (f" · {label}" if label else "")
