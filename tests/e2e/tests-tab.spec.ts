import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("saves samples and runs deterministic local selection tests", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);
  await page.getByTestId("accept-draft-button").click();

  await openTab(page, "tests");
  const positiveInput = page.getByPlaceholder("A query that SHOULD hit this skill").first();
  const negativeInput = page.locator('[data-test-rows="negative"] .test-neg-query').first();
  await positiveInput.fill("Use the E2E calendar skill");
  await negativeInput.fill("Tell me a joke");
  await page.getByTestId("save-test-samples-button").click();
  await page.getByRole("button", { name: "Just update the samples" }).click();
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await expect(positiveInput).toHaveValue("Use the E2E calendar skill");

  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();
  await expect(page.getByTestId("test-results")).toContainText("Positive");
  await expect(page.getByTestId("test-results")).toContainText("100%");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.test_runs).toHaveLength(1);
  expect(session.test_runs[0].positive_hit_rate).toBe(1);
});
