#!/bin/bash
# This script mimics the GitHub Actions environment
# Run this locally to catch CI/CD failures before pushing
set -e

echo "🔍 Running CI/CD checks locally..."

# Python checks
echo "🐍 Python linting (ruff)..."
cd apps/api
ruff check .

echo "🐍 Python formatting (black)..."
black --check .

# Type checks
echo "🔍 Python type checking (mypy)..."
mypy app/ || echo "⚠️  mypy warnings (non-blocking)"

# Tests
echo "🧪 Running Python tests..."
pytest tests/ -v
cd ../..

# TypeScript checks  
echo "📦 TypeScript linting (eslint)..."
cd apps/web
pnpm lint

echo "🔍 TypeScript type checking (tsc)..."
pnpm tsc --noEmit
cd ../..

echo "✅ All checks passed!"
