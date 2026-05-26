# Contributing

## Development setup

```bash
pip install -e '.[dev]'
pre-commit install --hook-type pre-commit --hook-type pre-push
```

A single command installs both gates. `pre-commit install` alone (without
`--hook-type pre-push`) silently skips the pytest gate, which is why the
two-stage install above is the canonical setup.

Pre-commit hooks run `ruff check --fix` and `ruff format` on every commit
(fast — typically <1s). The full `pytest` suite runs on `git push` so commits
stay snappy while pushes are still gated. CI runs the same suite plus coverage
on Linux + macOS (Python 3.12) and enforces `fail_under = 92`.

## Running tests

```bash
pytest                                    # full suite
pytest --cov                              # with coverage (fails under 92%)
pytest tests/test_properties.py -q        # property-based suite only
```

## Coverage

Coverage configuration lives in `pyproject.toml` under `[tool.coverage.*]`.
HTML reports go to `htmlcov/` (gitignored). The CI workflow uploads them as
build artifacts.
