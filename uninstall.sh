#!/bin/bash
# Removes VNC Turbo. Run "Undo my changes" in the app first if you want your old settings back.
rm -rf "$HOME/.local/share/vnc-turbo"
rm -f "$HOME/.local/bin/vnc-turbo" "$HOME/.local/share/applications/vnc-turbo.desktop" "$HOME/Desktop/vnc-turbo.desktop"
echo "VNC Turbo removed. (Your undo history is kept in ~/.config/vnc-turbo)"
