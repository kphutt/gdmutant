<!-- Keep it short. Link the issue this closes, if any. -->

## What & why

<!-- What does this change, and why? -->

## How it was tested

<!-- Commands run / cases covered. This is a testing tool, so new behavior needs tests. -->

## Checklist

- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run mypy gdmutant` passes
- [ ] `uv run pytest` passes (new behavior has tests)
- [ ] `uv run pip-audit` passes
- [ ] `uv run python scripts/verify_local.py --job license-check` passes
- [ ] Any new dependency is recorded in `docs/credits.md`
- [ ] A design change is captured as an ADR in `docs/decisions/` (if applicable)
- [ ] New pure logic got a local mutation-test pass, `pre-commit run gdmutant-mutation --hook-stage manual` (if applicable)
