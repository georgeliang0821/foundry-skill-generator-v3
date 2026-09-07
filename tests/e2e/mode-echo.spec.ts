import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

// A runtime that drops the top-level `mode` echo must take the whole batch down.
// Reporting results anyway would mean scoring an unconfirmed execution mode, and
// a silent upgrade to execute would let a routing test write real data.
test("aborts the batch when the runtime does not echo the mode", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);
  await page.getByTestId("accept-draft-button").click();

  await openTab(page, "tests");
  await page.getByPlaceholder("A query that SHOULD hit this skill").first().fill("Use the E2E calendar skill");
  await page.locator('[data-test-rows="negative"] .test-neg-query').first().fill("Tell me a joke");
  await page.getByTestId("save-test-samples-button").click();
  await page.getByRole("button", { name: "Just update the samples" }).click();
  await expect(page.getByTestId("send-button")).toHaveText("Send");

  await setScenario(request, "mode_echo_missing");
  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();

  const failure = page.locator(".conversation-status.failed-status").last();
  await expect(failure).toContainText("did not echo a top-level");
  await expect(failure).toContainText("aborted");

  // No partial run may be persisted: an aborted batch has nothing to report.
  const session = await readSession(request, await currentSessionId(page));
  expect(session.test_runs).toHaveLength(0);
});
