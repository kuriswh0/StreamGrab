#!/bin/bash
# Builds the Windows version of StreamGrab from macOS (or Linux):
#   dist/windows/StreamGrab-Setup-<version>.exe     installer (per-user, no admin)
#   dist/windows/StreamGrab-Portable-<version>.zip  unzip anywhere, run StreamGrab.exe
#
# Needs: makensis (brew install makensis) and the files in windows/vendor (windows/fetch_vendor.sh).
set -euo pipefail
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8  # makensis crashes (bad_alloc) without a UTF-8 locale
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VERSION="$(tr -d '[:space:]' < VERSION)"  # single source: the VERSION file
VENDOR="$ROOT/windows/vendor"
OUT="$ROOT/dist/windows"
STAGE="$OUT/StreamGrab"

for f in python-embed.zip yt-dlp_win.zip deno.zip ffmpeg.zip; do
  [ -f "$VENDOR/$f" ] || { echo "Missing windows/vendor/$f; run windows/fetch_vendor.sh first." >&2; exit 1; }
done
ls "$VENDOR"/wheels/pillow-*win_amd64.whl "$VENDOR"/wheels/fonttools-*win_amd64.whl >/dev/null
command -v makensis >/dev/null || { echo "makensis not found: brew install makensis" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$STAGE"/{python,app,bin,licenses}

echo "→ Python runtime"
unzip -q "$VENDOR/python-embed.zip" -d "$STAGE/python"
PTH=$(ls "$STAGE"/python/python3*._pth)
PYZIP=$(basename "$(ls "$STAGE"/python/python3*.zip)")
# Module search path: stdlib zip, site-packages (Pillow, fontTools) and the app folder.
printf '%s\r\n.\r\nLib\\site-packages\r\n..\\app\r\n' "$PYZIP" > "$PTH"
mkdir -p "$STAGE/python/Lib/site-packages"
for w in "$VENDOR"/wheels/*.whl; do unzip -q -o "$w" -d "$STAGE/python/Lib/site-packages"; done
find "$STAGE/python/Lib/site-packages" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp "$STAGE/python/LICENSE.txt" "$STAGE/licenses/Python-LICENSE.txt"

echo "→ App"
cp server.py chat.py render.py live.py fastdl.py compat.py convert.py updater.py chatrender.py launcher.pyw VERSION "$STAGE/app/"
cp -R static "$STAGE/app/static"
find "$STAGE/app" -name ".DS_Store" -delete

echo "→ yt-dlp + FFmpeg"
unzip -q "$VENDOR/yt-dlp_win.zip" -d "$STAGE/bin"  # yt-dlp.exe + its _internal folder
unzip -q "$VENDOR/deno.zip" -d "$STAGE/bin"        # deno.exe: yt-dlp finds it on PATH (YouTube's JS challenges)
TMP=$(mktemp -d)
unzip -q "$VENDOR/ffmpeg.zip" -d "$TMP"
FF=$(ls -d "$TMP"/ffmpeg-*)
cp "$FF"/bin/ffmpeg.exe "$FF"/bin/ffprobe.exe "$FF"/bin/*.dll "$STAGE/bin/"
cp "$FF/LICENSE.txt" "$STAGE/licenses/FFmpeg-LICENSE.txt"
rm -rf "$TMP"

cat > "$STAGE/licenses/THIRD-PARTY.txt" <<'TXT'
StreamGrab for Windows bundles these programs, each under its own license:

- Python (python.org embeddable package): PSF License, see Python-LICENSE.txt
- Pillow and fontTools (in python\Lib\site-packages, licenses in their *.dist-info folders)
- yt-dlp (github.com/yt-dlp/yt-dlp): The Unlicense (public domain)
- Deno (github.com/denoland/deno), used by yt-dlp for YouTube: MIT License
- FFmpeg, GPL build from github.com/BtbN/FFmpeg-Builds: GNU GPL v3, see FFmpeg-LICENSE.txt.
  The complete corresponding source code is available from https://ffmpeg.org and the build
  scripts from https://github.com/BtbN/FFmpeg-Builds.
TXT

cat > "$STAGE/README.txt" <<TXT
StreamGrab $VERSION for Windows
=============================

Double-click StreamGrab.exe. The app opens in its own window (it uses Microsoft Edge,
which comes with Windows 10 and 11). Closing the window quits StreamGrab; any live
recording is stopped and saved first.

Downloads go to Downloads\\StreamGrab by default (change it in Settings).
Settings: %USERPROFILE%\\.streamgrab.json   Log: %LOCALAPPDATA%\\StreamGrab\\streamgrab.log

Keep StreamGrab.exe together with the python, app and bin folders.
TXT

echo "→ StreamGrab.exe"
makensis -V2 -DOUT="$STAGE/StreamGrab.exe" -DICON="$ROOT/windows/streamgrab.ico" -DVERSION="$VERSION" \
  windows/launcher.nsi

echo "→ Portable zip"
(cd "$OUT" && zip -qry "StreamGrab-Portable-$VERSION.zip" StreamGrab)

echo "→ Installer"
SIZE_KB=$(du -sk "$STAGE" | cut -f1)
makensis -V2 -DOUT="$OUT/StreamGrab-Setup-$VERSION.exe" -DICON="$ROOT/windows/streamgrab.ico" \
  -DSTAGE="$STAGE" -DVERSION="$VERSION" -DSIZE_KB="$SIZE_KB" windows/installer.nsi

ls -lh "$OUT"/*.exe "$OUT"/*.zip
echo "✓ Built $OUT"
