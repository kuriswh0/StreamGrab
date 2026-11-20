#!/bin/bash
# Builds a shareable, self-contained Mac installer:
#   dist/mac/StreamGrab-<version>.dmg   (open it, drag StreamGrab to Applications)
#
# Unlike macos/build.sh (which uses Homebrew's python3 / yt-dlp / ffmpeg), this app carries its
# own Python, yt-dlp and FFmpeg, so it runs on any Apple Silicon Mac (macOS 12+).
# Needs the files in macos/vendor (macos/fetch_vendor.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VERSION="$(tr -d '[:space:]' < VERSION)"  # single source: the VERSION file
VENDOR="$ROOT/macos/vendor"
OUT="$ROOT/dist/mac"
for f in python.tar.gz yt-dlp_macos.zip deno.zip ffmpeg.zip ffprobe.zip; do
  [ -f "$VENDOR/$f" ] || { echo "Missing macos/vendor/$f; run macos/fetch_vendor.sh first." >&2; exit 1; }
done

echo "→ Building the app"
macos/build.sh >/dev/null
rm -rf "$OUT"
mkdir -p "$OUT/dmg"
APP="$OUT/dmg/StreamGrab.app"
cp -R "$ROOT/dist/StreamGrab.app" "$APP"
RES="$APP/Contents/Resources"

echo "→ Bundling Python"
tar xzf "$VENDOR/python.tar.gz" -C "$RES"
PYLIB=$(ls -d "$RES"/python/lib/python3.*)
# Trim what a background engine never uses (IDE, GUI toolkit, tests, pip).
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$PYLIB"/site-packages/pip* \
       "$RES"/python/lib/{tcl,tk,itcl,thread}* "$RES"/python/share "$RES"/python/include \
       "$RES"/python/bin/{idle,pip,pydoc,python3-config,python3.*-config}*
find "$RES/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
mkdir -p "$RES/licenses"
[ -f "$RES/python/LICENSE" ] && mv "$RES/python/LICENSE" "$RES/licenses/Python-LICENSE.txt" || true

echo "→ Bundling yt-dlp + FFmpeg"
mkdir -p "$RES/bin"
# yt-dlp's folder build: the program plus its _internal folder, which must sit next to it.
TMP=$(mktemp -d)
unzip -q "$VENDOR/yt-dlp_macos.zip" -d "$TMP"
mv "$TMP/yt-dlp_macos" "$RES/bin/yt-dlp"
mv "$TMP/_internal" "$RES/bin/_internal"
rm -rf "$TMP"
unzip -q -o "$VENDOR/ffmpeg.zip" -d "$RES/bin"
unzip -q -o "$VENDOR/ffprobe.zip" -d "$RES/bin"
unzip -q -o "$VENDOR/deno.zip" -d "$RES/bin"  # yt-dlp finds it on PATH (YouTube's JS challenges)
chmod +x "$RES"/bin/yt-dlp "$RES"/bin/ffmpeg "$RES"/bin/ffprobe "$RES"/bin/deno
xattr -cr "$APP"
cat > "$RES/licenses/THIRD-PARTY.txt" <<'TXT'
StreamGrab for Mac bundles these programs, each under its own license:

- Python (github.com/astral-sh/python-build-standalone): PSF License, see Python-LICENSE.txt
- yt-dlp (github.com/yt-dlp/yt-dlp): The Unlicense (public domain)
- Deno (github.com/denoland/deno), used by yt-dlp for YouTube: MIT License
- FFmpeg and FFprobe, static builds from https://ffmpeg.martin-riedl.de: GNU GPL v3 (with libx264).
  The complete corresponding source code is available from https://ffmpeg.org; build scripts
  from https://git.martin-riedl.de/ffmpeg/build-script.
TXT

echo "→ Signing (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "→ DMG"
ln -s /Applications "$OUT/dmg/Applications"
cat > "$OUT/dmg/First time opening.txt" <<'TXT'
Installing StreamGrab
=====================

1. Drag StreamGrab onto the Applications folder.
2. Open StreamGrab from Applications.

StreamGrab isn't notarized by Apple, so the first time macOS says it "could not verify"
the app. To allow it:

   System Settings → Privacy & Security → scroll down → "StreamGrab was blocked" → Open Anyway

(or, in Terminal:  xattr -dr com.apple.quarantine /Applications/StreamGrab.app)

You only need to do this once. Requires an Apple Silicon Mac (M1 or later), macOS 12+.
TXT
DMG="$OUT/StreamGrab-$VERSION.dmg"
hdiutil create -quiet -volname "StreamGrab" -srcfolder "$OUT/dmg" -fs HFS+ -format ULFO -ov "$DMG"
rm -rf "$OUT/dmg"
ls -lh "$DMG"
echo "✓ Built $DMG"
