# Pytest Unit And API Tests

Pytest verifies the backend without opening a browser. These tests are fast,
deterministic, and should be the first line of defense for backend behavior.

## What Pytest Tests

Pytest covers:

- Pydantic model validation and defaults.
- Stage transition rules.
- Prompt assembly and runtime state formatting.
- V4A patch parsing and application.
- Patch failure cases such as missing anchors, ambiguous anchors, and version
  mismatches.
- Skill frontmatter parsing and safe skill names.
- Local skill store save/load/list behavior.
- Local session persistence and corrupt-session tolerance.
- Agent helper parsing for JSON payloads and tool calls.
- MCP JSON-RPC response parsing.
- Diagnostics masking for sensitive fields.
- Selection-test helper parsing and hit-rate calculation.
- FastAPI session routes.
- Draft update and checklist update routes.
- Save/list/load skill routes.
- Patch apply, tool result, patch history, and undo routes.
- Fake or mocked test runner behavior.
- Auth status/logout helper routes.
- Inspect endpoint structure.
- Chat SSE backend side effects with a fake agent.

## What Pytest Does Not Test

Pytest does not verify:

- Browser rendering.
- User clicks and keyboard input.
- CSS layout or visual state.
- Whether the UI updates correctly after SSE events.
- Full user journeys across tabs.

Those belong to Playwright.

## External Services

Pytest does not call real external services. `tests/conftest.py` sets local test
configuration:

```env
SGV2_SKILL_STORE=local
SGV2_SESSION_DIR=<pytest temp dir>
FOUNDRY_PROJECT_ENDPOINT=https://example.test/foundry
FOUNDRY_MODEL=test-model
MICROSOFT_TENANT_ID=tenant
MICROSOFT_CLIENT_ID=client
MICROSOFT_CLIENT_SECRET=secret
MICROSOFT_OBO_SCOPE=api://scope/.default
```

It also replaces module-level stores so each API test starts from isolated state.

## Commands

Run all pytest tests:

```powershell
uv run pytest
```

Run unit tests only:

```powershell
uv run pytest tests/unit
```

Run API tests only:

```powershell
uv run pytest tests/api
```

Run one file:

```powershell
uv run pytest tests/unit/test_patch.py
```

Run one test:

```powershell
uv run pytest tests/api/test_tool_result_api.py::test_accept_propose_patch_updates_content_and_patch_history
```

Run with coverage:

```powershell
uv run pytest --cov=backend --cov-report=term-missing
```

## When To Add Pytest Coverage

Add or update pytest tests when changing:

- Backend state transitions.
- Any Pydantic model.
- Patch apply behavior.
- Session persistence.
- Skill save/load behavior.
- API response shape.
- Error handling.
- Test runner parsing or pass/fail logic.
- Any helper that Playwright should not need to cover repeatedly.
