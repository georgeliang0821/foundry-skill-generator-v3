# AGENTS.md

Conversational **Skill generator**: FastAPI backend + vanilla-JS frontend whose only
product is a `SKILL.md` file. Runs locally against Microsoft Foundry, Entra, Azure SQL
and Azure Blob.

Read these before making non-trivial changes — do not duplicate their content here:

| Topic | Doc |
| --- | --- |
| What the product does, personas, user flow | [docs/01-overview.md](docs/01-overview.md) |
| Env vars, Azure/Entra/SQL/Blob setup, startup | [docs/02-setup.md](docs/02-setup.md) |
| Architecture, data flow, per-file responsibilities | [docs/03-architecture.md](docs/03-architecture.md) |
| Agent mechanism: stages, prompts, tools, materials | [docs/04-agent-mechanism.md](docs/04-agent-mechanism.md) |
| `reference/` snapshot provenance and sync | [docs/05-reference-snapshot-sync.md](docs/05-reference-snapshot-sync.md) |
| Which test layer to add to, and why | [tests/README.md](tests/README.md) |

Prose docs are written in Traditional Chinese; code, comments and prompt files are in
English. Keep each in its existing language.

## Commands (PowerShell)

```powershell
uv sync --dev                                   # python deps (uv, not pip)
npm install; npx playwright install chromium    # e2e deps

uv run --no-sync python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 6274

uv run --no-sync python -m pytest tests/unit tests/api -q   # fast, local-only, no Azure
npx playwright test                                         # auto-starts server on :6275
uv run --no-sync python scripts/sync_reference.py check
```

- **Always pass `--no-sync` to `uv run`.** A bare `uv run` implicitly syncs, which on
  a corporate network can fail outright and otherwise silently downgrades packages to
  `uv.lock`. Run `uv sync --dry-run` and read the `-` lines before ever syncing for
  real. See the troubleshooting section of [docs/02-setup.md](docs/02-setup.md).
- Playwright launches its own server from `.\.venv\Scripts\python.exe`, so `.venv`
  must already exist. Do not hand-start a server on `6275`
  (`reuseExistingServer: false`).
- Dev server port `6274` and the Playwright port `6275` are deliberately different.
- No linter or formatter is configured. Match surrounding style; do not introduce
  ruff/black/mypy config as a side effect of another change.

## Non-negotiable invariants

**`backend/` must never import `reference/`.** `reference/` is a read-only vendored
snapshot of the external EAA runtime. Never hand-edit those files or lift a function
out of them into `backend/`; update them only through `scripts/sync_reference.py sync`
with a new commit in [reference/manifest.json](reference/manifest.json). CI enforces
this via [.github/workflows/reference-check.yml](.github/workflows/reference-check.yml).
`.gitattributes` pins `reference/*.py` to LF so a Windows checkout cannot fake drift.

**Section-name mechanics live only in [backend/sections.py](backend/sections.py).**
`normalize_section` lowercases *first* and strips *second*, and matching is substring
containment, not equality. Both are load-bearing — `topology.py` and `skill_lint.py`
import from here so a rule and the thing it validates cannot drift. Do not "tidy" it.

**Stage changes touch two files.** `Stage` in [backend/models.py](backend/models.py)
and `TRANSITION_TABLE` / `STAGE_PROMPT_MAP` in
[backend/state_machine.py](backend/state_machine.py) must stay in sync; transitions
are whitelist-only.

**Prompts are data, not code.** Everything the model is told is assembled by
`build_system_prompt()` in [backend/state_machine.py](backend/state_machine.py) from
markdown in [prompts/](prompts/). Change wording there, not in Python string literals.
The composition is layered: `00_global_system.md` → stage prompt (overridable per
`SkillKind` via `KIND_PROMPT_OVERRIDES`) → optional `KIND_STAGE_ADDENDA` → shared
`09`/`10`/`11`/`12` files → runtime session state. Adding a prompt file requires
registering it in one of those maps, otherwise it is dead weight.

**Scenario skills are an addendum, not a fork.** When capability/scenario behaviour
differs, add to `KIND_STAGE_ADDENDA` rather than copying a whole stage prompt —
forked copies drift.

**Patches are V4A and strict.** [backend/patch.py](backend/patch.py) raises
`PatchError` when an anchor is missing or ambiguous; partial application is never
acceptable. A `propose_patch` closes an open-fix item only when `addresses` names it
verbatim.

## Frontend

Vanilla ES modules, no framework, no build step; FastAPI serves
[frontend/](frontend/) directly. [frontend/js/api.js](frontend/js/api.js) is the only
place that calls `fetch` — every call goes through `apiFetch` with
`credentials: "include"` for the `sgv2_auth` cookie. Styling uses the CSS custom
properties at the top of [frontend/css/style.css](frontend/css/style.css); add tokens
rather than hard-coded colors. Do not add a bundler, a framework, or a package that
needs one.

## Tests

Tests never touch Azure, Foundry or real OAuth. Pick the cheaper layer:

- Backend behaviour, API shapes, parsing, persistence → pytest. Fixtures in
  [tests/conftest.py](tests/conftest.py): `fake_sql` (in-memory SQL schema v2.2),
  `backend_main`, `client` (sends `X-Test-UPN: test@example.com`), `anon_client`
  (for 401 paths), `grant_skill`.
- DOM structure, SSE-driven UI updates, tool/question cards, tab behaviour →
  Playwright, against the deterministic fake agent enabled by `SGV2_E2E_MODE` /
  `SGV2_E2E_FAKE_AGENT` (see [backend/e2e.py](backend/e2e.py)).

Do not write a Playwright test for a branch pytest can cover. See
[tests/PYTEST_TESTS.md](tests/PYTEST_TESTS.md) and
[tests/PLAYWRIGHT_TESTS.md](tests/PLAYWRIGHT_TESTS.md) for the full split.

## Identity and access

UPN resolution order is defined once in [backend/acl.py](backend/acl.py):
`X-Test-UPN` header (only when `E2E_MODE` is on) → `sgv2_auth` cookie →
`E2E_DEFAULT_UPN`. UPNs are lowercased everywhere; storing mixed case in
`dbo.user_skill_grants` produces silent 403s. Every skill read goes through
`assert_can_access`, which is cached for 60s — invalidate the cache after changing
grants. Schema changes are additive migration files in [db/](db/); add a new
`v2.x migration.sql` instead of editing an existing one.

## Naming

- Skill name: `[a-z0-9-]`, no leading/trailing hyphen — sanitize via
  `safe_skill_name()` in [backend/blob_store.py](backend/blob_store.py).
- `blob_prefix_of()` ends with `/`, `blob_path_of()` does not; mixing them yields 404s.
- Blob paths always use `/`, never `\`, regardless of host OS.
