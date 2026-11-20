#!/bin/bash
# Builds dist/StreamGrab.app (native macOS window around the Python engine).
# Usage: macos/build.sh            -> dist/StreamGrab.app
#        macos/build.sh --install  -> also copies it to /Applications
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
APP="$ROOT/dist/StreamGrab.app"
BUILD="$ROOT/build"
VERSION="$(tr -d '[:space:]' < VERSION)"  # single source: the VERSION file

rm -rf "$APP" "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app" "$BUILD"

echo "→ Compiling Swift shell"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos12.0" \
  -framework AppKit -framework WebKit \
  macos/main.swift -o "$APP/Contents/MacOS/StreamGrab"

echo "→ Rendering icon"
swiftc -O macos/make_icon.swift -o "$BUILD/make_icon"
"$BUILD/make_icon" "$BUILD/icon_1024.png"
ICONSET="$BUILD/AppIcon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "→ Bundling Python engine"
cp server.py chat.py render.py live.py fastdl.py compat.py convert.py updater.py VERSION "$APP/Contents/Resources/app/"
mkdir -p "$APP/Contents/Resources/app/bin"
echo "→ Compiling chat renderer"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos12.0" macos/chatrender.swift \
  -o "$APP/Contents/Resources/app/bin/chatrender"
cp -R static "$APP/Contents/Resources/app/static"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>StreamGrab</string>
  <key>CFBundleDisplayName</key><string>StreamGrab</string>
  <key>CFBundleIdentifier</key><string>local.streamgrab.app</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>StreamGrab</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "→ Signing (ad-hoc)"
codesign --force --deep --sign - "$APP"
rm -rf "$BUILD"

if [[ "${1:-}" == "--install" ]]; then
  rm -rf /Applications/StreamGrab.app
  cp -R "$APP" /Applications/
  echo "✓ Installed /Applications/StreamGrab.app"
fi
echo "✓ Built $APP"
