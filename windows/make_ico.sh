#!/bin/bash
# Builds windows/streamgrab.ico (16-256 px, PNG-compressed) from the Mac app icon artwork (macos/make_icon.swift).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
swiftc -O "$ROOT/macos/make_icon.swift" -o "$TMP/make_icon"
"$TMP/make_icon" "$TMP/icon.png" --windows
for s in 16 24 32 48 64 128 256; do sips -z $s $s "$TMP/icon.png" --out "$TMP/$s.png" >/dev/null; done
python3 - "$TMP" "$ROOT/windows/streamgrab.ico" "$ROOT/static/icon.png" <<'PY'
import struct, sys, shutil
tmp, out, web = sys.argv[1:]
sizes = [16, 24, 32, 48, 64, 128, 256]
data = [open(f"{tmp}/{s}.png", "rb").read() for s in sizes]
head = struct.pack("<HHH", 0, 1, len(sizes))
off, entries = 6 + 16 * len(sizes), b""
for s, d in zip(sizes, data):
    entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(d), off); off += len(d)
open(out, "wb").write(head + entries + b"".join(data))
shutil.copy(f"{tmp}/256.png", web)  # window / taskbar icon for the UI page
shutil.copy(out, web.replace("icon.png", "favicon.ico"))
PY
echo "wrote windows/streamgrab.ico and static/icon.png"
