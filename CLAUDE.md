# CLAUDE.md

This file provides project-specific guidance for Claude Code when working in this repository.

## Project Overview

This is a local FastAPI + Jinja2 pytest runner platform. It lets users configure pytest projects, start test runs from a web UI, and inspect run history, reports, logs, and progress.

## Runtime and Dependencies

- Python: use `python3`; the project is tested in this environment with Python 3.11. Keep code compatible with Python 3.10+ unless the project explicitly raises the floor.
- Web stack: FastAPI, Uvicorn, Jinja2 templates, and `python-multipart` for form handling.
- Storage: SQLite via Python's standard `sqlite3` module. There is no SQLAlchemy or aiosqlite layer.
- Tests: pytest 7.x plus pytest-html, pytest-xdist, and allure-pytest. FastAPI's TestClient is used for route tests.
- Template dependency: Jinja2 is listed in `requirements.txt` without a pinned version; use standard Jinja2 syntax already present in `app/templates/`.

## Common Commands

### Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

If proxy is needed in this environment, prefix commands with:

```bash
https_proxy=http://127.0.0.1:7897 \
http_proxy=http://127.0.0.1:7897 \
all_proxy=socks5://127.0.0.1:7897
```

### Run tests

Use `python3 -m pytest`, not bare `pytest`. In this environment, bare `pytest` may use a Python interpreter that does not have FastAPI installed.

```bash
python3 -m pytest -q
```

Focused tests:

```bash
python3 -m pytest tests/test_storage.py tests/test_routes_pagination.py -v
```

### Run the app

```bash
PYTHONPATH="/Users/mac/Documents/pytest" python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Smoke test:

```bash
curl -sS http://127.0.0.1:8000/runs | grep -E "删除选中记录|bulk-delete-form|select-all-runs"
```

## Development Rules

- Prefer test-first changes for bug fixes and behavior changes.
- Add or update automated tests for storage, route, and UI-template behavior changes.
- Keep changes focused; do not bundle unrelated refactors.
- Match existing FastAPI/Jinja2 patterns in `app/main.py` and `app/templates/`.
- Use `python3 -m pytest` for verification and report exact failures if tests do not pass.

## Run Record Deletion Safety

The run history deletion flow is safety-sensitive because it deletes files from disk. Keep changes conservative:

- Do not delete active runs; skip `queued` and `running` records and tell the user what happened.
- Constrain file deletion to the selected run's own report directory; never broaden cleanup paths casually.
- Prefer recoverable failure modes and clear user-visible messages over silent cleanup success.
- Keep storage, route, and template behavior covered by focused tests when changing deletion logic.

Relevant files:

- `app/storage.py` — run metadata storage and deletion rules
- `app/main.py` — `/runs`, `/runs/delete`, and run-detail routes
- `app/templates/runs.html` — run history bulk-delete UI
- `tests/test_storage.py` — storage deletion coverage
- `tests/test_routes_pagination.py` — route and UI rendering coverage

## Git Workflow

- Do not commit or push unless explicitly asked.
- When committing from the default branch, create a feature/fix branch first unless the user explicitly asks for a direct main-branch change.
- Prefer PR-based merges for changes that are pushed to GitHub.
