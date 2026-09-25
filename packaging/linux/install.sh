#!/usr/bin/env bash
set -eu

PREFIX="/opt/family-dashboard"
DATA_DIR="/var/lib/family-dashboard"
SERVICE_NAME="family-dashboard"
SERVICE_USER="family-dashboard"
SERVICE_GROUP="family-dashboard"
PORT="8080"
FORCE=0

usage() {
  printf '%s\n' "Usage: sudo $0 [options]" "" "Options:" "  --prefix PATH       Install prefix (default: /opt/family-dashboard)" "  --data-dir PATH     Persistent data directory (default: /var/lib/family-dashboard)" "  --service-name NAME systemd service name (default: family-dashboard)" "  --user NAME         Service user (default: family-dashboard)" "  --port PORT         Service port (default: 8080)" "  --force             Reinstall even if the service exists" "  --help              Show this help"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "This installer must run as root." >&2
  exit 1
fi

if [ ! -x "$(command -v python3)" ]; then
  printf '%s\n' "python3 is required." >&2
  exit 1
fi

python3 -m venv --help >/dev/null 2>&1 || true
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="/etc/${SERVICE_NAME}.env"

if [ -e "$SERVICE_FILE" ] && [ "$FORCE" -ne 1 ]; then
  printf 'Service %s already exists. Use --force to replace it.\n' "$SERVICE_NAME" >&2
  exit 1
fi

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 "$PREFIX" "$PREFIX/app" "$PREFIX/web"
install -d -m 0750 "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"

if [ -f "$PREFIX/requirements.txt" ] && [ "$FORCE" -ne 1 ]; then
  printf 'Existing installation found at %s. Use --force to update it.\n' "$PREFIX" >&2
  exit 1
fi

cp -a "$PROJECT_ROOT/app/." "$PREFIX/app/"
cp -a "$PROJECT_ROOT/web/." "$PREFIX/web/"
cp -a "$PROJECT_ROOT/requirements.txt" "$PREFIX/requirements.txt"
cp -a "$PROJECT_ROOT/pyproject.toml" "$PREFIX/pyproject.toml"

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/python" -m pip install --upgrade pip
"$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"
"$PREFIX/venv/bin/pip" install --no-deps --force-reinstall "$PREFIX"
chown -R root:root "$PREFIX"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PREFIX/venv"

if [ ! -f "$ENV_FILE" ]; then
  install -m 0640 /dev/null "$ENV_FILE"
  cat >"$ENV_FILE" <<EOF
FAMILY_DASHBOARD_HOST=127.0.0.1
FAMILY_DASHBOARD_PORT=$PORT
FAMILY_DASHBOARD_DATA_DIR=$DATA_DIR
FAMILY_DASHBOARD_STATIC_DIR=$PREFIX/web
FAMILY_DASHBOARD_PASSWORD=change-me
FAMILY_DASHBOARD_SECRET_KEY=change-this-to-a-long-random-value
FAMILY_DASHBOARD_TIMEZONE=Europe/Copenhagen
FAMILY_DASHBOARD_SYNC_INTERVAL=900
FAMILY_DASHBOARD_BACKGROUND_SYNC=true
FAMILY_DASHBOARD_SECURE_COOKIES=false
FAMILY_DASHBOARD_ALLOWED_ORIGINS=http://localhost:$PORT,http://127.0.0.1:$PORT
EOF
  chmod 0640 "$ENV_FILE"
  chown root:"$SERVICE_GROUP" "$ENV_FILE"
  printf 'Created %s. Change FAMILY_DASHBOARD_PASSWORD and FAMILY_DASHBOARD_SECRET_KEY before using it.\n' "$ENV_FILE"
fi

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Family Dashboard
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PREFIX
EnvironmentFile=$ENV_FILE
ExecStart=$PREFIX/venv/bin/family-dashboard
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
printf 'Family Dashboard is installed and started at http://127.0.0.1:%s\n' "$PORT"
printf 'Edit %s to change the password, secret, timezone, or network settings.\n' "$ENV_FILE"
