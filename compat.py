"""Small cross-platform layer (macOS / Windows) for process handling and app folders.

Import this before anything that starts subprocesses. On Windows it makes every child process
(yt-dlp, ffmpeg, ffprobe, the chat renderer) start without a console window and decode its text
output as UTF-8, so the app never flashes black windows or trips over non-ASCII titles.
"""
import os
import signal
import subprocess
import sys

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"

if WINDOWS:
    _CREATE_NO_WINDOW = 0x08000000
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")  # yt-dlp.exe prints titles as UTF-8
    os.environ.setdefault("PYTHONUTF8", "1")

    class _QuietPopen(subprocess.Popen):
        def __init__(self, *args, **kw):
            flags = kw.get("creationflags", 0) | _CREATE_NO_WINDOW
            if kw.pop("start_new_session", False):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            kw["creationflags"] = flags
            if (kw.get("text") or kw.get("universal_newlines")) and not kw.get("encoding"):
                kw["encoding"] = "utf-8"
                kw.setdefault("errors", "replace")
            super().__init__(*args, **kw)

    subprocess.Popen = _QuietPopen  # subprocess.run() and friends look this name up at call time


def signal_group(proc, sig):
    """Signals a subprocess and everything it started (yt-dlp runs ffmpeg as a child).

    Windows has no process-group signals for windowless children, so any signal ends the
    whole process tree; callers recover partial live recordings from the .part file.
    """
    if not proc or proc.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def kill_tree(proc):
    signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))


def cache_dir(*parts):
    if WINDOWS:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "StreamGrab")
    elif MAC:
        base = os.path.expanduser("~/Library/Caches/StreamGrab")
    else:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "streamgrab")
    return os.path.join(base, *parts)


# Windows only lets the foreground app bring windows to the front, and that's the StreamGrab window
# (Edge), not this background process: without help the dialog opens behind it, invisible. So it gets a
# visible always-on-top owner and takes the focus (the Alt key press is the standard way to be allowed).
_FRONT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -Namespace SG -Name W -MemberDefinition '"
    "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(System.IntPtr h);"
    "[DllImport(\"user32.dll\")] public static extern void keybd_event(byte k, byte s, uint f, System.UIntPtr e);';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false;Opacity=0;"
    "FormBorderStyle='None';StartPosition='CenterScreen';Width=1;Height=1};"
)
_SHOW = (
    "$o.Add_Shown({[SG.W]::keybd_event(0x12,0,0,[UIntPtr]::Zero);[SG.W]::keybd_event(0x12,0,2,[UIntPtr]::Zero);"
    "[void][SG.W]::SetForegroundWindow($o.Handle);$o.Activate();"
    "if($d.ShowDialog($o) -eq 'OK'){[Console]::Out.Write(RESULT)};$o.Close()});"
    "[void]$o.ShowDialog()"
)


def _dialog(setup, result, env=None):
    script = _FRONT + setup + _SHOW.replace("RESULT", result)
    p = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, env=env, timeout=600)
    return p.stdout or ""


def pick_folder(current=""):
    """Native folder dialog on Windows (the macOS app has its own). Returns a path or ''."""
    if not WINDOWS:
        return ""
    setup = ("$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
             "$d.Description='Choose where StreamGrab saves downloads';$d.ShowNewFolderButton=$true;"
             "if($env:SG_CURRENT -and (Test-Path $env:SG_CURRENT)){$d.SelectedPath=$env:SG_CURRENT};")
    env = {**os.environ, "SG_CURRENT": os.path.expanduser(current or "")}
    return _dialog(setup, "$d.SelectedPath", env).strip()


def pick_files():
    """Native multi-file dialog on Windows (the macOS app has its own). Returns a list of paths."""
    if not WINDOWS:
        return []
    setup = ("$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Multiselect=$true;"
             "$d.Title='Choose audio files to convert';"
             "$d.Filter='Audio and video|*.mp3;*.flac;*.wav;*.aif;*.aiff;*.ogg;*.opus;*.m4a;*.aac;*.wma;*.alac;*.ape;"
             "*.wv;*.mka;*.ac3;*.mp4;*.mkv;*.mov;*.webm|All files|*.*';")
    return [l.strip() for l in _dialog(setup, "($d.FileNames -join \"`n\")").splitlines() if l.strip()]
