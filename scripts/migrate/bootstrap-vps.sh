#!/usr/bin/env bash
# Bring Titan up on a fresh Ubuntu server, from the backup tarball.
#
# Runs steps 3 to 6 of the migration: Docker, the repo, the secrets, the
# database, and the stack -- with campaigns paused, because two hosts that can
# both send is the one mistake in this migration that reaches real people.
#
#   sudo bash bootstrap-vps.sh /root/titan-migrate-YYYYMMDDTHHMMSSZ.tar.gz
#
# Idempotent as far as it can be: re-running skips what is already installed and
# refuses to overwrite a database that already has rows.

set -euo pipefail

TARBALL="${1:-}"
INSTALL_DIR="${TITAN_INSTALL_DIR:-/opt/titan}"
REPO_URL="${TITAN_REPO_URL:-https://github.com/arslanvuzmal/Titan.git}"
BRANCH="${TITAN_BRANCH:-phase0/stop-the-bleeding}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s ==\n' "$*"; }

[ -n "${TARBALL}" ] || die "usage: bootstrap-vps.sh /path/to/titan-migrate-*.tar.gz"
[ -f "${TARBALL}" ] || die "no such file: ${TARBALL}"
[ "$(id -u)" -eq 0 ] || die "run with sudo"

# ---- 3. Docker, and nothing that needs a desktop ---------------------------
step "Docker Engine"
if command -v docker >/dev/null 2>&1; then
  echo "already installed: $(docker --version)"
else
  # Docker's own convenience script. Engine and the compose plugin only -- no
  # Docker Desktop, no WSL, no desktop session, which is the entire point of
  # moving off the laptop.
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || die "the compose plugin is missing"

step "firewall"
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow 80/tcp  >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  yes | ufw enable >/dev/null 2>&1 || true
  echo "ufw: 22, 80, 443"
else
  echo "ufw not present; skipping (check your provider's firewall instead)"
fi

# ---- 4. the repository -----------------------------------------------------
step "repository"
if [ -d "${INSTALL_DIR}/.git" ]; then
  git -C "${INSTALL_DIR}" fetch --all --quiet
  git -C "${INSTALL_DIR}" checkout --quiet "${BRANCH}"
  git -C "${INSTALL_DIR}" pull --quiet
  echo "updated ${INSTALL_DIR} to $(git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
else
  git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
  echo "cloned to ${INSTALL_DIR}"
fi

# ---- 5. secrets and .env, out of the tarball -------------------------------
step "secrets"
WORK="$(mktemp -d)"
tar -xzf "${TARBALL}" -C "${WORK}"
SRC="$(find "${WORK}" -maxdepth 1 -type d -name 'titan-migrate-*' | head -1)"
[ -n "${SRC}" ] || die "the tarball does not look like a Titan migration backup"

[ -d "${SRC}/secrets" ] && cp -R "${SRC}/secrets" "${INSTALL_DIR}/secrets"
[ -f "${SRC}/.env" ] && cp "${SRC}/.env" "${INSTALL_DIR}/.env"
# Readable only by root. These are live mailbox passwords.
chmod -R go-rwx "${INSTALL_DIR}/secrets" "${INSTALL_DIR}/.env" 2>/dev/null || true
echo "secrets/ and .env in place, mode 600"

cat "${SRC}/MANIFEST.txt" 2>/dev/null | head -20

