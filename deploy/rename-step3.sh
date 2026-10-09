#!/bin/sh
# One-time: finish renaming the production host from Titan to ColdOps.
#
#   sh /opt/titan/deploy/rename-step3.sh
#
# Run as root, from anywhere. Stops at the first problem and says which step.
# What it does, in order:
#
#   1. pulls the code (the compose file now reads COLDOPS_* names)
#   2. builds the three images under their new tags, BEFORE changing or
#      stopping anything -- a failed build leaves the server exactly as it was
#   3. backs up .env, then renames every TITAN_* key to COLDOPS_* and the
#      image tags titan-*:local to coldops-*:local -- values are not touched
#   4. stops the stack (containers only -- volumes, and so all data, stay)
#   5. moves /opt/titan to /opt/coldops, and points certbot's renewal webroot
#      at the new path (otherwise the certificate silently stops renewing)
#   6. removes the old, disabled titan-tls-bootstrap systemd unit
#   7. starts the stack from /opt/coldops and waits for it to be healthy
#   8. installs the coldops-* schedules and retires each titan-* twin
#
# Deliberately NOT renamed (never seen outside the server, and renaming them
# risks live data or in-flight work): the database name and user, the Docker
# volumes, the titan-research / titan-maintenance job queues, the "titan"
# workspace slug, and the titan.arslanvuzmallone.com host the CRM runs on.
#
# The new link host is a separate step, because it needs a DNS record first:
# deploy/switch-link-host.sh.

set -eu

OLD=/opt/titan
NEW=/opt/coldops
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

log() { printf '\n== %s\n' "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -d "$OLD/.git" ] || die "$OLD is not the repository (already moved? then this script is done)"
[ ! -e "$NEW" ] || die "$NEW already exists; refusing to overwrite it"

log "1/8 pull"
cd "$OLD"
git pull --ff-only
git log --oneline -1

log "2/8 build images first (the stack keeps running; nothing is renamed if this fails)"
docker build -q -f apps/api/Dockerfile -t coldops-api:local .
docker build -q -f apps/browser-worker/Dockerfile -t coldops-browser-worker:local apps/browser-worker
# Either name: on the first run .env still says TITAN_, on a rerun COLDOPS_.
origin=$(sed -n -e 's/^COLDOPS_PUBLIC_ORIGIN=//p' -e 's/^TITAN_PUBLIC_ORIGIN=//p' .env | head -1 | tr -d '\r"')
[ -n "$origin" ] || die "PUBLIC_ORIGIN is empty in .env; the web build needs it"
docker build -q -f apps/web/Dockerfile --build-arg NEXT_PUBLIC_API_URL="$origin" -t coldops-web:local .

log "3/8 settings: TITAN_* -> COLDOPS_* (backup: .env.bak-rename-$STAMP)"
cp .env ".env.bak-rename-$STAMP"
sed -i \
    -e 's/^TITAN_/COLDOPS_/' \
    -e 's/^\(COLDOPS_[A-Z_]*_IMAGE=\)titan-\(api\|web\|browser-worker\):local$/\1coldops-\2:local/' \
    .env
left=$(grep -c '^TITAN_' .env || true)
[ "$left" = 0 ] || die "$left TITAN_ keys still in .env"
grep -E '^COLDOPS_(API|WEB|BROWSER_WORKER)_IMAGE=' .env

log "4/8 stop the stack (containers only; volumes and data stay)"
deploy/compose.sh down --remove-orphans
docker network rm deploy_titan_network 2>/dev/null || true

log "5/8 move $OLD -> $NEW, and repoint certificate renewal"
cd /
mv "$OLD" "$NEW"
for conf in /etc/letsencrypt/renewal/*.conf; do
    [ -f "$conf" ] || continue
    cp "$conf" "$conf.bak-rename-$STAMP"
    sed -i "s#$OLD/#$NEW/#g" "$conf"
done
grep -h 'webroot' /etc/letsencrypt/renewal/*.conf 2>/dev/null || true

log "6/8 remove the old (disabled) titan-tls-bootstrap unit"
systemctl disable --now titan-tls-bootstrap.timer 2>/dev/null || true
rm -f /etc/systemd/system/titan-tls-bootstrap.service /etc/systemd/system/titan-tls-bootstrap.timer
systemctl daemon-reload

log "7/8 start from $NEW"
cd "$NEW"
deploy/compose.sh up -d
for i in $(seq 1 30); do
    unhealthy=$(docker ps --format '{{.Names}} {{.Status}}' | grep -cE 'starting|unhealthy' || true)
    [ "$unhealthy" = 0 ] && break
    sleep 5
done
docker ps --format '{{.Names}} {{.Status}}'
docker logs deploy-migrate-1 2>&1 | tail -1

log "8/8 schedules: install coldops-*, retire titan-* twins"
docker exec deploy-api-1 python -m coldops.cli schedules --workspace titan --retire-legacy

log "done"
echo "Repository is now at $NEW. Old .env saved as $NEW/.env.bak-rename-$STAMP."
echo "Next: add a DNS A record for the new link host, then run"
echo "  sh $NEW/deploy/switch-link-host.sh coldops.arslanvuzmallone.com"
