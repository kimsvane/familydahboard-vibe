#!/usr/bin/env bash
# Bygger og sender Family Dashboard-image med build-id = git commit.
# Kør på maskinen med docker (TrueNAS-shell eller Mac med Docker/Colima):
#   ./build-push.sh
#
# Uden et fuldt git-klon (f.eks. en ren kopi på TrueNAS) kan SHA og IMAGE
# sættes udefra, så bygget stadig kan identificeres:
#   SHA=255dbb9 IMAGE=ghcr.io/kimsvane/familydahboard-vibe:latest ./build-push.sh
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-ghcr.io/kimsvane/familydahboard-vibe:latest}"
VERSION="$(tr -d '[:space:]' < VERSION)"

if [ -n "${SHA:-}" ]; then
  echo ">>> Bruger SHA fra miljøet: $SHA"
elif git rev-parse --short HEAD >/dev/null 2>&1; then
  SHA="$(git rev-parse --short HEAD)"
else
  echo "Fejl: ikke et git-klon, og SHA er ikke sat." >&2
  echo "Sæt den sådan:  SHA=<commit> $0" >&2
  exit 1
fi

BUILD_ID="${SHA}-$(date -u +%Y%m%d%H%M)"

echo ">>> Bygger $IMAGE version $VERSION (build $BUILD_ID)"

docker build \
  --pull \
  --build-arg FAMILY_DASHBOARD_BUILD="$BUILD_ID" \
  --build-arg FAMILY_DASHBOARD_VERSION="$VERSION" \
  -t "$IMAGE" \
  -t "${IMAGE%:*}:${VERSION}" \
  .

docker push "$IMAGE"
docker push "${IMAGE%:*}:${VERSION}"

echo
echo ">>> Færdig: $IMAGE version $VERSION build $BUILD_ID"
echo ">>> Genstart containeren, og verificér i browserens bundstribe at build viser $BUILD_ID"
echo ">>> Build-id er også synlig via: docker inspect --format '{{index .Config.Labels \"org.opencontainers.image.version\"}}' $IMAGE"
