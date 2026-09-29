#!/bin/sh
set -eu

APP_NAME="family-dashboard-kiosk"
APP_DIR="/opt/$APP_NAME"
LAUNCHER="/usr/local/bin/$APP_NAME"
AUTOSTART_FILE="/etc/xdg/autostart/$APP_NAME.desktop"
EXECUTABLE="$APP_DIR/$APP_NAME"

if [ ! -x "$EXECUTABLE" ]; then
  printf 'Family Dashboard kiosk executable not found at %s\n' "$EXECUTABLE" >&2
  exit 1
fi

ln -sfn "$EXECUTABLE" "$LAUNCHER"
install -d -m 0755 /etc/xdg/autostart
cat >"$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=application
Version=1.0
Name=Family Dashboard
Comment=Vis Family Dashboard i fuldskærm
Exec=/usr/bin/systemd-inhibit --what=idle-sleep --mode=block --why=FamilyDashboardKiosk $LAUNCHER
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
chmod 0644 "$AUTOSTART_FILE"

# Skærmlysstyringen skal være skrivbar uden root, ellers kan dashboardet
# ikke dæmpe skærmen. Uden denne regel er filen rod-ejet igen efter boot.
UDEV_RULE_SRC="$APP_DIR/resources/backlight.rules"
UDEV_RULE_DST="/etc/udev/rules.d/99-surface-dashboard-backlight.rules"
if [ -f "$UDEV_RULE_SRC" ]; then
  install -d -m 0755 /etc/udev/rules.d
  install -m 0644 "$UDEV_RULE_SRC" "$UDEV_RULE_DST"
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=backlight --action=add 2>/dev/null || true
fi
