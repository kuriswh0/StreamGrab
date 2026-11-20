"""In-app updates: download the new release from GitHub, check it, install it and reopen StreamGrab.

Mac (StreamGrab.app):    the DMG is mounted, the app inside is copied next to the current one and its
                         signature checked; a helper script waits for StreamGrab to quit, swaps the two
                         (putting the old one back if the swap fails) and reopens it.
Windows (installed):     a helper waits for StreamGrab to exit, runs the Setup .exe silently into the
                         same folder, then reopens it.
Portable Windows copies and runs from source can't update themselves; they get the download link.

Every download must match the SHA-256 digest GitHub publishes for the release asset.
"""
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAC = sys.platform == "darwin"
WINDOWS = os.name == "nt"

STATE = {"stage": "idle", "progress": 0.0, "error": ""}
QUIT_REQUESTED = threading.Event()  # set when the app should close so the update can be installed
_LOCK = threading.Lock()


def install_kind():
    """("mac", app_path) / ("windows", install_root) when this copy can update itself, else (None, reason)."""
    if MAC:
        # .../StreamGrab.app/Contents/Resources/app/updater.py
        app = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
        if not app.endswith(".app") or not os.path.isdir(os.path.join(app, "Contents", "MacOS")):
            return None, "running from source"
        if not os.access(os.path.dirname(app), os.W_OK):
            return None, f"no permission to replace {app}"
        return "mac", app
    if WINDOWS:
        root = os.path.dirname(HERE)  # ...\StreamGrab\app\updater.py
        if not os.path.exists(os.path.join(root, "Uninstall.exe")):
            return None, "portable copy"
        return "windows", root
    return None, "unsupported platform"


def pick_asset(assets):
    """The release file this install needs, as {name, url, digest, size}, or None."""
    kind, _ = install_kind()
    for a in assets or []:
        name = a.get("name") or ""
        if (kind == "mac" and name.endswith(".dmg")) or (kind == "windows" and name.startswith("StreamGrab-Setup-") and name.endswith(".exe")):
            digest = a.get("digest") or ""
            if digest.startswith("sha256:") and (a.get("browser_download_url") or "").startswith("https://github.com/"):
                return {"name": name, "url": a["browser_download_url"], "digest": digest[7:], "size": a.get("size") or 0}
    return None


def _set(stage, progress=None, error=""):
    STATE["stage"] = stage
    if progress is not None:
        STATE["progress"] = progress
    STATE["error"] = error


def start(asset, busy):
    """Starts downloading + installing in the background. busy(): True while jobs are running."""
    with _LOCK:
        if STATE["stage"] in ("downloading", "verifying", "installing", "restarting"):
            return STATE
        if busy():
            raise RuntimeError("Finish or stop the running downloads first.")
        kind, where = install_kind()
        if not kind:
            raise RuntimeError(f"This copy can't update itself ({where}).")
        _set("downloading", 0.0)
        threading.Thread(target=_run, args=(asset, kind, where), daemon=True).start()
        return STATE


def _run(asset, kind, where):
    try:
        tmp = tempfile.mkdtemp(prefix="streamgrab-update-")
        path = os.path.join(tmp, asset["name"])
        _download(asset["url"], path, asset.get("size") or 0)
        _set("verifying", 1.0)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != asset["digest"]:
            raise RuntimeError("The download doesn't match the release's checksum; nothing was changed.")
        _set("installing")
        (_apply_mac if kind == "mac" else _apply_windows)(path, where, tmp)
        _set("restarting")
        QUIT_REQUESTED.set()
    except Exception as e:  # noqa: BLE001 - shown in the app; the current version stays as it is
        _set("error", error=str(e)[:300])


def _download(url, path, size):
    req = urllib.request.Request(url, headers={"User-Agent": "StreamGrab"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as out:
        total = int(r.headers.get("Content-Length") or size or 0)
        done = 0
        while True:
            block = r.read(1 << 18)
            if not block:
                break
            out.write(block)
            done += len(block)
            if total:
                STATE["progress"] = min(1.0, done / total)


def _apply_mac(dmg, app, tmp):
    mnt = os.path.join(tmp, "mnt")
    os.makedirs(mnt)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg],
                   check=True, capture_output=True, timeout=120)
    staged = os.path.join(os.path.dirname(app), ".StreamGrab-update.app")
    try:
        src = os.path.join(mnt, "StreamGrab.app")
        with open(os.path.join(src, "Contents", "Info.plist"), "rb") as fh:
            if plistlib.load(fh).get("CFBundleIdentifier") != "local.streamgrab.app":
                raise RuntimeError("The download isn't a StreamGrab app.")
        shutil.rmtree(staged, ignore_errors=True)
        subprocess.run(["ditto", src, staged], check=True, capture_output=True, timeout=600)
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True, timeout=60)
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", staged], capture_output=True)
    if subprocess.run(["codesign", "--verify", "--deep", "--strict", staged], capture_output=True).returncode != 0:
        shutil.rmtree(staged, ignore_errors=True)
        raise RuntimeError("The new app's signature didn't verify; nothing was changed.")
    old = os.path.join(os.path.dirname(app), ".StreamGrab-old.app")
    script = os.path.join(tmp, "swap.sh")
    with open(script, "w") as fh:
        fh.write(f"""#!/bin/bash
# Waits for StreamGrab to quit, swaps in the new version, and reopens it.
APP={_sh(app)}; NEW={_sh(staged)}; OLD={_sh(old)}
for i in $(seq 1 240); do kill -0 {os.getppid()} 2>/dev/null || break; sleep 0.5; done
rm -rf "$OLD"
if mv "$APP" "$OLD"; then
  if mv "$NEW" "$APP"; then rm -rf "$OLD"; else mv "$OLD" "$APP"; fi
fi
open "$APP"
rm -rf {_sh(tmp)}
""")
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/bash", script], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    # The page asks the window to quit (see static/index.html). Older app windows can't, so if the
    # app is still running after a while, end it; the helper then installs and reopens.
    threading.Thread(target=_quit_parent_later, daemon=True).start()


def _quit_parent_later(wait=12):
    time.sleep(wait)
    try:
        os.kill(os.getppid(), 15)
    except OSError:
        pass
    time.sleep(3)
    os._exit(0)


def _apply_windows(setup, root, tmp):
    pid = os.getpid()  # the launcher runs the engine in its own process
    profile = os.path.join(os.environ.get("LOCALAPPDATA") or "", "StreamGrab", "window")
    script = os.path.join(tmp, "update.cmd")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(f"""@echo off
rem Waits for StreamGrab to exit, installs the new version silently and reopens it.
for /l %%i in (1,1,240) do (
  tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul || goto installed
  timeout /t 1 /nobreak >nul
)
:installed
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \\"Name='msedge.exe'\\" | Where-Object {{ $_.CommandLine -like '*{profile}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
"{setup}" /S /D={root}
start "" "{os.path.join(root, 'StreamGrab.exe')}"
""")
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _sh(s):
    return "'" + s.replace("'", "'\\''") + "'"
