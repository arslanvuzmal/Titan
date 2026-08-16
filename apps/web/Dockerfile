FROM node:20-alpine AS base

# Dependencies stage
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Copy package files
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./
COPY apps/web/package.json ./apps/web/
RUN npm install -g pnpm@8 && pnpm i --no-frozen-lockfile

# Builder stage
FROM base AS builder
WORKDIR /app
COPY --from=deps /app ./
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm install -g pnpm@8 && pnpm --filter titan-web build

# Runner stage
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/apps/web/public ./apps/web/public
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next ./apps/web/.next
COPY --from=builder /app/node_modules ./node_modules
# pnpm puts every package in the root `node_modules/.pnpm` store and links it
# into the workspace that depends on it, so `next` lives at
# apps/web/node_modules/next as a relative symlink into that store. Copying only
# the root left the runner with no `next` at all -- and because the command
# below used `npx`, that absence was silent: npx downloaded its own copy at
# whatever version npm resolved that day, which is a different build of Next
# from the one that produced .next. The failure surfaces as
#   Cannot find module 'next/dist/compiled/next-server/app-page-turbo.runtime.prod.js'
# from a require stack rooted in /home/nextjs/.npm/_npx/, which is the tell.
COPY --from=builder /app/apps/web/node_modules ./apps/web/node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/apps/web/package.json ./apps/web/package.json

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# The local binary, never `npx`. npx falling back to a network fetch is what
# turned a missing dependency into a version mismatch discovered at runtime;
# invoking the binary directly makes a missing dependency fail at start, loudly.
CMD ["./apps/web/node_modules/.bin/next", "start", "apps/web"]
