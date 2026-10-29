#!/bin/bash
# VNC Turbo installer — Raspberry Pi 5 (Raspberry Pi OS Bookworm / Trixie)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/vnc-turbo"

echo "⚡ Installing VNC Turbo..."
mkdir -p "$DEST" "$HOME/.local/share/applications" "$HOME/.local/bin"
cp "$DIR/vnc_turbo.py" "$DIR/vnc-turbo.svg" "$DEST/"
[ -f "$DIR/vnc-turbo.png" ] && cp "$DIR/vnc-turbo.png" "$DEST/"
chmod +x "$DEST/vnc_turbo.py"

if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "Installing python3-tk (needs your password)..."
  sudo apt-get install -y python3-tk
fi

ln -sf "$DEST/vnc_turbo.py" "$HOME/.local/bin/vnc-turbo"

DESKTOP_FILE="$HOME/.local/share/applications/vnc-turbo.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=VNC Turbo
Comment=Find and fix what's making VNC slow
Exec=python3 $DEST/vnc_turbo.py
Icon=$DEST/vnc-turbo.svg
Terminal=false
Categories=System;Utility;
EOF

if [ -d "$HOME/Desktop" ]; then
  cp "$DESKTOP_FILE" "$HOME/Desktop/vnc-turbo.desktop"
  chmod +x "$HOME/Desktop/vnc-turbo.desktop"
  gio set "$HOME/Desktop/vnc-turbo.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "✅ Done! Open it from the menu (System Tools / Accessories), the Desktop icon,"
echo "   or type:  vnc-turbo        (text report over SSH:  vnc-turbo --scan)"
