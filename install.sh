#!/usr/bin/env bash
# Install Video Workflow for this user and pin it to the nwg-dock.
# Re-run after changing the code to update the installed copy.
set -euo pipefail

APP_ID=local.descentkatil.VideoWorkflow
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/video-workflow"
BIN="$HOME/.local/bin/video-workflow"
DESKTOP="$DATA/applications/$APP_ID.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
PINNED="${XDG_CACHE_HOME:-$HOME/.cache}/nwg-dock-pinned"

command -v ffmpeg >/dev/null || echo "warning: ffmpeg is missing; Polish voiceover needs it" >&2
python3 -c 'import gi; gi.require_version("Adw", "1"); from gi.repository import Adw' \
  || { echo "error: needs python-gobject and libadwaita" >&2; exit 1; }

# App files
mkdir -p "$DEST"
install -m 644 "$SRC"/app.py "$SRC"/apps.py "$SRC"/core.py "$DEST"/
rm -f "$DEST/resolve_sync.py" "$DEST/resolve_sync.lua"
rm -rf "$DEST/__pycache__"
python3 -m compileall -q "$DEST"

# Command
mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$DEST/app.py" "\$@"
EOF
chmod 755 "$BIN"

# Icon
install -Dm 644 "$SRC/icon.svg" "$ICON"
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

# Launcher, named after the app ID so the dock matches the running window to it
rm -f "$DATA/applications/VideoWorkflow.desktop"
install -Dm 644 /dev/stdin "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Video Workflow
GenericName=Video Project Manager
Comment=One folder per video, with OBS, Audacity and DaVinci Resolve pointed at it
Exec=$BIN
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Video;
Keywords=youtube;obs;audacity;resolve;davinci;voiceover;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
update-desktop-database -q "$DATA/applications" 2>/dev/null || true

# Dock
if [[ "${1:-}" != "--no-pin" ]]; then
  touch "$PINNED"
  if ! grep -qxF "$APP_ID" "$PINNED"; then
    echo "$APP_ID" >> "$PINNED"
    # nwg-dock only reads the pinned list at startup: restart it with its own arguments.
    if pid=$(pgrep -x nwg-dock-hyprla | head -1) && [[ -n "$pid" ]]; then
      mapfile -d '' args < "/proc/$pid/cmdline"
      kill "$pid"
      for _ in {1..20}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      launcher=(); command -v uwsm-app >/dev/null && launcher=(uwsm-app --)
      setsid -f "${launcher[@]}" "${args[@]}" >/dev/null 2>&1 < /dev/null
    fi
    echo "Pinned to the dock."
  fi
fi

echo "Installed. Open it from the app menu, the dock, or run: video-workflow"
