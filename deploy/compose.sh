#!/bin/sh
# Use this instead of `docker compose` for the production stack.
#
# Two things it stops anyone getting wrong:
#
#   --env-file is not optional. `env_file:` supplies variables to *containers*;
#   ${VAR} interpolation inside the compose file is resolved from the shell or
#   from a .env beside it. Those are different mechanisms and the stack needs
#   both, which cost an evening during the migration to work out.
#
#   The TLS override is layered exactly when there is a TLS listener to publish
#   a port for, so nobody has to remember a second -f -- and 443 is never
#   published with nothing behind it.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ -f nginx/tls/443.conf ]; then
    exec docker compose --env-file ../.env \
        -f docker-compose.prod.yml -f docker-compose.tls.yml "$@"
fi
exec docker compose --env-file ../.env -f docker-compose.prod.yml "$@"
