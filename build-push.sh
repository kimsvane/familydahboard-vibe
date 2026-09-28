#!/usr/bin/env bash
# Bygger og sender Family Dashboard-image med build-id = git commit.
# Kør på maskinen med docker (TrueNAS-shell eller Mac med Colima/Docker Desktop):
#   ./build-push.sh
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-ghcr.io/kimsvane/familydahboard-vibe:latest}"
SHA="$(git rev-parse --short HEAD)"
BUILD_ID="${SHA}-$(date -u +%Y%m%d%H%M)"
VERSION="$(tr -d '[:space:]' < VERSION)"

echo ">>> Bygger $IMAGE version $VERSION (build $BUILD_ID)"

docker build \
  --pull \
  --build-arg FAMILY_DASHBOARD_BUILD="$BUILD_ID" \
  --build-arg FAMILY_DASHBOARD_VERSION="$VERSION" \
  -t "$IMAGE" \
  -t "${IMAGE%:*}:${VERSION}" \
  .

docker push "$IMAGE"

echo
echo ">>> Færdig: $IMAGE version $VERSION build $BUILD_ID"
echo ">>> Genstart containeren, og verificér i browserens bundstribe at build viser $BUILD_ID"
