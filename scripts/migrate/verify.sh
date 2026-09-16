#!/usr/bin/env bash
# Prove the new host is actually working, before anything is switched off here.
#
# Every check is something that has failed silently at least once on this
# project. It reports and exits non-zero on a real problem rather than printing
# a wall of green -- the point is to be trusted when it says nothing is wrong.
#
#   bash scripts/migrate/verify.sh

set -uo pipefail

INSTALL_DIR="${TITAN_INSTALL_DIR:-/opt/titan}"
cd "${INSTALL_DIR}" 2>/dev/null || { echo "no ${INSTALL_DIR}"; exit 1; }
COMPOSE="docker compose -f deploy/docker-compose.prod.yml"

PASS=0
FAIL=0
ok()   { printf '  PASS  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '        %s\n' "$*"; }

psql_q() { ${COMPOSE} exec -T postgres psql -U titan -d titan -t -A -c "$1" 2>/dev/null | tr -d '\r'; }

echo "Titan post-migration verification"
echo

# ---- containers ------------------------------------------------------------
echo "containers"
EXPECTED="api outbox-worker inbound-worker temporal-worker browser-worker postgres redis temporal"
for svc in ${EXPECTED}; do
  state="$(${COMPOSE} ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="${svc}" '$1==s {print $2}')"
  if [ "${state}" = "running" ]; then ok "${svc}"; else bad "${svc} is '${state:-absent}'"; fi
done

# inbound-worker was missing from this compose file until 16 September. Without
# it no reply is collected and no bounce is ingested, so the estate looks
# healthy while losing both halves of its feedback loop.
echo
echo "the feedback loop"
if ${COMPOSE} ps --format '{{.Service}}' 2>/dev/null | grep -q '^inbound-worker$'; then
  ok "inbound-worker is defined and running"
else
  bad "inbound-worker missing -- no replies, no bounce learning"
fi

# ---- the data actually arrived ---------------------------------------------
echo
echo "database"
for pair in "leads:1000" "organizations:1000" "contact_channels:500" "outbox_messages:100"; do
  table="${pair%%:*}"; floor="${pair##*:}"
  n="$(psql_q "select count(*) from ${table}")"
  if [ -n "${n}" ] && [ "${n}" -ge "${floor}" ]; then ok "${table}: ${n}"
  else bad "${table}: ${n:-unreadable} (expected at least ${floor})"; fi
done

# The memory that makes verification work. An empty suppression table after a
# restore means the address history layer starts blind.
n="$(psql_q "select count(*) from suppression_entries")"
if [ -n "${n}" ] && [ "${n}" -gt 0 ]; then ok "suppression_entries: ${n}"
else bad "suppression_entries is empty -- bounce memory did not come across"; fi

# The follow-up sequence only advances if drafts carry their step.
n="$(psql_q "select count(*) from message_drafts where sequence_step_id is not null")"
if [ -n "${n}" ] && [ "${n}" -gt 0 ]; then ok "drafts carrying a sequence step: ${n}"
else bad "no draft carries a sequence step -- follow-ups cannot advance"; fi

# ---- secrets ---------------------------------------------------------------
echo
echo "credentials"
MAILBOX_PATH="$(${COMPOSE} exec -T outbox-worker sh -c 'echo "$TITAN_MAILBOX_FILE"' 2>/dev/null | tr -d '\r')"
if [ -z "${MAILBOX_PATH}" ]; then
  note "TITAN_MAILBOX_FILE unset; single-mailbox mode via TITAN_SMTP_*"
elif ${COMPOSE} exec -T outbox-worker sh -c "[ -r '${MAILBOX_PATH}' ]" 2>/dev/null; then
  ok "mailbox file readable at ${MAILBOX_PATH}"
else
  bad "TITAN_MAILBOX_FILE=${MAILBOX_PATH} is not readable -- every send will fail to authenticate"
fi

# ---- schedules -------------------------------------------------------------
echo
echo "schedules"
SCHED="$(${COMPOSE} exec -T temporal sh -c 'temporal schedule list --address temporal:7233 --namespace default 2>/dev/null' | grep -c 'titan-' || echo 0)"
if [ "${SCHED}" -ge 4 ]; then ok "${SCHED} schedules installed"
else bad "only ${SCHED} schedules -- run: ${COMPOSE} exec api python -m titan.cli schedules"; fi

# ---- preflight -------------------------------------------------------------
echo
echo "preflight"
if ${COMPOSE} exec -T api python -m titan.cli preflight >/tmp/titan-preflight.txt 2>&1; then
  ok "preflight clean"
else
  # Non-zero is normal while campaigns are paused or sending is not authorised,
  # so this reports rather than failing the run.
  note "preflight exited non-zero (expected while paused):"
  tail -6 /tmp/titan-preflight.txt | sed 's/^/        /'
fi

# ---- the thing the VPS was bought for --------------------------------------
echo
echo "reverse DNS"
IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '')"
if [ -z "${IP}" ]; then
  note "could not determine the public IP"
else
  PTR="$(getent hosts "${IP}" 2>/dev/null | awk '{print $2}')"
  if [ -n "${PTR}" ]; then ok "PTR for ${IP} is ${PTR}"
  else bad "no PTR record for ${IP} -- SMTP probes will still be judged on our IP, not theirs"; fi
fi

echo
echo "-------------------------------------------"
printf '  %s passed, %s failed\n' "${PASS}" "${FAIL}"
if [ "${FAIL}" -gt 0 ]; then
  echo
  echo "  Do NOT stop the laptop yet. Fix the failures above first."
  exit 1
fi
echo
echo "  Safe to proceed: stop the old host, send one test message, then unpause."
