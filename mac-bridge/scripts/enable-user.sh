#!/bin/bash
# Sætter FamilyBridge i gang for en bestemt bruger.
#
# Installations-pkg'en konfigurerer LaunchAgent for den bruger, der er logget
# ind under installationen. Hvis en anden bruger logger ind på maskinen, skal
# dette script køres for vedkommende, ellers læser bridgeen fra den gamle brugers
# databaser og bruger en token, dashboardet ikke kender.
#
#   sudo /Library/Application\ Support/FamilyBridge/enable-user.sh <brugernavn>
#
# Kør det én gang per bruger, der skal have adgang. Scriptet er idempotent:
# at køre det igen er harmløst og genstarter bare bridgeen.
set -euo pipefail

BUNDLE_ID="dk.ssvanefamily.bridge"
NAME="FamilyBridge"
APP="/Applications/${NAME}.app"
EXECUTABLE="${APP}/Contents/MacOS/${NAME}"
TEMPLATE="/Library/Application Support/FamilyBridge/LaunchAgent.plist.template"
CONFIG_DIR_TEMPLATE="/Library/Application Support/FamilyBridge"

usage() {
  echo "Brug: sudo $0 <brugernavn>" >&2
  echo "Uden argument installeres for den bruger, der er logget ind nu." >&2
}

TARGET_USER="${1:-}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" = "-h" ] || [ "$TARGET_USER" = "--help" ]; then
  usage
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Skal køres med sudo." >&2
  usage
  exit 1
fi

if [ ! -x "$EXECUTABLE" ]; then
  echo "Fejl: $EXECUTABLE findes ikke. Er FamilyBridge installeret?" >&2
  exit 1
fi
if [ ! -f "$TEMPLATE" ]; then
  echo "Fejl: mangler $TEMPLATE. Geninstaller .pkg'en." >&2
  exit 1
fi

if [ -z "$TARGET_USER" ]; then
  TARGET_USER="$(stat -f%Su /dev/console 2>/dev/null || true)"
  if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "Fejl: ingen bruger er logget ind. Angiv et brugernavn: sudo $0 <brugernavn>" >&2
    exit 1
  fi
  echo "Ingen bruger angivet: bruger den loggede bruger '$TARGET_USER'."
fi

if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
  echo "Fejl: brugeren '$TARGET_USER' findes ikke." >&2
  exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(dscl . -read "/Users/${TARGET_USER}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [ -z "$TARGET_HOME" ] || [ ! -d "$TARGET_HOME" ]; then
  echo "Fejl: kunne ikke finde hjemmappen for '$TARGET_USER'." >&2
  exit 1
fi

AGENT_DIR="${TARGET_HOME}/Library/LaunchAgents"
AGENT_PLIST="${AGENT_DIR}/${BUNDLE_ID}.plist"
CONFIG_PATH="${TARGET_HOME}/Library/Application Support/${NAME}/config.json"

echo "==> Sætter $NAME op for $TARGET_USER (uid $TARGET_UID)"

# 1. Fjern et gammelt LaunchDaemon-layout, hvis en tidligere version havde det.
if [ -f "/Library/LaunchDaemons/${BUNDLE_ID}.plist" ]; then
  echo "    fjerner forældet LaunchDaemon-plist"
  launchctl bootout "system/${BUNDLE_ID}" >/dev/null 2>&1 || true
  rm -f "/Library/LaunchDaemons/${BUNDLE_ID}.plist"
fi

# 2. LaunchAgent i brugerens egen mappe, så EventKit kører i deres login-session.
mkdir -p "$AGENT_DIR"
sed -e "s/__BUNDLE_ID__/${BUNDLE_ID}/g" \
    -e "s/__LABEL__/${BUNDLE_ID}/g" \
    -e "s#__EXECUTABLE__#${EXECUTABLE}#g" \
    "$TEMPLATE" > "$AGENT_PLIST"
chown "$TARGET_USER":staff "$AGENT_PLIST"
chmod 644 "$AGENT_PLIST"

# 3. Ejet config/token, så brugeren selv kan læse sit eget token med --token.
if [ ! -f "$CONFIG_PATH" ]; then
  launchctl asuser "$TARGET_UID" sudo -u "$TARGET_USER" "$EXECUTABLE" --token >/dev/null 2>&1 || true
fi
if [ -f "$CONFIG_PATH" ]; then
  chown "$TARGET_USER":staff "$CONFIG_PATH" 2>/dev/null || true
  chmod 600 "$CONFIG_PATH" 2>/dev/null || true
fi

# 4. (Gen)start i brugerens GUI-session. Det er her EventKit har adgang.
launchctl bootout "gui/${TARGET_UID}/${BUNDLE_ID}" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/${TARGET_UID}" "$AGENT_PLIST" >/dev/null 2>&1; then
  :
else
  launchctl load -w "$AGENT_PLIST" >/dev/null 2>&1 || true
fi
if ! launchctl kickstart -k "gui/${TARGET_UID}/${BUNDLE_ID}" >/dev/null 2>&1; then
  # kickstart kan fejle hvis launchd endnu ikke har registreret agenten.
  # Uden en genstart ville den gamle binære og det gamle token blive liggende,
  # selv om filerne på disken er opdateret.
  launchctl bootout "gui/${TARGET_UID}/${BUNDLE_ID}" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/${TARGET_UID}" "$AGENT_PLIST" >/dev/null 2>&1; then
    echo "    genstartede agenten (kickstart fejlede)"
  else
    launchctl load -w "$AGENT_PLIST" >/dev/null 2>&1 || true
  fi
fi

sleep 2

TOKEN="$(launchctl asuser "$TARGET_UID" sudo -u "$TARGET_USER" "$EXECUTABLE" --token 2>/dev/null || true)"
RUNNING="$(launchctl print "gui/${TARGET_UID}/${BUNDLE_ID}" >/dev/null 2>&1 && echo ja || echo nej)"
INSTALLED_VERSION="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  "${APP}/Contents/Info.plist" 2>/dev/null || echo ukendt)"

echo
echo "  Bruger:        $TARGET_USER"
echo "  Agent:         $AGENT_PLIST"
echo "  Kører:         $RUNNING"
echo "  Installeret:   $INSTALLED_VERSION"
echo "  Config/token:  $CONFIG_PATH"
if [ -n "$TOKEN" ]; then
  echo "  Token:         $TOKEN"
  echo
  echo "  Dette token skal indsættes i Family Dashboard under"
  echo "  Indstillinger > Påmindelser > Mac mini-adresse."
  echo
  echo "  Hvis brugeren er logget ind via fjernstyring eller SSH uden grafisk"
  echo "  session, kan EventKit ikke læse databaserne. Brugeren skal være"
  echo "  logget ind med en aktiv grafisk session."
fi
echo "  Godkend adgang under Systemindstillinger > Anonymitet og sikkerhed:"
echo "    Påmindelser   -> $NAME skal være slået til"
echo "    Automatisering -> $NAME -> Notes skal være slået til"
exit 0
