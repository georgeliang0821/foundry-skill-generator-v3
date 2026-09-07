import { expect, test } from "@playwright/test";
import { openApp, sendChat, startNewSession } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("submits selected option plus custom answer", async ({ page, request }) => {
  await setScenario(request, "question_single");
  await openApp(page);
  await startNewSession(page);
  await sendChat(page, "Ask me a question.");

  await expect(page.getByTestId("question-card")).toBeVisible();
  await page.getByLabel("Use deterministic draft").check();
  await page.getByTestId("custom-answer").fill("Add this custom note.");
  await page.getByTestId("submit-questions-button").click();

  await expect(page.getByTestId("question-card")).toContainText("Submitted answers");
  await expect(page.getByTestId("question-card")).toContainText("Selected option: Use deterministic draft");
  await expect(page.getByTestId("question-card")).toContainText("Additional input: Add this custom note.");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.pending_tool_calls).toHaveLength(0);
});

test("submits no-answer fallback when the user leaves a question blank", async ({ page, request }) => {
  await setScenario(request, "question_single");
  await openApp(page);
  await startNewSession(page);
  await sendChat(page, "Ask me a question.");

  await page.getByTestId("submit-questions-button").click();

  await expect(page.getByTestId("question-card")).toContainText("(no answer)");
  const session = await readSession(request, await currentSessionId(page));
  expect(session.pending_tool_calls).toHaveLength(0);
});

test("answers pending questions from the chat box", async ({ page, request }) => {
  await setScenario(request, "question_multi");
  await openApp(page);
  await startNewSession(page);
  await sendChat(page, "Ask me two questions.");

  await expect(page.getByTestId("question-item")).toHaveCount(2);
  await sendChat(page, "Use the deterministic draft and the small sample set.");

  await expect(page.getByTestId("question-card")).toContainText("Answered from chat");
  await expect(page.getByTestId("question-card")).toContainText("Use the deterministic draft and the small sample set.");
  const session = await readSession(request, await currentSessionId(page));
  expect(session.pending_tool_calls).toHaveLength(0);
});
