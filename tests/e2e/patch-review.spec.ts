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
  await setScenario(request, "patch_refine");

  await sendChat(page, "Please improve the selection boundary.");
  await expect(page.getByTestId("patch-card")).toBeVisible();
  await page.getByRole("button", { name: "Accept patch" }).click();
  await expect(page.getByTestId("patch-card")).toContainText("Accepted");

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
  await setScenario(request, "patch_failure");

  await sendChat(page, "Please propose a patch that will fail.");
  await page.getByRole("button", { name: "Accept patch" }).click();

  await expect(page.getByTestId("chat-stream")).toContainText("Patch was not applied");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).not.toHaveValue(/patched text/);
});
