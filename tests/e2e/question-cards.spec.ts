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
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && /\/(tool-result|save|test|chat)$/.test(request.url())) mutations.push(request.url());
  });
  await page.reload();
  await page.getByTestId("session-list").selectOption(session.id);
  const history = page.getByTestId("history-card").filter({ hasText: "Use deterministic draft" });
  await expect(history).toHaveCount(1);
  await expect(history).toContainText("Add this custom note.");
  await expect(history.locator("input, button, textarea")).toHaveCount(0);
  await page.evaluate(() => (window as any).__sgv2.renderLoadedSessionContent());
  await expect(history).toHaveCount(1);
  expect((await readSession(request, session.id)).ui_history).toEqual(session.ui_history);
  expect(mutations).toEqual([]);
  await page.screenshot({ path: test.info().outputPath("resumed-history-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(history).toBeVisible();
  expect(await history.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("resumed-history-mobile.png") });
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

test("notification confirmation continues and enables saving an unchanged source", async ({ page, request }) => {
  await openApp(page);
  await startNewSession(page);
  await expect(page.getByTestId("send-button")).toBeEnabled();
  const payloads: Array<{ auto_continue?: boolean; message: string }> = [];
  let saved = false;
  let queuedSession: Record<string, any>;
  await page.route("**/api/sessions/*/chat", async (route) => {
    payloads.push(route.request().postDataJSON());
    const sessionId = new URL(route.request().url()).pathname.split("/")[3];
    queuedSession = await readSession(request, sessionId);
    queuedSession.current_stage = "refine";
    queuedSession.remote_skill_id = "source";
    queuedSession.remote_version_hash = "saved-version";
    queuedSession.current_skill = { skill_md: "---\nname: source\ndescription: Source\n---\nBody", version_hash: "saved-version", script: null };
    queuedSession.neighbor_notice_outcomes = [{
      skill_key: "target", skill_name: "target", target_user: "owner@example.com",
      status: "pending", notice_id: null, message: "Notification queued; source save required.",
    }];
    const call = payloads.length === 1
      ? { call_id: "queue-notice", tool: "confirm_neighbor_notification", args: { skill_name: "target", notify: true } }
      : { call_id: "wait-save", tool: "ask_user_input", args: { question: "The notification is queued, not sent.", options: ["Save the source", "Do not notify"] } };
    queuedSession.pending_tool_calls = payloads.length === 1 ? [] : [call];
    await route.fulfill({ json: { events: [
      { event: "tool_call", data: call },
      { event: "state_update", data: queuedSession },
      { event: "done", data: {} },
    ] } });
  });
  await page.route("**/api/sessions/*/draft", async (route) => {
    await route.fulfill({ json: queuedSession });
  });
  await page.route("**/api/sessions/*/save", async (route) => {
    saved = true;
    queuedSession.neighbor_notice_outcomes[0] = {
      ...queuedSession.neighbor_notice_outcomes[0], status: "sent", notice_id: 42,
      message: "Notification sent to owner@example.com.",
    };
    await route.fulfill({ json: queuedSession });
  });

  await sendChat(page, "Notify the selected neighbor creator.");

  await expect(page.getByTestId("question-card")).toContainText("queued, not sent");
  expect(payloads).toHaveLength(2);
  expect(payloads[1].auto_continue).toBe(true);
  expect(payloads[1].message).toContain("Neighbor Notification Outcomes");
  const saveButton = page.getByTestId("accept-draft-button");
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  await expect(page.getByText("Notification sent to owner@example.com.", { exact: true })).toBeVisible();
  expect(saved).toBe(true);
  await expect(saveButton).toBeDisabled();
});
