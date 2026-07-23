#!/bin/bash
# Debug script to replicate GitHub Actions environment

set -e

echo "🧹 Cleaning environment..."
rm -rf node_modules
rm -rf apps/web/node_modules
rm -rf apps/web/.next
rm -rf apps/web/.netlify
rm -rf apps/web/apps/web

echo "📦 Installing dependencies..."
pnpm install --no-frozen-lockfile
(cd apps/api && uv pip install -e .[dev])

echo "🔍 Running linters..."
(cd apps/api && uv run ruff check .)
(cd apps/api && uv run black --check .)
(cd apps/web && pnpm lint)

echo "🧪 Running type checks and tests..."
(cd apps/api && uv run mypy app/)
(cd apps/api && uv run coverage run -m pytest tests/)
(cd apps/web && pnpm exec tsc --noEmit)

echo "✅ All checks passed!"
