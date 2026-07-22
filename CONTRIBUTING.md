# Contributing to TITAN

We love your input! We want to make contributing to TITAN as easy and transparent as possible.

## Code Style Guidelines

- **Python**: We strictly enforce `ruff` for linting, `black` for formatting, and `mypy` for static typing. No exceptions.
- **TypeScript**: We use `eslint` and `prettier` (via pnpm lint). All code must pass `tsc --noEmit` in strict mode.

## Development Workflow

1. Create a feature branch (`feat/...` or `fix/...`)
2. Write tests for your feature! Run the chaos suite locally if touching the action engine.
3. Run the CI debug script locally before pushing:
   ```bash
   ./scripts/debug_ci.sh
   ```
4. Push and open a PR.

## Running Tests

All external APIs and LLMs MUST be mocked in tests to ensure determinism.

```bash
# Run backend tests
cd apps/api
pytest tests/ -v
```

## Pull Request Template

When opening a PR, please ensure you answer the following:
- What does this PR do?
- Does it require a database migration?
- Are there new environment variables needed?
- Did you write/update tests?
