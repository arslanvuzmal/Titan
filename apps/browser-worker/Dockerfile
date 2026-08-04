# Isolated browser evidence worker.
#
# This image deliberately receives NO database, email, or model credentials.
# It is the only component that fetches attacker-controlled URLs, so a full
# browser escape yields nothing that can send mail or read tenant data.
#
# Two stages, so the TypeScript compiler never reaches the runtime image. The
# previous single-stage build installed production dependencies, then tried to
# add the compiler with `npm install --no-save typescript` and invoke it with
# `npx tsc`. Neither worked: --no-save under NODE_ENV=production was a no-op,
# and `npx tsc` then downloaded and executed an unrelated registry package
# named `tsc` -- a build-time supply-chain hazard as well as a failure.

# ---------------------------------------------------------------- build stage
FROM mcr.microsoft.com/playwright:v1.49.1-noble AS builder

WORKDIR /app

# NODE_ENV is deliberately not "production" here: the compiler is a
# devDependency and has to be installed.
COPY package.json package-lock.json* ./
RUN npm ci --include=dev --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
COPY fixtures ./fixtures

# The project's own typescript, resolved from node_modules. Nothing is fetched
# at build time beyond the locked dependency set.
RUN ./node_modules/.bin/tsc -p tsconfig.json

# -------------------------------------------------------------- runtime stage
FROM mcr.microsoft.com/playwright:v1.49.1-noble

ENV NODE_ENV=production \
    BROWSER_WORKER_PORT=8800

WORKDIR /app

# `npm ci` with no fallback. A lockfile that disagrees with package.json is a
# reproducibility failure and must fail the build rather than quietly resolve
# to whatever the registry offers today.
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY --from=builder /app/dist ./dist

# The playwright base image ships a non-root `pwuser`.
USER pwuser
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://localhost:8800/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/src/server.js"]
