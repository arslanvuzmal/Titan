#!/bin/sh
# Give each cold-sending domain its tracking host (go.<domain>), with TLS.
#
# Reads TITAN_TRACKING_HOSTS from the repo .env -- a comma- or space-separated
# list such as "go.vuzmalstudio.com,go.workwitharslan.com" -- and, for each
# host not already serving, proves locally that Let's Encrypt's challenge would
# succeed, asks for a certificate, installs the server block, and checks the
# pixel answers over https. Safe to re-run; a host that is not ready yet is
# skipped with the reason, never half-installed.
#
# TITAN_TRACKING_HOME is where every other path on these hosts redirects
# (default arslanvuzmallone.com).
#
# Exit codes: 0 every host is serving; 75 at least one is not ready yet;
# 1 something is wrong that retrying will not fix.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY=$(dirname "$HERE")
ROOT=$(dirname "$DEPLOY")
ENV_FILE="$ROOT/.env"
TEMPLATE="$HERE/tracking-host.conf.template"
ACME="$DEPLOY/nginx/acme"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
command -v certbot >/dev/null 2>&1 || die "certbot is not installed"

# Read three values, and only these, from our own .env -- never source it.
read_env() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d '\r"'; }
HOSTS=$(read_env TITAN_TRACKING_HOSTS | tr ',' ' ')
HOME_SITE=$(read_env TITAN_TRACKING_HOME)
EMAIL=$(read_env TITAN_TLS_EMAIL)
[ -n "$HOSTS" ] || die "set TITAN_TRACKING_HOSTS in $ENV_FILE"
[ -n "$EMAIL" ] || die "set TITAN_TLS_EMAIL in $ENV_FILE"
[ -n "$HOME_SITE" ] || HOME_SITE=arslanvuzmallone.com

[ -f "$DEPLOY/nginx/tls/443.conf" ] || die \
    "the main TLS listener is not on yet; run enable-tls.sh first (443 is only published with it)"

MY_IP=$(curl -fsS --max-time 15 https://api.ipify.org 2>/dev/null || true)
[ -n "$MY_IP" ] || { log "not yet: cannot determine this host's public IP"; exit 75; }

pending=0
for HOST in $HOSTS; do
    INSTALLED="$DEPLOY/nginx/tls/track-$HOST.conf"
    LIVE="/etc/letsencrypt/live/$HOST"

    if [ -f "$INSTALLED" ] && [ -s "$LIVE/fullchain.pem" ]; then
        log "$HOST: already serving"
        continue
    fi

    RESOLVED=$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1; exit}' || true)
    if [ "$RESOLVED" != "$MY_IP" ]; then
        log "$HOST: not yet -- resolves to '${RESOLVED:-nothing}', needs an A record -> $MY_IP"
        pending=1; continue
    fi

    # The :80 default server answers the challenge path for any name, so this
    # works before the host has a server block of its own.
    mkdir -p "$ACME/.well-known/acme-challenge"
    TOKEN="titan-selftest-$$"
    printf 'ok' > "$ACME/.well-known/acme-challenge/$TOKEN"
    GOT=$(curl -fsS --max-time 20 "http://$HOST/.well-known/acme-challenge/$TOKEN" 2>/dev/null || true)
    rm -f "$ACME/.well-known/acme-challenge/$TOKEN"
    if [ "$GOT" != "ok" ]; then
        log "$HOST: not yet -- the ACME path is not reachable over http"
        pending=1; continue
    fi

    if [ ! -s "$LIVE/fullchain.pem" ]; then
        log "$HOST: asking Let's Encrypt"
        if ! certbot certonly --webroot -w "$ACME" -d "$HOST" \
                --non-interactive --agree-tos -m "$EMAIL" --keep-until-expiring; then
            log "$HOST: certbot did not succeed; nothing installed"
            pending=1; continue
        fi
    fi

    sed -e "s/__HOST__/$HOST/g" -e "s/__HOME__/$HOME_SITE/g" "$TEMPLATE" > "$INSTALLED"
    if ! docker exec deploy-nginx-1 nginx -t >/dev/null 2>&1; then
        rm -f "$INSTALLED"
        die "$HOST: nginx rejected the server block; removed it, ingress untouched"
    fi
    docker exec deploy-nginx-1 nginx -s reload

    # The pixel is served for any token, signed or not, so a made-up one is a
    # fair test of the whole path without recording anything.
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$HOST/o/selftest.gif" || echo 000)
    if [ "$CODE" != "200" ]; then
        rm -f "$INSTALLED"
        docker exec deploy-nginx-1 nginx -s reload || true
        log "$HOST: https pixel answered $CODE; removed the server block"
        pending=1; continue
    fi
    log "$HOST: serving -- https://$HOST/o/ answers 200"
done

[ "$pending" -eq 0 ] || exit 75
log "every tracking host is serving"
