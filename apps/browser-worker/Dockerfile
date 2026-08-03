# Isolated browser evidence worker.
#
# This image deliberately receives NO database, email, or model credentials.
# It is the only component that fetches attacker-controlled URLs, so a full
# browser escape yields nothing that can send mail or read tenant data.
FROM mcr.microsoft.com/playwright:v1.49.1-noble

ENV NODE_ENV=production \
    BROWSER_WORKER_PORT=8800

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --no-audit --no-fund || npm install --omit=dev --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
COPY fixtures ./fixtures

RUN npm install --no-save typescript@5.7.2 \
 && npx tsc -p tsconfig.json \
 && npm prune --omit=dev

# The playwright base image ships a non-root `pwuser`.
USER pwuser
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://localhost:8800/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/src/server.js"]
