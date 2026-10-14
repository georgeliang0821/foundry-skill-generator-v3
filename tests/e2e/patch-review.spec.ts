import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab, sendChat } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

for (const omitPatchEvent of [false, true]) {
  test(`restores a pending patch from session state (${omitPatchEvent ? "missing event" : "normal event"})`, async ({ page, request }) => {
    await openApp(page);
    await createDraftWithFakeAgent(page);
    await page.getByTestId("accept-draft-button").click();
    await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
    await setScenario(request, "patch_refine");
    await page.route("**/chat", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (omitPatchEvent) {
        body.events = body.events.filter((event: { event: string; data?: { tool?: string } }) =>
          event.event !== "tool_call" || event.data?.tool !== "propose_patch");
      }
      await route.fulfill({ response, json: body });
    });

    await sendChat(page, "Please improve the selection boundary.");
    const card = page.getByTestId("patch-card");
    await expect(card).toHaveCount(1);
    await expect(card.getByRole("button", { name: "Accept patch" })).toBeVisible();
    let session = await readSession(request, await currentSessionId(page));
    expect(session.patch_history).toHaveLength(0);
    expect(session.pending_tool_calls.filter((call: { tool: string }) => call.tool === "propose_patch")).toHaveLength(1);
    await page.evaluate(() => {
      const app = (window as any).__sgv2;
      app.renderSession();
      app.renderSession();
    });
    await expect(card).toHaveCount(1);

    await setScenario(request, "default");
    await card.getByRole("button", { name: "Accept patch" }).click();
    await expect(card).toContainText("Accepted");
    session = await readSession(request, await currentSessionId(page));
    expect(session.patch_history).toHaveLength(1);
    expect(session.conversation.some((message: { metadata?: { patch_sync?: { status: string } } }) =>
      message.metadata?.patch_sync?.status === "saved")).toBe(true);
  });
}

test("accepts and undoes a conversation patch", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await setScenario(request, "patch_refine");

  await sendChat(page, "Please improve the selection boundary.");
  await expect(page.getByTestId("patch-card")).toBeVisible();
  await page.getByRole("button", { name: "Accept patch" }).click();
  // Accepting auto-continues the turn, and the fake agent may propose another patch.
  await expect(page.getByTestId("patch-card").first()).toContainText("Accepted");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);
  let session = await readSession(request, await currentSessionId(page));
  expect(session.patch_history).toHaveLength(1);

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("file-editor")).not.toHaveValue(/E2E refined selection boundary/);
  session = await readSession(request, await currentSessionId(page));
  expect(session.patch_history).toHaveLength(0);
});

for (const retryFails of [false, true]) {
  test(`retires a failed patch before regeneration (${retryFails ? "request fails" : "replacement ready"})`, async ({ page, request }) => {
    await openApp(page);
    await createDraftWithFakeAgent(page);
    await page.getByTestId("accept-draft-button").click();
    await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
    await setScenario(request, "patch_failure");

    await sendChat(page, "Please propose a patch that will fail.");
    const cards = page.getByTestId("patch-card");
    const failedCard = cards.first();
    const failedId = await failedCard.getAttribute("data-call-id");
    const sessionId = await currentSessionId(page);
    const before = (await readSession(request, sessionId)).current_skill;
    await setScenario(request, "patch_refine");
    if (retryFails) {
      await page.route("**/chat", (route) => route.fulfill({
        status: 503, json: { detail: "E2E regeneration unavailable" },
      }));
    }

    await failedCard.getByRole("button", { name: "Accept patch" }).click();
    await expect(failedCard).toContainText("Not applied");
    await expect(failedCard.getByRole("button", { name: "Accept patch" })).toBeDisabled();
    await expect(failedCard.getByRole("button", { name: "Reject", exact: true })).toBeDisabled();
    await expect(failedCard.getByRole("button", { name: "Undo", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("chat-stream")).toContainText("Patch was not applied");
    await expect(page.getByTestId("send-button")).toHaveText("Send");
    if (retryFails) {
      await expect(page.getByTestId("chat-stream")).toContainText("Model request failed");
      await expect(cards).toHaveCount(1);
    } else {
      await expect(cards).toHaveCount(2);
      await expect(cards.last().getByRole("button", { name: "Accept patch" })).toBeEnabled();
      await expect(cards.last()).not.toContainText("Accepted");
    }
    let session = await readSession(request, sessionId);
    expect(session.current_skill).toEqual(before);
    expect(session.patch_history).toHaveLength(0);
    expect(session.pending_tool_calls.some((call: { call_id: string }) => call.call_id === failedId)).toBe(false);
    expect(session.pending_tool_calls.filter((call: { tool: string }) => call.tool === "propose_patch")).toHaveLength(retryFails ? 0 : 1);
    await page.evaluate(() => {
      const app = (window as any).__sgv2;
      app.renderSession();
      app.renderSession();
    });
    await expect(cards).toHaveCount(retryFails ? 1 : 2);
    await expect(failedCard.getByRole("button", { name: "Accept patch" })).toBeDisabled();
    await failedCard.locator("summary").click();
    await expect(failedCard.locator("details")).toHaveAttribute("open", "");

    await page.reload();
    await page.getByTestId("session-list").selectOption(sessionId);
    await expect(page.getByTestId("chat-stream")).toContainText("Patch was not applied");
    await expect(cards).toHaveCount(retryFails ? 0 : 1);
    if (!retryFails) await expect(cards.getByRole("button", { name: "Accept patch" })).toBeEnabled();
    session = await readSession(request, sessionId);
    expect(session.current_skill).toEqual(before);
  });
}

for (const lintFailure of [true, false]) {
  test(`does not report a failed patch sync as saved (${lintFailure ? "lint" : "MCP unavailable"})`, async ({ page, request }) => {
    await openApp(page);
    await createDraftWithFakeAgent(page);
    await page.getByTestId("accept-draft-button").click();
    await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
    await setScenario(request, "patch_refine");
    await sendChat(page, "Please improve the selection boundary.");
    await expect(page.getByTestId("patch-card")).toBeVisible();

    const followups: string[] = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/chat")) followups.push(request.postData() || "");
    });
    await page.route("**/tool-result", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const outcome = body.conversation.at(-1).metadata.patch_sync;
      expect(outcome.status).toBe("saved");
      outcome.status = "failed";
      outcome.detail = lintFailure
        ? { kind: "skill_lint_failed", message: "Skill lint failed", issues: ["D4: reserved secret"] }
        : "EAA skill lint could not run, so the skill was not saved";
      await route.fulfill({ response, json: body });
    });

    await page.getByRole("button", { name: "Accept patch" }).click();
    await expect(page.getByTestId("patch-card").first()).toContainText("Accepted");
    await expect(page.getByTestId("chat-stream")).toContainText("Patch applied locally, but not synced");
    if (lintFailure) {
      await expect(page.getByTestId("chat-stream")).toContainText("The agent has been asked to fix them");
      await expect.poll(() => followups.length).toBe(1);
      expect(followups[0]).toContain("D4: reserved secret");
    } else {
      expect(followups).toHaveLength(0);
    }
    await expect(page.getByTestId("chat-stream")).not.toContainText("Saved to Blob + SQL.");
    await expect(page.getByTestId("chat-stream")).not.toContainText("Requesting the next skill-selection test run");
    expect(followups.join("\n")).not.toContain("Immediately request the next skill-selection test run");
  });
}
