import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("a draft refused by the skill lint is handed back to the agent and the redraft saves", async ({ page, request }) => {
  await setScenario(request, "lint_failure");
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await page.getByRole("button", { name: "Accept SKILL.md" }).click();

  const chat = page.getByTestId("chat-stream");
  await expect(chat).toContainText("SKILL.md was not saved: the skill lint found 1 problem(s)");
  await expect(chat).not.toContainText("Test failed");
  await expect(chat).toContainText("E2E fake agent fixed the lint findings.");
  let session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBeFalsy();

  await page.getByRole("button", { name: "Accept SKILL.md" }).last().click();

  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-calendar-skill");
  expect(session.pending_tool_calls.filter((call: { tool: string }) => call.tool === "propose_skill_draft")).toHaveLength(0);
});
