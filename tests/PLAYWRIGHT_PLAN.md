# Playwright E2E Test Plan

This plan covers browser-level tests for Skill Generator v2 using only local,
deterministic services. Playwright should verify that a real user can complete
the UI workflow, while pytest continues to verify backend rules and API behavior.

## Goals

- Run the real frontend in a browser.
- Avoid real LLM calls.
- Avoid all cloud services.
- Verify that visible UI state matches backend session state.
- Cover new skill creation, conversation-based edits, modify mode, checklist
  interaction, question cards, patch review, save, reload, and local test results.

## Non-Goals

- Do not call Foundry, Azure Blob, APIM, Microsoft OAuth, or ACA MCP.
- Do not test every backend edge case in Playwright. Backend edge cases belong in
  pytest.
- Do not depend on generated model text. Fake agent responses must be stable.

## Local E2E Runtime

Add a local-only E2E mode controlled by environment variables:

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

- `OrchestratorAgent` is replaced with a deterministic fake agent.
- Skill storage uses local in-memory or local test storage.
- Selection tests return deterministic fake pass/fail results.
- Auth status can remain unauthenticated unless a specific UI case needs a fake
  signed-in profile.
- Session files are written under `.e2e-sessions/`, which must be git ignored.

## Backend Test Hooks

Add test-only endpoints, enabled only when `SGV2_E2E_MODE=1`:

```http
POST /api/e2e/reset
POST /api/e2e/seed-skill
GET  /api/e2e/session/{id}
POST /api/e2e/scenario
```

Suggested behavior:

- `reset`: clears sessions, local skills, auth tokens, fake-agent scenario state.
- `seed-skill`: saves a known skill into local skill store for modify-mode tests.
- `session/{id}`: returns raw session for assertions that are hard to verify from UI.
- `scenario`: selects fake-agent scenario, such as `new_skill`, `patch_refine`,
  `question_cards`, or `test_failure`.

These endpoints must not be enabled outside E2E mode.

## Fake Agent Design

The fake agent should implement the same stream contract as the real agent:

```python
class FakeE2EAgent:
    def stream(self, session, user_message):
        yield {"event": "text_delta", "data": {"delta": "..."}}
        yield {"event": "tool_call", "data": {...}}
```

It should choose responses based on:

- Current session stage.
- The latest user message.
- The selected fake scenario.
- Existing pending tool calls.

Recommended scenarios:

| Scenario | Purpose |
|---|---|
| `new_skill_happy_path` | Move from intake to checklist to draft. |
| `question_cards` | Emit one or more `ask_user_input` calls. |
| `patch_refine` | Emit `propose_patch` after user asks for a change. |
| `modify_existing` | Patch an existing loaded skill. |
| `test_success` | Request fake selection test and return passing results. |
| `test_failure` | Return failing results, then ask for correction direction. |
| `mode_echo_missing` | Fake runner omits the top-level `mode` echo, so the batch must abort with a 502 instead of reporting results. |

Fake agent output should be stable and short. Playwright should not need to parse
long prose.

## Frontend Selector Strategy

Current stable IDs can be used:

- `#modeSelect`
- `#targetSkill`
- `#sessionList`
- `#newSessionBtn`
- `#messageInput`
- `#sendBtn`
- `#chatStream`
- `#toolCalls`
- `#editor`
- `#saveDraftBtn`
- `#positiveSamplesInput`
- `#negativeSamplesInput`
- `#saveTestSamplesBtn`
- `#runTestBtn`
- `#testResults`

Prefer adding `data-testid` for Playwright stability:

```html
<button id="newSessionBtn" data-testid="new-session-button">
<textarea id="messageInput" data-testid="message-input">
<div id="chatStream" data-testid="chat-stream">
<div id="toolCalls" data-testid="tool-calls">
<textarea id="editor" data-testid="file-editor">
```

Use text selectors only for user-visible labels that are intentionally stable,
such as `Accept`, `Reject`, `Undo`, `Run tests`, or `Accept draft`.

## Test Files

Suggested structure:

```text
tests/e2e/
  new-skill.spec.ts
  question-cards.spec.ts
  patch-review.spec.ts
  modify-existing.spec.ts
  tests-tab.spec.ts
  helpers/
    app.ts
    e2eApi.ts
    assertions.ts
playwright.config.ts
package.json
```

## Core E2E Flows

### 1. App Boot And New Session

Purpose: verify the app renders and can create a local session.

Steps:

1. Reset E2E backend.
2. Open `/`.
3. Verify header, stage title, context tabs, chat input, and session controls.
4. Select `new`.
5. Click `New session`.
6. Verify `sessionId` changes from `No session`.
7. Verify stage shows Prepare / INTAKE state.

Assertions:

- No fatal error is visible.
- `#messageInput` and `#sendBtn` are enabled.
- `#skillBindingStatus` shows `Blob skill: NEW`.
- Backend session exists via `/api/e2e/session/{id}`.

### 2. New Skill Happy Path

Purpose: verify a user can create a skill from chat/materials without LLM calls.

Steps:

