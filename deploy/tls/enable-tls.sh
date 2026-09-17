#!/bin/sh
# Obtain a certificate and turn on TLS, or explain why it cannot yet.
#
# Safe to run repeatedly, and designed to be run by a timer rather than a
# person: every reason it might not work today is a *retry*, not a failure, so
# the box finishes the job itself whenever DNS finally points here.
#
# It refuses to ask Let's Encrypt anything until it has proved, locally, that
# the challenge would succeed. Failed validations are rate limited (5 per
# hostname per hour), and a timer that burns them on a domain that does not
# resolve yet would lock out the attempt that would have worked.
#
# Exit codes:
#   0   TLS is on (either just now, or it already was)
#   75  not yet -- preconditions unmet, try again later. The timer keeps going.
#   1   something is actually wrong and retrying will not fix it.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY=$(dirname "$HERE")
ROOT=$(dirname "$DEPLOY")
ENV_FILE="$ROOT/.env"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
later() { log "not yet: $*"; exit 75; }

[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"

# Only these two, and only from our own .env -- never `set -a; . .env`, which
# would execute whatever a password happens to contain.
DOMAIN=$(sed -n 's/^TITAN_TLS_DOMAIN=//p' "$ENV_FILE" | head -1 | tr -d '\r"')
EMAIL=$(sed -n 's/^TITAN_TLS_EMAIL=//p' "$ENV_FILE" | head -1 | tr -d '\r"')
[ -n "$DOMAIN" ] || die "set TITAN_TLS_DOMAIN in $ENV_FILE"
[ -n "$EMAIL" ] || die "set TITAN_TLS_EMAIL in $ENV_FILE (Let's Encrypt expiry warnings go there)"

LIVE="/etc/letsencrypt/live/$DOMAIN"
INSTALLED="$DEPLOY/nginx/tls/443.conf"
TEMPLATE="$HERE/443.conf.template"
ACME="$DEPLOY/nginx/acme"
COMPOSE="$DEPLOY/compose.sh"

# ---------------------------------------------------------------- already on?

if [ -f "$INSTALLED" ] && [ -s "$LIVE/fullchain.pem" ]; then
    log "TLS is already on for $DOMAIN; nothing to do"
    exit 0
fi

# ------------------------------------------------------------- preconditions

command -v certbot >/dev/null 2>&1 || die "certbot is not installed (apt-get install -y certbot)"

MY_IP=$(curl -fsS --max-time 15 https://api.ipify.org 2>/dev/null || true)
[ -n "$MY_IP" ] || later "cannot determine this host's public IP"

RESOLVED=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
[ -n "$RESOLVED" ] || later "$DOMAIN does not resolve yet (add an A record -> $MY_IP)"
[ "$RESOLVED" = "$MY_IP" ] || later "$DOMAIN resolves to $RESOLVED, not $MY_IP"

# Prove the challenge path works before spending an attempt on it. This is the
# check that makes the timer safe to run every few minutes.
mkdir -p "$ACME/.well-known/acme-challenge"
TOKEN="titan-selftest-$$"
printf 'ok' > "$ACME/.well-known/acme-challenge/$TOKEN"
GOT=$(curl -fsS --max-time 20 "http://$DOMAIN/.well-known/acme-challenge/$TOKEN" 2>/dev/null || true)
rm -f "$ACME/.well-known/acme-challenge/$TOKEN"
[ "$GOT" = "ok" ] || later "the ACME path is not reachable over http://$DOMAIN yet"

log "preconditions met: $DOMAIN -> $MY_IP, challenge path serving"

# ------------------------------------------------------------------- issuance

if [ ! -s "$LIVE/fullchain.pem" ]; then
    log "asking Let's Encrypt for $DOMAIN"
    certbot certonly \
        --webroot -w "$ACME" \
        -d "$DOMAIN" \
        --non-interactive --agree-tos -m "$EMAIL" \
        --keep-until-expiring \
        || later "certbot did not succeed; leaving everything as it was"
fi
[ -s "$LIVE/fullchain.pem" ] || later "certbot reported success but there is no certificate"

log "certificate in hand: $(openssl x509 -enddate -noout -in "$LIVE/fullchain.pem" 2>/dev/null || echo '?')"

# --------------------------------------------- the bundle has to move with us

# NEXT_PUBLIC_API_URL is compiled into the browser bundle, so the image built
# against http://<ip> keeps calling http://<ip> from an https:// page -- which
# the browser blocks as mixed content. The CRM would load and then fail every
# request, which looks like the API being down. Nothing about turning on TLS
# fixes this by itself, so it happens here, before the listener exists.

cp "$ENV_FILE" "$ENV_FILE.pre-tls"

set_env() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}

set_env TITAN_PUBLIC_ORIGIN "https://$DOMAIN"
# CORS. The bare IP stays allowed on purpose: it is the way back in when DNS or
# the certificate breaks, and a diagnostic page that renders but cannot call the
# API is not much of a way back in.
set_env TITAN_FRONTEND_URL "https://$DOMAIN"
# A JSON array, not a bare URL. `extra_cors_origins` is `list[str]`, and
# pydantic-settings parses a complex type from the environment as JSON -- a
# bare value raises SettingsError, which is raised while *settings load*, so
# every service that reads config dies at startup rather than at the point of
# use. That is what took the stack down on the first run of this script: the
# migrate one-shot exited 1, api could not start behind it, and the failure
# looked like TLS because TLS was what had just changed.
set_env TITAN_EXTRA_CORS_ORIGINS "[\"http://$MY_IP\"]"

log "rebuilding the CRM bundle against https://$DOMAIN"
build_web() {
    (cd "$ROOT" && docker build -q -f apps/web/Dockerfile \
        --build-arg "NEXT_PUBLIC_API_URL=$1" \
        -t titan-web:local . >/dev/null 2>&1)
}

if ! build_web "https://$DOMAIN"; then
    cp "$ENV_FILE.pre-tls" "$ENV_FILE"
    later "the CRM image would not build; nothing changed"
fi

# ------------------------------------------------------- turn the listener on

sed "s/__DOMAIN__/$DOMAIN/g" "$TEMPLATE" > "$INSTALLED"

rollback() {
    log "ROLLING BACK to plain HTTP"
    rm -f "$INSTALLED"
    cp "$ENV_FILE.pre-tls" "$ENV_FILE"
    build_web "http://$MY_IP" || true
    "$COMPOSE" up -d web api nginx >/dev/null 2>&1 || true
    log "ingress restored on port 80"
}

# `up -d` rather than a reload: the 443 port publish is a container property,
# so nginx has to be recreated for it to exist at all. `web` takes the rebuilt
# bundle and `api` takes the new CORS origins.
# Output kept, not discarded. The first failure of this script reported only
# "the stack would not come up", while compose had said exactly what was wrong
# -- `service "migrate" didn't complete successfully: exit 1` -- and swallowing
# it turned a one-line diagnosis into an investigation.
if ! "$COMPOSE" up -d web api nginx 2>&1 | tee /tmp/titan-tls-up.log; then
    log "compose refused to bring the stack up; its output:"
    sed 's/^/    /' /tmp/titan-tls-up.log | tail -15
    rollback
    die "the stack would not come up with TLS"
fi
if grep -q "didn't complete successfully\|Error response from daemon" /tmp/titan-tls-up.log; then
    log "compose reported a failure while bringing the stack up:"
    sed 's/^/    /' /tmp/titan-tls-up.log | tail -15
    rollback
    die "the stack would not come up with TLS"
fi

sleep 5
if ! docker exec deploy-nginx-1 nginx -t >/dev/null 2>&1; then
    rollback
    die "nginx rejected the TLS configuration"
fi

# The real test: a full TLS handshake and a real page, from outside the daemon.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "https://$DOMAIN/crm" 2>/dev/null || echo 000)
if [ "$CODE" != "200" ] && [ "$CODE" != "307" ]; then
    rollback
    die "https://$DOMAIN/crm answered $CODE; TLS not enabled"
fi

# The escape hatch must still work, or there is no way back in.
IP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://$MY_IP/crm" 2>/dev/null || echo 000)
[ "$IP_CODE" = "200" ] || log "WARNING: http://$MY_IP/crm answered $IP_CODE; the plain-HTTP fallback is not serving"

log "TLS is on: https://$DOMAIN/crm -> $CODE (fallback http://$MY_IP/crm -> $IP_CODE)"

# ------------------------------------------------------------------- renewal

# certbot's own timer does the renewing. All that is missing is telling nginx,
# which runs in a container certbot knows nothing about. A deploy hook fires
# only when a certificate actually changed.
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/titan-nginx.sh <<'HOOK'
#!/bin/sh
# Renewal rewrites the files under /etc/letsencrypt, which nginx has already
# opened. Without this it would serve the expired certificate until something
# restarted it -- roughly 60 days of looking fine followed by a hard outage.
set -eu
docker exec deploy-nginx-1 nginx -s reload 2>/dev/null || true
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/titan-nginx.sh

systemctl enable --now certbot.timer >/dev/null 2>&1 || true

# This script's own timer has done its job and should stop waking up.
systemctl disable --now titan-tls-bootstrap.timer >/dev/null 2>&1 || true

log "renewal armed (certbot.timer + nginx reload hook); bootstrap timer stopped"
exit 0
