# Test README

This project has two test layers:

- Pytest unit/API tests: fast backend tests for state machines, patch logic, API
  routes, persistence, and mocked integrations.
- Playwright E2E tests: browser tests that operate the real UI with a local fake
  agent and local fake test runner.

Start here when you want to run tests. Read the two detailed documents for what
each layer is responsible for:

- [PYTEST_TESTS.md](./PYTEST_TESTS.md)
- [PLAYWRIGHT_TESTS.md](./PLAYWRIGHT_TESTS.md)

## Prerequisites

Install Python dependencies:

```powershell
uv sync --dev
```

Install Node dependencies for Playwright:

```powershell
npm install
npx playwright install chromium
```

## Run Pytest

Run all unit and API tests:

```powershell
uv run pytest
```

Run by layer:

```powershell
uv run pytest tests/unit
uv run pytest tests/api
```

Run with backend coverage:

```powershell
uv run pytest --cov=backend --cov-report=term-missing
```

### Pytest never reaches ACA / MCP

`backend.main` runs `load_dotenv(override=True)` on import, so a real
`MCP_ENDPOINT` / `ACA_*` from `.env` wins over anything the test set. Two
fixtures in [conftest.py](./conftest.py) keep pytest local:

- An autouse fixture sets `state_machine.PostTransitionHook = None` for every
  test. Once any test has imported `backend.main`, its PREPARE-entry hook (ACA
  lookup + peer-skill load in a background thread) is process-global, so a
  plain unit test calling `transition()` would otherwise start real MCP calls.
- `backend_main` re-enables the hook for API tests but replaces
  `load_aca_env_for_session` with a no-op that keeps whatever
  `aca_env_result` the session already has. A test that needs the EAA script
  flags on sets `session.aca_env_result` itself (see
  `tests/api/test_script_skill_api.py`).

## Run Playwright

Playwright runs fully locally. It does not call Foundry, Azure Blob, APIM,
Microsoft OAuth, or ACA MCP. The Playwright config starts the FastAPI app with:

```env
SGV2_E2E_MODE=1
SGV2_E2E_FAKE_AGENT=1
SGV2_E2E_FAKE_TEST_RUNNER=1
SGV2_SKILL_STORE=local
SGV2_SESSION_DIR=.e2e-sessions
```

Run the browser tests:

```powershell
npx playwright test
```

In E2E mode the ACA lookup never leaves the machine either:
`load_aca_env_for_session` returns `backend.e2e.fake_aca_env_result()`, which
turns the EAA script flags on only in the `script_flags_on` scenario
(`/api/e2e/scenario`). Every other scenario sees no ACA result, i.e. the flags
are off.

Run headed:

```powershell
npx playwright test --headed
```

Debug interactively:

```powershell
npx playwright test --debug
```

Run one Playwright spec:

```powershell
npx playwright test tests/e2e/question-cards.spec.ts
```

Run one named test:

```powershell
npx playwright test -g "answers pending questions from the chat box"
```

## Useful Order

For normal local validation:

```powershell
uv run pytest
npx playwright test
```

If Playwright fails, inspect:

```text
test-results/
playwright-report/
```

These folders are generated artifacts and should not be committed.

## Local State

Pytest uses temporary session directories.

Playwright uses:

```text
.e2e-sessions/
```

The app itself may also create:

```text
.sessions/
```

Both are ignored by git.

Playwright uses port `6275` by default so it does not accidentally reuse a normal
development server on `6274`. Override with `E2E_PORT` only if needed.
