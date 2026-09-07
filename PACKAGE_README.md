# Handoff Package

This archive contains the Skill Generator source, documentation, prompts, tests, and reproducible dependency lock files.

## Not included

- `.env` and all live credentials
- `.git`
- `.venv` and `node_modules` (rebuild with lock files)
- `.sessions`, test results, Playwright reports, and caches

## Setup

```powershell
Copy-Item .env.example .env
uv sync
npm ci
az login
uv run python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 6274
```

Open http://localhost:6274/ and sign in through Microsoft Entra.
See `docs/02-setup.md` for complete configuration.