#!/bin/sh
set -eu

rm -f /etc/xdg/autostart/family-dashboard-kiosk.desktop
rm -f /usr/local/bin/family-dashboard-kiosk

# Fjern kun vores egen udev-regel, så skærmlysstyringen ikke efterlades
# med de rettigheder vi har sat.
rm -f /etc/udev/rules.d/99-surface-dashboard-backlight.rules
udevadm control --reload-rules 2>/dev/null || true
