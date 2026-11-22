"""StreamGrab for Windows: starts the engine and opens it in its own app window.

Run by StreamGrab.exe as:  python\\pythonw.exe -X utf8 app\\launcher.pyw
The window is Microsoft Edge in app mode (installed on every Windows 10/11 PC), with its own
profile so it opens as a separate, chrome-less window. The app quits when that window closes;
running recordings are stopped and saved first.
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
BIN = os.path.join(ROOT, "bin")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "StreamGrab")
os.makedirs(LOCAL, exist_ok=True)
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, APP)

LOG = os.path.join(LOCAL, "streamgrab.log")
INSTANCE = os.path.join(LOCAL, "instance.json")


def log(*parts):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


def message_box(text, title="StreamGrab"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


def find_edge():
    candidates = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                    candidates.insert(0, winreg.QueryValue(k, None))
            except OSError:
                pass
    except ImportError:
        pass
    return next((c for c in candidates if c and os.path.exists(c)), None)


def open_window(url):
    """Opens the UI; returns the window's process (None if it fell back to the default browser)."""
    edge = find_edge()
    if not edge:
        import webbrowser
        webbrowser.open(url)
        return None
    args = [edge, f"--app={url}", f"--user-data-dir={os.path.join(LOCAL, 'window')}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync", "--window-size=1280,880",
            # Keep the UI polling while minimised, so long recordings don't look like a closed window.
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=msEdgeSidebarV2,msHubApps,msUndersideButton,EdgeCollections"]
    return subprocess.Popen(args, creationflags=0x08000000)


def keep_window_icon(stop):
    """Edge gives app windows a small favicon scaled up, which looks blurry on high-DPI screens. Give the
    StreamGrab window our own icon at the right size for its DPI (taskbar and title bar), and keep
    re-applying it, since Edge can reset it."""
    try:
        import ctypes
        import ctypes.wintypes as W
        u = ctypes.windll.user32
        u.LoadImageW.restype = W.HANDLE
        u.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u.GetDpiForWindow.restype = W.UINT
        ico = os.path.join(APP, "static", "favicon.ico")
        cache = {}

        def handle(px):
            if px not in cache:
                cache[px] = u.LoadImageW(None, ico, 1, px, px, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            return cache[px]

        def ours():
            found = []

            @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
            def cb(h, _):
                n = u.GetWindowTextLengthW(h)
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                c = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(h, c, 64)
                if b.value == "StreamGrab" and c.value == "Chrome_WidgetWin_1" and u.IsWindowVisible(h):
                    found.append(h)
                return True
            u.EnumWindows(cb, 0)
            return found

        while not stop.is_set():
            for h in ours():
                scale = (u.GetDpiForWindow(h) or 96) / 96
                big, small = handle(round(32 * scale)), handle(round(16 * scale))
                if big and u.SendMessageW(h, 0x007F, 1, 0) != big:  # WM_GETICON, ICON_BIG
                    u.SendMessageW(h, 0x0080, 1, big)    # WM_SETICON, ICON_BIG
                    u.SendMessageW(h, 0x0080, 0, small)  # WM_SETICON, ICON_SMALL
            stop.wait(3)
    except Exception as e:  # noqa: BLE001 - cosmetic only
        log("window icon:", e)


def existing_instance():
    try:
        with open(INSTANCE, encoding="utf-8") as fh:
            port = json.load(fh)["port"]
        url = f"http://127.0.0.1:{port}/"
        with urllib.request.urlopen(url + "api/settings", timeout=2) as r:
            if r.status == 200:
                return url
    except Exception:  # noqa: BLE001
        pass
    return None


def main():
    url = existing_instance()
    if url:  # already running: just bring up another window
        open_window(url)
        return

    import server  # imported after PATH is set so it finds the bundled yt-dlp / ffmpeg
    server.check_tools()
    srv = server.make_server(0)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with open(INSTANCE, "w", encoding="utf-8") as fh:
        json.dump({"port": srv.server_address[1], "pid": os.getpid()}, fh)
    log("started", url)

    window = open_window(url)
    icon_stop = threading.Event()
    threading.Thread(target=keep_window_icon, args=(icon_stop,), daemon=True).start()
    started = time.time()
    # Quit when the window closes. Edge can hand the window to a helper process, so also treat
    # a minute without any request from the UI (it polls every second) as "closed".
    while True:
        time.sleep(1)
        if window is not None and window.poll() is not None and time.time() - server.LAST_SEEN > 5:
            break
        if server.updater.QUIT_REQUESTED.is_set():
            log("closing to install an update")
            break
        seen = server.LAST_SEEN
        if seen and time.time() - seen > 60:
            break
        if not seen and time.time() - started > 300:
            break  # the window never connected
    log("window closed; saving recordings and quitting")
    server.shutdown_jobs(timeout=600)  # a long recording can take minutes to finish writing
    srv.shutdown()
    try:
        os.remove(INSTANCE)
    except OSError:
        pass
    if server.updater.QUIT_REQUESTED.is_set():
        log("exiting for the update")
        os._exit(0)  # don't let a lingering thread keep the process (and its files) alive


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log("fatal:", traceback.format_exc())
        message_box(f"StreamGrab could not start:\n\n{e}\n\nDetails: {LOG}")
