#!/bin/sh
# Serve the CRM and API from a new host, keeping the old host working.
#
#   sh /opt/coldops/deploy/move-crm-host.sh coldops.arslanvuzmallone.com
#
# The new host must already have a certificate (switch-link-host.sh gave
# coldops.arslanvuzmallone.com one). In order, stopping at the first problem:
#
#   1. renders the full site block (CRM, API, evidence links) for the new
#      host from the same template as the main one, replacing its smaller
#      link-only block; nginx must accept it or nothing changes
#   2. checks https://<host>/crm answers before anything else moves
#   3. on the old host, forwards / and /crm to the new host; its /api, /e/ and
#      /o/ keep answering, so every link already sent keeps working
#   4. points COLDOPS_PUBLIC_ORIGIN and COLDOPS_FRONTEND_URL at the new host,
#      keeps the old origin allowed by CORS, rebuilds the web app against the
#      new API address and restarts api + web
#
# Backups of every file it changes are kept next to it.

set -eu

NEW=${1:-}
[ -n "$NEW" ] || { echo "usage: $0 <new host>"; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"
TLS="$ROOT/deploy/nginx/tls"
TEMPLATE="$ROOT/deploy/tls/443.conf.template"
SITE="$TLS/site-$NEW.conf"
TRACK="$TLS/track-$NEW.conf"
MAIN="$TLS/443.conf"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
read_env() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d '\r"'; }
set_env() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
answers() {
    # $1 url, $2 expected code; retried because an nginx reload is asynchronous
    for _ in 1 2 3 4 5 6 7 8; do
        sleep 2
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" || true)
        [ "$code" = "$2" ] && return 0
    done
    echo "$1 answered $code, expected $2"
    return 1
}

OLD=$(read_env COLDOPS_PUBLIC_ORIGIN | sed 's#^https://##; s#/.*##')
[ -n "$OLD" ] || die "COLDOPS_PUBLIC_ORIGIN is empty"
[ "$OLD" != "$NEW" ] || die "the CRM already lives at $NEW"
[ -s "/etc/letsencrypt/live/$NEW/fullchain.pem" ] || die "no certificate for $NEW yet (run switch-link-host.sh first)"
[ -f "$MAIN" ] || die "no main TLS block at $MAIN"

echo "== 1/4 full site block for $NEW"
sed "s/__DOMAIN__/$NEW/g" "$TEMPLATE" > "$SITE"
[ -f "$TRACK" ] && mv "$TRACK" "$TRACK.bak-$STAMP"
if ! docker exec deploy-nginx-1 nginx -t >/dev/null 2>&1; then
    rm -f "$SITE"
    [ -f "$TRACK.bak-$STAMP" ] && mv "$TRACK.bak-$STAMP" "$TRACK"
    die "nginx rejected the new block; put everything back"
fi
docker exec deploy-nginx-1 nginx -s reload

echo "== 2/4 check https://$NEW answers"
answers "https://$NEW/crm" 200 || die "the CRM does not answer on $NEW; old host untouched"
answers "https://$NEW/e/not-a-real-token" 404 || die "evidence links do not answer on $NEW"

echo "== 3/4 old host $OLD forwards the CRM to $NEW"
cp "$MAIN" "$MAIN.bak-$STAMP"
if ! grep -q "moved to $NEW" "$MAIN"; then
    # Inserted before the catch-all, in the 443 block only (the first one).
    awk -v new="$NEW" '
        !done && /^    location \/ \{/ {
            print "    # The CRM moved to " new "; /api, /e and /o still answer here."
            print "    location = / { return 301 https://" new "/crm; }"
            print "    location ^~ /crm { return 301 https://" new "$request_uri; }"
            done = 1
        }
        { print }
    ' "$MAIN.bak-$STAMP" > "$MAIN"
fi
if ! docker exec deploy-nginx-1 nginx -t >/dev/null 2>&1; then
    cp "$MAIN.bak-$STAMP" "$MAIN"
    die "nginx rejected the forward on $OLD; put it back"
fi
docker exec deploy-nginx-1 nginx -s reload

echo "== 4/4 settings, web app, restart"
cp "$ENV_FILE" "$ENV_FILE.bak-crmhost-$STAMP"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
set_env COLDOPS_PUBLIC_ORIGIN "https://$NEW"
set_env COLDOPS_FRONTEND_URL "https://$NEW"
set_env COLDOPS_EXTRA_CORS_ORIGINS "[\"http://$IP\",\"https://$OLD\"]"
cd "$ROOT"
docker build -q -f apps/web/Dockerfile --build-arg NEXT_PUBLIC_API_URL="https://$NEW" \
    -t coldops-web:local .
deploy/compose.sh up -d api web
answers "https://$NEW/crm" 200 || die "the rebuilt CRM does not answer on $NEW"
answers "https://$OLD/crm" 301 || echo "note: $OLD/crm is not forwarding yet"
echo "Done. The CRM is at https://$NEW/crm; https://$OLD/crm forwards there; old links still work."
