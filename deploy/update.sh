#!/bin/sh
# Butler bijwerken zodra er iets nieuws op GitHub (main) staat; anders gebeurt er niets.
# Voor cron, bv. elk kwartier (/etc/cron.d/butler-update):
#   */15 * * * * root /opt/butler/deploy/update.sh >> /var/log/butler-update.log 2>&1
set -eu
cd "$(dirname "$0")/.."
exec 9>/tmp/butler-update.lock
flock -n 9 || exit 0  # vorige update loopt nog

git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0

echo "$(date -Is) bijwerken $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/main)"
git merge -q --ff-only origin/main
# dezelfde opzet als nu draait: met of zonder Tailscale-container
if docker ps --format '{{.Names}}' | grep -qx butler-tailscale; then f=docker-compose.tailscale.yml; else f=docker-compose.yml; fi
docker compose -f "$f" up -d --build
docker image prune -f >/dev/null
echo "$(date -Is) klaar"
