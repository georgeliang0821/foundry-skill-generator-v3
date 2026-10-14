import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab, sendChat } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

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

test("shows a recoverable error when a patch anchor is missing", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await setScenario(request, "patch_failure");

  await sendChat(page, "Please propose a patch that will fail.");
  await page.getByRole("button", { name: "Accept patch" }).click();

  await expect(page.getByTestId("chat-stream")).toContainText("Patch was not applied");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).not.toHaveValue(/patched text/);
});

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
