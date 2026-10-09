#!/bin/sh
# Move the links prospects see -- evidence pages -- to a new host, safely.
#
#   sh /opt/coldops/deploy/switch-link-host.sh coldops.arslanvuzmallone.com
#
# Order is the point. Every email ever sent carries a link, so the switch must
# never leave a moment where the new links do not open:
#
#   1. the host must already resolve to this server (the DNS A record)
#   2. enable-tracking-hosts.sh gets it a certificate and an nginx block that
#      serves /e/ (evidence pages) and /o/ (the open pixel)
#   3. a real https request to the new host must answer -- checked here
#   4. only then does COLDOPS_EVIDENCE_BASE_URL change, and the services that
#      write links are restarted
#
# The old host keeps serving, so every link already sent keeps working.
# Safe to re-run; stops with the reason at the first step that is not ready.

set -eu

HOST=${1:-}
[ -n "$HOST" ] || { echo "usage: $0 <host>"; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
set_env() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}

echo "== 1/4 DNS"
me=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
seen=$(getent hosts "$HOST" | awk '{print $1}' | head -1 || true)
[ -n "$seen" ] || die "$HOST does not resolve yet. Add an A record: $HOST -> ${me:-the IP of this server}"
[ -z "$me" ] || [ "$seen" = "$me" ] || die "$HOST points at $seen, not this server ($me)"
echo "$HOST -> $seen"

echo "== 2/4 certificate + nginx"
current=$(sed -n 's/^COLDOPS_TRACKING_HOSTS=//p' "$ENV_FILE" | head -1 | tr -d '\r"')
case " $(echo "$current" | tr ',' ' ') " in
    *" $HOST "*) ;;
    *) set_env COLDOPS_TRACKING_HOSTS "${current:+$current,}$HOST" ;;
esac
sh "$ROOT/deploy/tls/enable-tracking-hosts.sh" || die "enable-tracking-hosts.sh did not finish (see above)"

echo "== 3/4 check it answers"
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/e/not-a-real-token" || true)
case "$code" in
    404|400|410) echo "https://$HOST/e/ is served by ColdOps (HTTP $code for a fake token)" ;;
    *) die "https://$HOST/e/ answered HTTP $code; not switching" ;;
esac

echo "== 4/4 switch new links to https://$HOST"
cp "$ENV_FILE" "$ENV_FILE.bak-linkhost-$STAMP"
set_env COLDOPS_EVIDENCE_BASE_URL "https://$HOST"
"$ROOT/deploy/compose.sh" up -d api temporal-worker outbox-worker
sleep 20
docker exec deploy-api-1 python -c "from coldops.config import get_settings; print('evidence links now use', get_settings().evidence_base_url)"
echo "Done. Old links on the previous host keep working."
