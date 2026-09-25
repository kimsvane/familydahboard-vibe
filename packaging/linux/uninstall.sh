#!/usr/bin/env bash
set -eu

PREFIX="/opt/family-dashboard"
DATA_DIR="/var/lib/family-dashboard"
SERVICE_NAME="family-dashboard"
SERVICE_USER="family-dashboard"
PURGE=0

usage() {
  printf '%s\n' "Usage: sudo $0 [options]" "" "Options:" "  --prefix PATH       Install prefix (default: /opt/family-dashboard)" "  --data-dir PATH     Data directory (default: /var/lib/family-dashboard)" "  --service-name NAME systemd service name (default: family-dashboard)" "  --user NAME         Service user (default: family-dashboard)" "  --purge             Also remove the application, data, and environment file" "  --help              Show this help"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
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
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

if [ "$PURGE" -eq 1 ]; then
  rm -f "/etc/${SERVICE_NAME}.env"
  rm -rf "$PREFIX" "$DATA_DIR"
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  if getent group "$SERVICE_USER" >/dev/null 2>&1; then
    groupdel "$SERVICE_USER" 2>/dev/null || true
  fi
  printf 'Family Dashboard and its data were removed.\n'
else
  printf 'Family Dashboard was stopped. Re-run with --purge to remove files and data.\n'
fi
