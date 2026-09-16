#!/usr/bin/env bash
# Everything the new host needs, in one file.
#
# Run this on the machine Titan is running on now. It produces a single tarball
# containing the database, the secrets, and a manifest saying exactly what was
# captured and from which commit.
#
# **The tarball contains live mailbox passwords and API keys.** It is written
# outside the repository on purpose, it must never be committed, and it should
# be moved with `scp` and deleted from both ends once the migration is done.
#
# Deliberately not incremental and not clever. A migration backup is used once,
# under mild stress, and the only property that matters is that restoring it
# produces a working system.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${TITAN_BACKUP_DIR:-${TMPDIR:-/tmp}}/titan-migrate-${STAMP}"
DB_CONTAINER="${TITAN_DB_CONTAINER:-titan-postgres-1}"
DB_USER="${TITAN_DB_USER:-titan}"
DB_NAME="${TITAN_DB_NAME:-titan}"

say() { printf '  %s\n' "$*"; }

echo "Titan migration backup"
echo "  repo   : ${REPO_ROOT}"
echo "  staging: ${OUT_DIR}"
echo

mkdir -p "${OUT_DIR}"

# ---- 1. the database -------------------------------------------------------
# Custom format (-Fc) rather than plain SQL: it restores with pg_restore, which
# can run in parallel and does not care that the target was created by a
# different Postgres minor version. --no-owner because the role names on the new
# host will not match and nothing here depends on ownership.
echo "1/4  dumping ${DB_NAME} from ${DB_CONTAINER}"
if ! docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
  echo "     ERROR: ${DB_CONTAINER} is not accepting connections." >&2
  echo "     Start Docker and the stack, then run this again." >&2
  exit 1
fi
docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc --no-owner \
  > "${OUT_DIR}/titan.dump"
say "$(du -h "${OUT_DIR}/titan.dump" | cut -f1) written"

# ---- 2. the secrets --------------------------------------------------------
# The mailbox passwords and the .env. Without these the new host starts, looks
# healthy, and cannot authenticate to send a single message.
echo "2/4  copying secrets and .env"
if [ -d "${REPO_ROOT}/secrets" ]; then
  cp -R "${REPO_ROOT}/secrets" "${OUT_DIR}/secrets"
  say "secrets/ ($(find "${REPO_ROOT}/secrets" -type f | wc -l | tr -d ' ') files)"
else
  echo "     WARNING: no secrets/ directory found" >&2
fi
if [ -f "${REPO_ROOT}/.env" ]; then
  cp "${REPO_ROOT}/.env" "${OUT_DIR}/.env"
  say ".env"
else
  echo "     WARNING: no .env found" >&2
fi

# ---- 3. the manifest -------------------------------------------------------
# What this is, so a tarball found in six months can be identified without
# opening it, and so a restore can be checked against what was captured.
echo "3/4  writing the manifest"
{
  echo "captured_at      ${STAMP}"
  echo "source_host      $(hostname)"
  echo "git_commit       $(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "git_branch       $(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  echo "db_container     ${DB_CONTAINER}"
  echo "db_name          ${DB_NAME}"
  echo
  echo "-- row counts at capture, to check the restore against --"
  docker exec "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -t -A -F'  ' -c "
    select 'leads', count(*) from leads
    union all select 'organizations', count(*) from organizations
    union all select 'contact_channels', count(*) from contact_channels
    union all select 'message_drafts', count(*) from message_drafts
    union all select 'outbox_messages', count(*) from outbox_messages
    union all select 'messages', count(*) from messages
    union all select 'suppression_entries', count(*) from suppression_entries
    union all select 'campaigns', count(*) from campaigns
    order by 1;"
} > "${OUT_DIR}/MANIFEST.txt"
say "manifest written"

# ---- 4. one file -----------------------------------------------------------
echo "4/4  packing"
TARBALL="${OUT_DIR}.tar.gz"
tar -czf "${TARBALL}" -C "$(dirname "${OUT_DIR}")" "$(basename "${OUT_DIR}")"
rm -rf "${OUT_DIR}"

echo
echo "Done."
echo "  ${TARBALL}"
echo "  $(du -h "${TARBALL}" | cut -f1)"
echo
echo "This file contains live mailbox passwords and API keys."
echo "Move it with scp, restore it, then delete it from both machines."
echo
echo "  scp \"${TARBALL}\" root@YOUR_VPS:/root/"