1. Reset E2E backend.
2. Create a new session.
3. Attach material in Materials tab.
4. Send a user message requesting a new skill.
5. Fake agent emits stage/checklist updates.
6. Verify conversation message appears.
7. Verify checklist tab shows confirmed/revised items.
8. Continue until fake agent emits `propose_skill_draft`.
9. Verify Files tab shows the `SKILL.md` editor and rendered preview.
10. Click `Accept draft`.
11. Verify local save completes.

Assertions:

- Checklist contains expected item names:
  - understanding
  - differentiation
  - identity
  - metadata
  - io_spec
  - env_vars
  - test_samples
- Files editor and preview contain the fake skill name and sample code block.
- Skill binding changes from `NEW` to saved local skill.
- Backend session has `current_skill.skill_md` and `remote_skill_id`.

### 3. Checklist Editing

Purpose: verify checklist UI updates and routes the session back to VERIFY.

Steps:

1. Create a session with fake checklist data.
2. Open Checklist tab.
3. Edit test samples through Tests tab.
4. Click `Save samples`.
5. Verify session returns to VERIFY.
6. Verify updated positive/negative samples remain visible.

Assertions:

- `verify_checklist.test_samples.status` is `revised` or expected status.
- Positive and negative textareas preserve input after tab switching.
- Stage title reflects VERIFY / Prepare state.

## Question Card Coverage

Question cards are high-risk because users can answer in several valid ways.
Playwright should cover these cases explicitly.

### 4. Single Question: Select Option Only

Steps:

1. Fake agent emits one `ask_user_input`.
2. Select one radio option.
3. Click `Submit answers`.

Assertions:

- Card becomes resolved.
- Selected option is shown in the resolved card.
- Backend receives tool result with that option as answer.
- Pending question count becomes zero.

### 5. Single Question: Custom Answer Only

Steps:

1. Fake agent emits one `ask_user_input`.
2. Do not select any radio option.
3. Type into the custom answer textarea.
4. Click `Submit answers`.

Assertions:

- Backend receives the custom text as answer.
- Card is resolved.
- Custom answer remains visible in conversation history.

### 6. Single Question: Option Plus Custom Answer

Steps:

1. Fake agent emits one `ask_user_input`.
2. Select a radio option.
3. Type additional custom text.
4. Click `Submit answers`.

Assertions:

- Backend receives answer in this shape:

```text
Selected option: <option>
Additional input: <custom text>
```

- UI shows both selected option and custom answer.

### 7. Single Question: Submit Without Answer

Steps:

1. Fake agent emits one `ask_user_input`.
2. Do not select any option.
3. Do not type custom text.
4. Click `Submit answers`.

Assertions:

- UI does not block submission.
- Backend receives `使用者沒有回覆` or the app's configured no-answer fallback.
- Card becomes resolved.
- Conversation shows that no answer was provided.

### 8. Multiple Questions: Partial Answers

Steps:

1. Fake agent emits two or more `ask_user_input` calls.
2. Answer only the first question.
3. Leave the second question blank.
4. Click `Submit answers`.

Assertions:

- First answer is submitted normally.
- Blank answer is submitted as no-answer fallback.
- Progress text updates to all answered / zero remaining.
- All question items are resolved together.

### 9. Ignore Question Card And Type In Chat

Purpose: verify the user can bypass the structured card by typing directly in
the chat input.

Steps:

1. Fake agent emits one or more pending question cards.
2. Do not interact with radio buttons or custom answer fields.
3. Type a freeform answer in `#messageInput`.
4. Click `Send`.

Expected behavior:

- The app treats the chat message as the answer to the pending questions.
- Pending question cards become resolved.
- The card is marked as answered from chat.
- Backend receives tool result(s) based on the freeform chat answer.
- The chat message appears in conversation.

Assertions:

- No duplicate unresolved question card remains.
- `#questionQueueStatus` is hidden or shows zero pending.
- Backend session has no pending `ask_user_input` calls.
- Conversation includes the user's freeform answer.

### 10. Pending Questions Then Run Tests

Purpose: verify test execution does not silently discard pending questions.

Steps:

1. Fake agent emits a pending question card.
2. Switch to Tests tab.
3. Click `Run tests`.
4. Confirm the app's pending-question handling flow if a confirmation appears.

Expected behavior:

- App resolves pending questions before running tests, or blocks with a clear
  message.
- If continuing, unanswered questions are submitted as no-answer fallback.
- Test run starts only after pending question state is handled.

Assertions:

- No pending question remains after test starts.
- Test result card appears.
- Backend session has a test run.

## Patch Review Flows

### 11. Accept Patch

Steps:

1. Start from a session with draft files.
2. Send chat message: "Please improve the negative selection boundary."
3. Fake agent emits `propose_patch`.
4. Verify patch card appears with Accept and Reject.
5. Click `Accept`.

Assertions:

- Patch card is marked accepted.
- Files editor content changes.
- Patch history is visible in backend session.
- Undo button appears for the latest patch.

### 12. Reject Patch

Steps:

1. Fake agent emits `propose_patch`.
2. Click `Reject`.

Assertions:

- Patch card is marked rejected.
- Files editor content does not change.
- Pending tool call is removed.

### 13. Undo Latest Patch

