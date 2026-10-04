#!/usr/bin/env bash
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

for f in \
  scripts/install.sh \
  scripts/uninstall.sh \
  systemd/family-dashboard-voice.service.in \
  etc/family-dashboard-voice.env \
  family-dashboard-voice.spec; do
  [ -f "$f" ] || fail "manglende fil: $f"
done

bash -n scripts/install.sh || fail "bash-syntaksfejl i scripts/install.sh"
bash -n scripts/uninstall.sh || fail "bash-syntaksfejl i scripts/uninstall.sh"

grep -Eq '^CLIENT_NAME=' etc/family-dashboard-voice.env || fail "CLIENT_NAME mangler i etc/family-dashboard-voice.env"
grep -Eq '^HOST=' etc/family-dashboard-voice.env || fail "HOST mangler i etc/family-dashboard-voice.env"
grep -Eq '^PORT=6053$' etc/family-dashboard-voice.env || fail "PORT=6053 forventet i etc/family-dashboard-voice.env"
grep -Eq '^AUDIO_INPUT_CHANNELS=1$' etc/family-dashboard-voice.env || fail "AUDIO_INPUT_CHANNELS mangler i etc/family-dashboard-voice.env"

grep -Eq '^Name:           family-dashboard-voice$' family-dashboard-voice.spec || fail "Name mangler i spec"
grep -Eq '^BuildArch:      noarch$' family-dashboard-voice.spec || fail "BuildArch noarch forventet i spec"
grep -Eq '^%post$' family-dashboard-voice.spec || fail "%post mangler i spec"
grep -Eq '^%global app_prefix /opt/family-dashboard-voice' family-dashboard-voice.spec || fail "app_prefix mangler i spec"

LVA_VERSION="$(sed -n 's/^LVA_VERSION="\(v[^"]*\)".*/\1/p' scripts/install.sh)"
[ -n "$LVA_VERSION" ] || fail "LVA_VERSION ikke fundet i scripts/install.sh"
printf 'linux-voice-assistant pin: %s\n' "$LVA_VERSION"

if [ "${LVA_NETWORK_CHECK:-1}" = "1" ]; then
  url="https://github.com/OHF-Voice/linux-voice-assistant/archive/refs/tags/${LVA_VERSION}.tar.gz"
  code="$(curl -sL -o /dev/null -w '%{http_code}' --max-time 30 "$url" || true)"
  [ "$code" = "200" ] || fail "LVA-$LVA_VERSION-tarballen svarde ikke 200 (fandt $code); pinnen i install.sh er sandsynligvis forældet"
fi

echo "OK: clients/voice tjek bestået"