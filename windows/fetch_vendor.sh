#!/bin/bash
# Downloads the Windows components bundled by windows/build.sh into windows/vendor.
set -euo pipefail
cd "$(dirname "$0")"
PY=3.14.10
mkdir -p vendor/wheels
cd vendor
curl -fSLo python-embed.zip "https://www.python.org/ftp/python/$PY/python-$PY-embed-amd64.zip"
# The folder build (not the single-file yt-dlp.exe), which starts much faster: the single-file build
# unpacks itself to %TEMP% on every run.
curl -fSLo yt-dlp_win.zip https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_win.zip
# deno: the JavaScript runtime yt-dlp needs to solve YouTube's playback challenges.
curl -fSLo deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip
curl -fSLo ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip
python3 -m pip download --only-binary=:all: --platform win_amd64 --python-version "${PY%.*}" --implementation cp \
  --no-deps -d wheels pillow==13.0.0 fonttools==4.68.0
echo "✓ vendor ready"
