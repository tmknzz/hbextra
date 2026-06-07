## Inputs

- AIB audit document: `/Users/jonji/Library/Mobile Documents/iCloud~md~obsidian/Documents/AIB/10_products/hbextra/security-audit-2026-06-07.md`
- Repo cwd: `/Users/jonji/GitHub/tmknzz/HBExtra`

## Current state

- Branch started from `main` with existing dirty worktree:
  - modified: `.gitignore`, `hbextra.html`, `hbextra.py`, `requirements.txt`
  - untracked: `ops/`, `wsgi.py`
- Existing local artifacts:
  - `.secret_key`
  - `hbextra.db`, `hbextra.db-shm`, `hbextra.db-wal`
  - `logs/`
- `.gitignore` currently ignores DB, WAL/SHM, `.secret_key`, logs via `*.log`.

## Architecture notes

- Single-file Flask backend in `hbextra.py`.
- Static frontend in `hbextra.html`, served from `/hbextra/`.
- App mounted under `/hbextra` with `DispatcherMiddleware`.
- SQLite data is shared for `entries` and `memberships`; per-user tables exist for stars/dismissed.
- `wsgi.py` starts scheduler/tag background threads for gunicorn.

## Risk notes

- `/api/proxy` is the highest risk because it turns attacker HTML into same-origin HTML if navigated directly.
- Import validation and inline handlers are the second highest risk because they produce stored XSS and server-side crashes.
- Public registration turns authenticated-only vulnerabilities into public self-service attacks.
- Security headers and cookie flags are defense-in-depth but should be handled after XSS/proxy surfaces are closed.

## Unknowns

- Whether full HTML preview must be preserved. Default plan: preserve iframe preview if possible while sandboxing proxy response at response header level.
- Whether public self-registration is a product requirement. Default plan: first user bootstrap remains, later registration requires an invite/registration token.
