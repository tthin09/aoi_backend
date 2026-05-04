#!/usr/bin/env bash
set -euo pipefail

# Deploy script for self-hosted runner.
# - Assumes repository is already checked out by the runner
# - Uses docker compose in backend/ to build and run the service

ROOT_DIR="$HOME/Code/aoi-backend"
cd "$ROOT_DIR"

echo "[deploy] Running in $ROOT_DIR"

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  COMPOSE_CMD="docker compose"
fi

echo "[deploy] Using compose command: $COMPOSE_CMD"

echo "[deploy] Pulling images (if any)..."
$COMPOSE_CMD pull --ignore-pull-failures || true

echo "[deploy] Building images..."
$COMPOSE_CMD build --pull --no-cache || true

echo "[deploy] Bringing up containers..."
$COMPOSE_CMD up -d --remove-orphans

echo "[deploy] Cleaning up unused images..."
docker image prune -f || true

echo "[deploy] Deployment finished. Containers status:"
docker ps --filter "name=aoi_backend" || docker ps

exit 0
