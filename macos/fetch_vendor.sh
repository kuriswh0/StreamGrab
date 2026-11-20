#!/bin/bash
# Downloads the components bundled by macos/make_dmg.sh into macos/vendor (Apple Silicon).
set -euo pipefail
cd "$(dirname "$0")"
PBS=20261110
PY=3.14.10
mkdir -p vendor
cd vendor
curl -fSLo python.tar.gz "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS/cpython-$PY%2B$PBS-aarch64-apple-darwin-install_only_stripped.tar.gz"
# The folder build (not the single-file yt-dlp_macos): it starts in ~0.2 s instead of ~4 s, because the
# single-file build unpacks itself to a temp folder on every run.
curl -fSLo yt-dlp_macos.zip https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos.zip
# deno: the JavaScript runtime yt-dlp needs to solve YouTube's playback challenges.
curl -fSLo deno.zip https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip
curl -fSLo ffmpeg.zip https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip
curl -fSLo ffprobe.zip https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip
echo "✓ vendor ready"
