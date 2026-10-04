#!/usr/bin/env bash
set -eu

PREFIX="/opt/family-dashboard-voice"
APP_DIR="$PREFIX/app"
VENV_DIR="$PREFIX/venv"
SCRIPTS_DIR="$PREFIX/scripts"
DATA_DIR="/var/lib/family-dashboard-voice"
SERVICE_NAME="family-dashboard-voice"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="/etc/${SERVICE_NAME}.env"
ENV_TEMPLATE="$PREFIX/etc/family-dashboard-voice.env"
SERVICE_TEMPLATE="$PREFIX/etc/family-dashboard-voice.service.in"
LVA_VERSION="v1.1.15"
LVA_URL="https://github.com/OHF-Voice/linux-voice-assistant/archive/refs/tags/${LVA_VERSION}.tar.gz"
FROM_RPM=0
NO_DEPS=0
FORCE=0
SERVICE_USER=""

usage() {
  printf '%s\n' "Usage: sudo $0 [options]" "" "Options:" "  --user NAME       Run the service as NAME (default: the auto-login desktop user)" "  --from-rpm        Payload is already installed; only configure (used by the RPM)" "  --no-deps         Skip installing RPM dependencies" "  --force           Replace an existing service configuration" "  --help            Show this help"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --user) SERVICE_USER="$2"; shift 2 ;;
    --from-rpm) FROM_RPM=1; shift ;;
    --no-deps) NO_DEPS=1; shift ;;
    --force) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "This installer must run as root." >&2
  exit 1
fi

if [ "${FAMILY_DASHBOARD_VOICE_USER:-}" != "" ] && [ -z "$SERVICE_USER" ]; then
  SERVICE_USER="$FAMILY_DASHBOARD_VOICE_USER"
fi

REQUIRES="python3 python3-pip python3-devel gcc gcc-c++ make pipewire pipewire-pulseaudio pipewire-alsa alsa-utils alsa-lib mpv-libs avahi-tools pulseaudio-utils iproute procps-ng curl ca-certificates"
if [ "$FROM_RPM" -eq 1 ] || [ "$NO_DEPS" -eq 1 ]; then
  :
elif [ -x "$(command -v dnf)" ]; then
  printf '%s\n' "Installing dependencies..."
  dnf install -y $REQUIRES
fi

find_service_user() {
  if [ -n "$SERVICE_USER" ]; then return; fi
  local sess uid
  sess=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$4 == "seat0" {print $1; exit}')
  if [ -n "$sess" ]; then
    SERVICE_USER=$(loginctl show-session "$sess" -p User --value 2>/dev/null || true)
    printf '%s\n' "Using user from graphical session: $SERVICE_USER"
    return
  fi
  for sock in /run/user/*/pulse/native; do
    if [ -e "$sock" ]; then
      SERVICE_USER=$(stat -c '%U' "$sock")
      printf '%s\n' "Using user owning the PulseAudio socket: $SERVICE_USER"
      return
    fi
  done
  if id 1000 >/dev/null 2>&1; then
    SERVICE_USER="$(id -un 1000)"
    printf '%s\n' "WARNING: No interactive session found; using uid 1000 ($SERVICE_USER). Pass --user to override." >&2
    return
  fi
  printf '%s\n' "Could not determine the desktop user. Pass --user NAME." >&2
  exit 1
}

if [ -z "$SERVICE_USER" ]; then
  find_service_user
fi

SERVICE_UID="$(id -u "$SERVICE_USER")"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$FROM_RPM" -eq 1 ]; then
  SYSTEMD_SRC="$PREFIX/etc/family-dashboard-voice.service.in"
  ENV_SRC="$PREFIX/etc/family-dashboard-voice.env"
else
  SYSTEMD_SRC="$SCRIPT_DIR/../systemd/family-dashboard-voice.service.in"
  ENV_SRC="$SCRIPT_DIR/../etc/family-dashboard-voice.env"
fi

install -d -m 0755 "$APP_DIR" "$SCRIPTS_DIR" "$PREFIX/etc"
install -d -m 0750 "$DATA_DIR"
install -d -m 0755 "$DATA_DIR/sounds/custom" "$DATA_DIR/wakewords/custom"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"

if [ ! -d "$APP_DIR/linux_voice_assistant" ] || [ ! -f "$APP_DIR/pyproject.toml" ]; then
  printf '%s\n' "Downloading Linux Voice Assistant $LVA_VERSION..."
  tmp="$(mktemp -d)"
  curl -fL "$LVA_URL" -o "$tmp/lva.tar.gz"
  tar -xzf "$tmp/lva.tar.gz" -C "$tmp" --strip-components=1
  rm -rf "$APP_DIR"/*
  cp -a "$tmp/." "$APP_DIR/"
  rm -rf "$tmp"
else
  printf '%s\n' "Linux Voice Assistant $LVA_VERSION already present at $APP_DIR."
fi

if [ "$FROM_RPM" -ne 1 ]; then
  cp -a "$SYSTEMD_SRC" "$SERVICE_TEMPLATE"
  cp -a "$ENV_SRC" "$ENV_TEMPLATE"
  cp -a "$SCRIPT_DIR/install.sh" "$SCRIPTS_DIR/install.sh"
  cp -a "$SCRIPT_DIR/uninstall.sh" "$SCRIPTS_DIR/uninstall.sh"
  chmod 0755 "$SCRIPTS_DIR/install.sh" "$SCRIPTS_DIR/uninstall.sh"
fi

chmod 0755 "$APP_DIR/docker-entrypoint.sh" "$APP_DIR"/script/* 2>/dev/null || true
chmod 0755 "$SCRIPTS_DIR/install.sh" "$SCRIPTS_DIR/uninstall.sh" 2>/dev/null || true

printf '%s\n' "Creating virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
SETUPTOOLS_SCM_PRETEND_VERSION="${LVA_VERSION#v}" "$VENV_DIR/bin/pip" install "$APP_DIR"
"$VENV_DIR/bin/linux-voice-assistant" --help >/dev/null 2>&1 || true

if [ ! -f "$ENV_FILE" ] || [ "$FORCE" -eq 1 ]; then
  install -m 0640 /dev/null "$ENV_FILE"
  cat "$ENV_SRC" >"$ENV_FILE"
fi
chown root:root "$ENV_FILE"

printf '%s\n' "Writing $SERVICE_FILE"
sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__GROUP__|$SERVICE_GROUP|g" \
    -e "s|__UID__|$SERVICE_UID|g" \
    -e "s|__PREFIX__|$PREFIX|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    "$SERVICE_TEMPLATE" >"$SERVICE_FILE"
chmod 0644 "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
if [ "$FROM_RPM" -eq 1 ]; then
  systemctl restart "$SERVICE_NAME" >/dev/null 2>&1 || true
else
  systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
fi

printf '%s\n' "Family Dashboard Voice is installed."
printf '%s\n' "  Service: $SERVICE_NAME (kører som $SERVICE_USER)"
printf '%s\n' "  Konfig:  $ENV_FILE"
printf '%s\n' "  Lyd/status-tjek: systemctl status $SERVICE_NAME && journalctl -u $SERVICE_NAME -f"
printf '%s\n' "Tilslut den i Home Assistant: Indstillinger -> Enheder og tjenester -> ESPHome -> Tilføj, indtast enhedens IP med port 6053."
printf '%s\n' "Lyt nu efter wake word 'okay nabu' (skift i $ENV_FILE)."