#!/usr/bin/env bash
# Air-gap transfer: save all images of the stack into one archive (dist/oee-images.tar).
# On the target host: `make import-images` (podman load) and `make up`.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE:-podman compose}"
ENGINE="${ENGINE:-podman}"
mkdir -p dist

$COMPOSE_CMD build
mapfile -t images < <($COMPOSE_CMD config --images | sort -u)
for img in "${images[@]}"; do
  $ENGINE image exists "$img" 2>/dev/null || $ENGINE pull "$img"
done

echo "exporting: ${images[*]}"
$ENGINE save --multi-image-archive -o dist/oee-images.tar "${images[@]}"
sha256sum dist/oee-images.tar > dist/oee-images.tar.sha256
echo "written dist/oee-images.tar (+ .sha256)"