# ---- 5b. make a Windows .env usable on Linux -------------------------------
step "normalising .env"
# Two things bite here, both found the hard way on 16 September.
#
# CRLF: the file comes off a Windows laptop, so every value ends in a carriage
# return. `set -a; . ./.env` then exports TITAN_DATABASE_URL with a trailing \r
# and every connection string is subtly wrong.
if [ -f "${INSTALL_DIR}/.env" ]; then
  sed -i 's/\r$//' "${INSTALL_DIR}/.env"
  for f in "${INSTALL_DIR}"/secrets/*.json; do
    [ -f "$f" ] && sed -i 's/\r$//' "$f"
  done
  echo "line endings normalised"
fi

# Host-mapped ports: the laptop reaches Postgres on localhost:5442 because
# compose publishes it there. Inside the compose network the service is
# `postgres:5432`, and a copied .env points the whole stack at a port nothing
# is listening on. migrate fails with "connection refused" and takes every
# dependent service with it.
if grep -qE '^TITAN_DATABASE_URL=.*(localhost|127\.0\.0\.1)' "${INSTALL_DIR}/.env" 2>/dev/null; then
  sed -i 's|^TITAN_DATABASE_URL=.*|TITAN_DATABASE_URL=postgresql+psycopg://titan:titan_dev_password@postgres:5432/titan|' "${INSTALL_DIR}/.env"
  echo "TITAN_DATABASE_URL pointed at the compose service"
fi

# Variables compose interpolates into the file itself, as opposed to the ones
# it passes into containers. These are `${VAR:?}` in the compose file, and an
# assignment that exists but is *empty* counts as missing -- which is exactly
# what TITAN_BROWSER_WORKER_TOKEN= was, so a `grep -q ^VAR=` guard matched it
# and never filled it in.
ensure_var() {
  local name="$1" value="$2"
  local current
  current="$(grep -E "^${name}=" "${INSTALL_DIR}/.env" | tail -1 | cut -d= -f2-)"
  if [ -z "${current}" ]; then
    sed -i "/^${name}=/d" "${INSTALL_DIR}/.env"
    echo "${name}=${value}" >> "${INSTALL_DIR}/.env"
    echo "  set ${name}"
  fi
}
ensure_var TITAN_API_IMAGE "titan-api:local"
ensure_var TITAN_BROWSER_WORKER_IMAGE "titan-browser-worker:local"
ensure_var TITAN_BROWSER_WORKER_TOKEN "$(openssl rand -hex 24)"
ensure_var POSTGRES_USER "titan"
ensure_var POSTGRES_PASSWORD "titan_dev_password"
ensure_var TEMPORAL_POSTGRES_PASSWORD "titan_dev_password"

# ---- 5c. images ------------------------------------------------------------
# Built here rather than pulled: there is no registry, and building on the
# target is one fewer moving part than pushing to one.
step "images"
cd "${INSTALL_DIR}"
docker build -q -f apps/api/Dockerfile -t titan-api:local . >/dev/null
docker build -q -f apps/browser-worker/Dockerfile -t titan-browser-worker:local apps/browser-worker >/dev/null
echo "titan-api:local and titan-browser-worker:local built"

# ---- 6. database, then the stack, paused -----------------------------------
step "database"
cd "${INSTALL_DIR}"
# --env-file is not optional. `env_file:` inside the compose file supplies
# variables to *containers*; ${VAR} interpolation in the compose file itself is
# resolved from the shell or from a .env sitting next to the compose file. Ours
# is at the repo root, so it has to be named explicitly or every image and
# password interpolates to nothing.
COMPOSE="docker compose --env-file .env -f deploy/docker-compose.prod.yml"

${COMPOSE} up -d postgres
echo "waiting for postgres"
for _ in $(seq 1 60); do
  if ${COMPOSE} exec -T postgres pg_isready -U titan -d titan >/dev/null 2>&1; then break; fi
  sleep 2
done
${COMPOSE} exec -T postgres pg_isready -U titan -d titan >/dev/null 2>&1 \
  || die "postgres did not come up"

EXISTING="$(${COMPOSE} exec -T postgres psql -U titan -d titan -t -A \
  -c "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo 0)"
if [ "${EXISTING}" -gt 5 ]; then
  echo "database already has ${EXISTING} tables -- refusing to restore over it."
  echo "Drop it deliberately if that is what you want:"
  echo "  ${COMPOSE} exec -T postgres psql -U titan -d titan -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'"
else
  ${COMPOSE} exec -T postgres psql -U titan -d titan -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
  echo "restoring (this takes a few minutes)"
  ${COMPOSE} exec -T postgres pg_restore -U titan -d titan --no-owner --clean --if-exists \
    < "${SRC}/titan.dump" 2>&1 | tail -5 || true
  echo "restored"
fi

step "migrations"
${COMPOSE} run --rm migrate 2>&1 | tail -3

step "stack (campaigns stay paused)"
${COMPOSE} up -d
sleep 20
${COMPOSE} ps

rm -rf "${WORK}"

cat <<'NEXT'

== what is NOT done, deliberately ==

Campaigns are whatever state the database says. Before unpausing, the old host
must be stopped -- two hosts sending from the same mailboxes will double-send to
real businesses, and there is no taking that back.

Next, in order:

  1. verify:   bash scripts/migrate/verify.sh
  2. set the PTR record on this server's IP, and an A record pointing back
  3. stop the stack on the laptop
  4. send one test message from here and confirm it arrives
  5. only then unpause, and set TITAN_HEALTHCHECK_PING_URL

NEXT
