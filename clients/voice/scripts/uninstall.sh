#!/usr/bin/env bash
set -eu

PREFIX="/opt/family-dashboard-voice"
DATA_DIR="/var/lib/family-dashboard-voice"
SERVICE_NAME="family-dashboard-voice"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="/etc/${SERVICE_NAME}.env"
PURGE=0

usage() {
  printf '%s\n' "Usage: sudo $0 [options]" "" "Options:" "  --purge           Also remove the application, data, and environment file" "  --help            Show this help"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "This uninstaller must run as root." >&2
  exit 1
fi

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl daemon-reload

if [ "$PURGE" -eq 1 ]; then
  rm -f "$ENV_FILE"
  rm -rf "$PREFIX" "$DATA_DIR"
  printf '%s\n' "Family Dashboard Voice and its data were removed."
else
  printf '%s\n' "Family Dashboard Voice was stopped. Re-run with --purge to remove files and data."
fi