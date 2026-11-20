"""Smoke test for a packaged StreamGrab build, run by .github/workflows/release.yml.

Starts the engine from the package (its own Python, yt-dlp, deno and ffmpeg), then checks the main
features over the local API: settings, video info, chat activity, chat preview (the chat renderer),
an audio conversion, and a short Twitch download with chat baked in.

  python tests/smoke.py --python <package python> --app <package app folder> --bin <package bin folder>

YouTube often blocks cloud machines ("Sign in to confirm you're not a bot"), so YouTube checks only warn.
Standard library only.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, wave

# Windows consoles default to a legacy code page; error texts from YouTube contain curly quotes.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser()
ap.add_argument("--python", required=True)
ap.add_argument("--app", required=True)
ap.add_argument("--bin", required=True)
ap.add_argument("--port", type=int, default=8799)
args = ap.parse_args()
# absolute paths: the engine runs with its app folder as the working directory
args.python, args.app, args.bin = (os.path.abspath(x) for x in (args.python, args.app, args.bin))

BASE = f"http://127.0.0.1:{args.port}"
TWITCH_VOD = None  # picked at run time: Twitch deletes old VODs, so a fixed link would stop working
TWITCH_CHANNELS = ["zackrawrr", "kaicenat", "xqc", "jynxzi", "summit1g", "shroud"]
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
home = tempfile.mkdtemp(prefix="sg-smoke-")
out = os.path.join(home, "out")
os.makedirs(out)

env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONUTF8="1",
           PATH=os.path.abspath(args.bin) + os.pathsep + os.environ.get("PATH", ""))
log = open(os.path.join(home, "engine.log"), "w")
engine = subprocess.Popen([args.python, os.path.join(args.app, "server.py"), "--port", str(args.port), "--no-browser"],
                          env=env, cwd=args.app, stdout=log, stderr=subprocess.STDOUT)
failures, warnings = [], []


def call(path, body=None, timeout=300):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Origin": BASE, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def check(name, fn, optional=False):
    t = time.time()
    try:
        info = fn()
        print(f"PASS  {name:34} {time.time() - t:5.1f}s  {info}", flush=True)
    except Exception as e:  # noqa: BLE001
        (warnings if optional else failures).append(name)
        print(f"{'WARN' if optional else 'FAIL'}  {name:34} {time.time() - t:5.1f}s  {str(e)[:300]}", flush=True)


def wait_jobs(ids, limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        jobs = {j["id"]: j for j in call("/api/jobs")}
        if all(jobs[i]["status"] not in ("queued", "running") for i in ids):
            return jobs
        time.sleep(2)
    raise RuntimeError("timed out")


def job(url, opts):
    i = call("/api/jobs", {"urls": [url], "opts": dict(opts, output_dir=out)})[0]["id"]
    j = wait_jobs([i])[i]
    if j["status"] != "done":
        raise RuntimeError(f"{j['status']}: {j.get('error')}")
    files = [f for f in j["files"] if os.path.exists(f)]
    if not files:
        raise RuntimeError("no files")
    return [os.path.basename(f)[-40:] for f in files]


try:
    for _ in range(60):
        try:
            call("/api/settings", timeout=2)
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)

    def settings():
        s = call("/api/settings")
        assert s["version"] and s["version"] != "dev", s["version"]
        return f"version {s['version']} on {s['platform']}"
    check("engine starts", settings)

    def tools():
        found = {t: shutil.which(t, path=env["PATH"]) for t in ("yt-dlp", "ffmpeg", "ffprobe", "deno")}
        missing = [t for t, p in found.items() if not p or not os.path.abspath(p).startswith(os.path.abspath(args.bin))]
        assert not missing, f"not bundled: {missing}"
        v = subprocess.run([found["yt-dlp"], "--version"], capture_output=True, text=True, env=env, timeout=60).stdout.strip()
        return f"bundled yt-dlp {v}, deno, ffmpeg"
    check("bundled tools", tools)

    def pick_vod():
        global TWITCH_VOD
        for ch in TWITCH_CHANNELS:
            try:
                items = call("/api/channel", {"platform": "twitch", "query": ch, "tab": "archive"})["items"]
            except Exception:  # noqa: BLE001
                continue
            long = [v for v in items if (v.get("duration") or 0) > 2 * 3600]
            if long:
                TWITCH_VOD = long[0]["url"]
                return f"{ch}: {TWITCH_VOD}"
        raise RuntimeError("no recent Twitch VOD found")
    check("find a current Twitch VOD", pick_vod)

    def info_twitch():
        i = call("/api/info", {"url": TWITCH_VOD})
        assert i["title"] and i["qualities"] and i["chat"], i
        return i["title"][:40]
    check("info: Twitch VOD", info_twitch)
    check("info: YouTube", lambda: call("/api/info", {"url": YOUTUBE})["title"], optional=True)

    def activity():
        pm = call("/api/chat/activity", {"url": TWITCH_VOD})["per_minute"]
        assert len(pm) > 10 and max(pm) > 0
        return f"{len(pm)} slots"
    check("chat activity", activity)

    def preview():
        r = call("/api/chat/preview", {"style": {"animate": True, "animate_twitch": True}, "url": TWITCH_VOD, "at": 3600})
        assert r["png"] and r["sample"] == "vod", r.get("sample")
        return f"{1 + len(r.get('frames') or [])} frame(s)"
    check("chat preview (renderer)", preview)

    def conversion():
        src = os.path.join(out, "tone.wav")
        with wave.open(src, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"\x00\x10" * 2 * 44100)
        res = []
        for fmt in ("mp3", "flac", "ogg"):
            i = call("/api/convert", {"paths": [src], "format": fmt, "dest": "same"})[0]["id"]
            j = wait_jobs([i])[i]
            assert j["status"] == "done", (fmt, j.get("error"))
            res.append(fmt)
        return ", ".join(res)
    check("audio conversion", conversion)

    check("Twitch section + chat + bake-in", lambda: job(TWITCH_VOD, {
        "video": True, "chat": True, "chat_formats": ["json", "txt", "html"], "start": "1:00:00", "end": "1:00:15",
        "height": 720, "burn": True, "keep_original": True}))
    check("YouTube MP3", lambda: job(YOUTUBE, {"video": True, "chat": False, "audio_only": True, "audio_format": "mp3"}), optional=True)
    check("YouTube trimmed video", lambda: job(YOUTUBE, {"video": True, "chat": False, "start": "0:03", "end": "0:08"}), optional=True)
finally:
    engine.terminate()
    try:
        engine.wait(15)
    except subprocess.TimeoutExpired:
        engine.kill()
    log.close()

if failures:
    print("\nEngine log (last 60 lines):")
    print("".join(open(os.path.join(home, "engine.log"), encoding="utf-8", errors="replace").readlines()[-60:]))
print(f"\n{len(failures)} failed, {len(warnings)} warnings (YouTube checks only warn)")
sys.exit(1 if failures else 0)
