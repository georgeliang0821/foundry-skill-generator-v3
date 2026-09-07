# Playwright E2E Tests

Playwright verifies that a real user can operate the website in a browser. These
tests use the real frontend and FastAPI backend, but run entirely locally with a
fake agent and fake test runner.

## What Playwright Tests

Playwright covers UI flows that pytest cannot prove:

- The app boots without a fatal UI error.
- A user can create a new session.
- A user can attach material and send chat messages.
- SSE events update the conversation, stage, checklist, SKILL.md editor, preview, and tool cards.
- The checklist is visible and reflects backend state.
- Question cards behave correctly for messy user behavior.
- The generated `SKILL.md` appears in the editor and rendered Markdown preview.
- A user can accept a draft.
- A draft is saved private unless the author opts in, confirms, and the skill is then published.
- A user can request a conversation-based patch.
- Patch cards can be accepted, rejected, and undone.
- Modify mode can load an existing local skill.
- Tests tab can save samples and display fake local test results.
- UI state and backend session state stay consistent.

## Local-Only Runtime

Playwright must not call cloud services. `playwright.config.ts` starts the server
with:

```env
SGV2_E2E_MODE=1
SGV2_E2E_FAKE_AGENT=1
SGV2_E2E_FAKE_TEST_RUNNER=1
SGV2_SKILL_STORE=local
SGV2_SESSION_DIR=.e2e-sessions
FOUNDRY_PROJECT_ENDPOINT=https://example.test/foundry
FOUNDRY_MODEL=e2e-fake-model
MICROSOFT_TENANT_ID=e2e
MICROSOFT_CLIENT_ID=e2e
MICROSOFT_CLIENT_SECRET=e2e
MICROSOFT_OBO_SCOPE=api://e2e/.default
```

In this mode:

- `FakeE2EAgent` replaces the real LLM orchestrator.
- The skill store is local.
- Selection tests are fake and deterministic.
- `/api/e2e/reset`, `/api/e2e/seed-skill`, and `/api/e2e/scenario` are enabled.

## Current Spec Files

```text
tests/e2e/app-boot.spec.ts
tests/e2e/new-skill.spec.ts
tests/e2e/question-cards.spec.ts
tests/e2e/patch-review.spec.ts
tests/e2e/modify-existing.spec.ts
tests/e2e/tests-tab.spec.ts
tests/e2e/mode-echo.spec.ts
tests/e2e/skill-visibility.spec.ts
```

## Question Card Coverage

Question cards are important because users do not always follow the expected
path. The E2E tests should cover:

- Selecting an option only.
- Typing a custom answer only.
- Selecting an option and typing custom text.
- Submitting with no answer.
- Answering multiple questions partially.
- Ignoring the question card and typing directly in the chat box.
- Running tests while questions are still pending.

The expected direct-chat behavior is:

- The chat message is treated as the answer to pending questions.
- Pending question cards become resolved.
- The card says it was answered from chat.
- Backend session has no pending `ask_user_input` calls.

## Commands

Install Node dependencies:

```powershell
npm install
npx playwright install chromium
```

Run all browser tests:

```powershell
npx playwright test
```

Run headed:

```powershell
npx playwright test --headed
```

Debug interactively:

```powershell
npx playwright test --debug
```

Run one spec:

```powershell
npx playwright test tests/e2e/question-cards.spec.ts
```

Run one named test:

```powershell
npx playwright test -g "answers pending questions from the chat box"
```

## How The Server Starts

You normally do not need to start the backend yourself. Playwright starts it from
`playwright.config.ts`:

```powershell
uv run python -m uvicorn backend.main:app --host 127.0.0.1 --port 6275
```

Playwright uses port `6275` by default and does not reuse an existing server.
This prevents the tests from accidentally hitting a normal development server
running on `6274`.

## Debug Artifacts

On failure, inspect:

```text
test-results/
playwright-report/
```

Useful commands:

```powershell
npx playwright show-report
```

## When To Add Playwright Coverage

Add or update Playwright tests when changing:

- Frontend DOM structure.
- Conversation rendering.
- Tool cards.
- Question card behavior.
- Files tab editor behavior.
- Tests tab behavior.
- Session switching.
- Modify mode.
- User-visible errors or loading states.

Do not add Playwright tests for pure backend branches when a pytest test can
cover the same behavior faster and more reliably.