Steps:

1. Accept a patch.
2. Click `Undo`.

Assertions:

- Files editor returns to previous content.
- Patch history latest entry is removed.
- Undo button disappears or moves to latest remaining patch.

### 14. Patch Failure UI

Steps:

1. Fake agent emits a patch with a missing or ambiguous anchor.
2. Click `Accept`.

Assertions:

- UI shows actionable patch failure.
- Patch card remains visible.
- User can send another message or fake agent can propose corrected patch.
- Files editor content is unchanged.

## Modify Mode Flows

### 15. Modify Existing Skill Loads Files

Steps:

1. Reset E2E backend.
2. Seed local store with `demo-existing-skill`.
3. Open `/`.
4. Select `modify`.
5. Select seeded skill from `#targetSkill`.
6. Click `New session`.

Assertions:

- Files tab loads existing `SKILL.md`.
- Skill binding shows seeded skill.
- Backend session has `remote_skill_id`.
- Stage starts in expected mode/state.

### 16. Modify Existing Skill Through Conversation

Steps:

1. Start from modify session.
2. Send a requested edit through chat.
3. Fake agent emits `propose_patch`.
4. Accept patch.
5. Save current skill.
6. Reload page or select session again.

Assertions:

- Edited content persists after reload.
- Skill list still contains the modified skill.
- Backend local store has the updated version hash.

## Tests Tab Flows

### 17. Save Test Samples

Steps:

1. Open Tests tab.
2. Type positive and negative samples.
3. Click `Save samples`.

Assertions:

- Textareas preserve values.
- Checklist `test_samples` is updated.
- Session moves to VERIFY if current behavior requires it.

### 18. Run Fake Selection Test

Steps:

1. Ensure draft is saved locally.
2. Open Tests tab.
3. Click `Run tests`.
4. Fake test runner returns deterministic results.

Assertions:

- Test card appears in conversation.
- Tests tab shows latest run open by default.
- Positive hit rate and negative correct reject rate are displayed.
- Individual sample rows show expected/actual skill and pass/fail.

### 19. Run Test While Unsaved

Steps:

1. Modify editor content without saving.
2. Click `Run tests`.

Assertions:

- UI warns that local files differ from saved skill.
- User gets clear options or a clear blocked state.
- No fake test run starts until save/run-saved/cancel behavior is resolved.

## Session Persistence Flows

### 20. Reload Restores Session

Steps:

1. Create session and produce draft.
2. Reload browser page.
3. Select session from `#sessionList` if not automatically selected.

Assertions:

- Conversation history is visible.
- Files editor content is restored.
- Stage and skill binding are restored.
- Pending tool calls are restored if any were pending before reload.

### 21. Multiple Sessions

Steps:

1. Create session A.
2. Create session B.
3. Select session A from dropdown.
4. Select session B from dropdown.

Assertions:

- Chat, files, checklist, and tests switch correctly.
- No data from another session leaks into the selected session.

## Error And Empty State Flows

### 22. No Session Send

Steps:

1. Load app before creating a session.
2. Try to send a message if UI allows it.

Assertions:

- App creates a session automatically or blocks clearly.
- No fatal error appears.

### 23. Backend API Error Visibility

Steps:

1. Configure fake endpoint to make save or patch API return an error.
2. Trigger the action from UI.

Assertions:

- User-visible error appears.
- Relevant buttons re-enable after failure.
- UI state does not falsely show success.

## Playwright Commands

Install:

```powershell
npm init playwright@latest
npm install
npx playwright install
```

Start local E2E backend:

```powershell
$env:SGV2_E2E_MODE="1"
$env:SGV2_E2E_FAKE_AGENT="1"
$env:SGV2_E2E_FAKE_TEST_RUNNER="1"
$env:SGV2_SKILL_STORE="local"
$env:SGV2_SESSION_DIR=".e2e-sessions"
$env:FOUNDRY_PROJECT_ENDPOINT="https://example.test/foundry"
$env:FOUNDRY_MODEL="e2e-fake-model"
uv run python -m uvicorn backend.main:app --port 6274
```

Run tests:

```powershell
npx playwright test
```

Debug:

```powershell
npx playwright test --headed
npx playwright test --debug
```

## Recommended Implementation Order

1. Add `SGV2_E2E_MODE` and test-only reset/seed endpoints.
2. Add `FakeE2EAgent`.
3. Add fake selection test runner switch.
4. Add `.e2e-sessions/` to `.gitignore`.
5. Add `data-testid` attributes to high-value controls.
6. Add Playwright config and helper functions.
7. Implement app boot and new session test.
8. Implement question-card tests.
9. Implement new skill happy path.
10. Implement patch review tests.
11. Implement modify existing tests.
12. Implement tests tab and session persistence tests.

## Exit Criteria

The E2E suite is useful when it can prove:

- A user can create a new skill locally from chat.
- The checklist and question-card flows handle common and messy user behavior.
- A user can modify a draft through conversation and patch review.
- A user can modify an existing local skill.
- Files, checklist, tests, conversation, session dropdown, and backend session
  state stay consistent.
- No test depends on real LLM output or cloud availability.
