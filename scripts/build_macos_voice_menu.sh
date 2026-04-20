#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/macos/CodexVoiceMenuBar.swift"
OUT_DIR="$ROOT/.build"
APP_DIR="$OUT_DIR/CodexVoiceMenuBar.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
BIN="$MACOS_DIR/CodexVoiceMenuBar"
PLIST="$CONTENTS_DIR/Info.plist"

mkdir -p "$MACOS_DIR"

if [[ ! -x "$BIN" || "$SRC" -nt "$BIN" ]]; then
  xcrun swiftc \
    -O \
    -parse-as-library \
    -framework AppKit \
    -framework Foundation \
    "$SRC" \
    -o "$BIN"
fi

cat > "$PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>CodexVoiceMenuBar</string>
  <key>CFBundleIdentifier</key>
  <string>local.vibevoice.codexvoicemenubar</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Codex Voice Menu</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

echo "$APP_DIR"
