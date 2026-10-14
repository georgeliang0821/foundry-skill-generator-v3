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

test("continues automatically after selecting a neighbor", async ({ page, request }) => {
  await openApp(page);
  await startNewSession(page);
  await expect(page.getByTestId("send-button")).toBeEnabled();
  const payloads: Array<{ auto_continue?: boolean; message: string }> = [];
  await page.route("**/api/sessions/*/chat", async (route) => {
    payloads.push(route.request().postDataJSON());
    const sessionId = new URL(route.request().url()).pathname.split("/")[3];
    const session = await readSession(request, sessionId);
    session.current_stage = "refine";
    const call = payloads.length === 1
      ? { call_id: "select-peer", tool: "select_neighbor_skill", args: { skill_name: "peer-selected" } }
      : { call_id: "confirm-peer", tool: "ask_user_input", args: { question: "Confirm the neighbor boundary?", options: ["Apply the boundary", "Keep it unchanged"] } };
    session.pending_tool_calls = payloads.length === 1 ? [] : [call];
    await route.fulfill({
      json: { events: [
        { event: "tool_call", data: call },
        { event: "state_update", data: session },
        { event: "done", data: {} },
      ] },
    });
  });

  await sendChat(page, "Adjust the selected neighbor.");

  await expect(page.getByTestId("question-card")).toContainText("Confirm the neighbor boundary?");
  expect(payloads).toHaveLength(2);
  expect(payloads[1].auto_continue).toBe(true);
  expect(payloads[1].message).toContain("Neighbor Edit Policy");
  await expect(page.getByTestId("activity-card")).toContainText("peer-selected");
  await expect(page.getByTestId("send-button")).toBeEnabled();
});
