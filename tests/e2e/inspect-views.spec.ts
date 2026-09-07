import { expect, test } from "@playwright/test";
import { openApp, openTab } from "./helpers/app";
import { resetE2E } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("shows inspect details and interactive agent graph", async ({ page }) => {
  // Inspect data is public and does not require a session. Stub only the UI
  // auth-status gate so this test stays focused on graph rendering.
  await page.route("**/api/auth/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, profile: { email: "e2e@example.com" } }),
    });
  });
  await openApp(page);
  await page.evaluate(() => (window as any).__sgv7?.showLoginGate?.(false));

  await openTab(page, "inspect");
  await expect(page.getByTestId("tab-inspect")).toHaveClass(/active/);
  await expect(page.locator("#inspectContent")).toContainText("Current Stage Detail");
  await expect(page.locator("#inspectContent")).toContainText("Output Rules");

  await openTab(page, "agent");
  await expect(page.getByTestId("tab-agent")).toHaveClass(/active/);
  await expect(page.getByTestId("agent-graph")).toBeVisible();
  await expect(page.locator("#agentGraph canvas").first()).toBeVisible();
  await expect(page.getByTestId("agent-graph-details")).toContainText("Blue In edges are shown only to explain how other stages return here");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Data Lineage");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Explanation only");
  const dataLineage = page.getByTestId("agent-graph-details").locator(".graph-prompt-details");
  await expect(dataLineage).toHaveCount(1);
  await expect(dataLineage).not.toHaveAttribute("open", "");
  await expect(page.getByTestId("agent-graph-details")).toContainText("User request and materials");
  await expect(page.getByTestId("agent-graph-details")).toContainText("01_prepare.md");
  await expect(page.getByTestId("agent-graph-details")).toContainText("PREPARE -> DRAFT");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Current stage");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Per-turn Message Assembly");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Built by build_system_prompt(session)");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("High-level order sent to the Foundry Agent for this stage.");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Built by _build_foundry_user_prompt(...)");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Prompt Details");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Fixed Prompt Files");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Only blocks eligible for this stage and the current session.");
  await expect(page.getByTestId("agent-graph-details")).toContainText('1 · role="system"');
  await expect(page.getByTestId("agent-graph-details")).toContainText("Global System");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Current Stage Prompt");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Eligible Dynamic Context");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Outgoing Allowed Stage Transitions");
  await expect(page.getByTestId("agent-graph-details")).toContainText('2 · role="user"');
  await expect(page.getByTestId("agent-graph-details")).toContainText("Turn instruction");
  await expect(page.getByTestId("agent-graph-details")).toContainText("JSON output shape");
  await expect(page.getByTestId("agent-graph-details")).toContainText("Output and orchestration rules");
  const messageFlows = page.getByTestId("agent-graph-details").locator(".graph-message-flow");
  await expect(messageFlows).toHaveCount(2);
  await expect(messageFlows.nth(0).locator(".graph-assembly-steps strong")).toHaveText([
    "Global System",
    "Current Stage Prompt",
    "Writing Best Practices",
    "Skill Format Spec",
    "Eligible Dynamic Context",
    "Outgoing Allowed Stage Transitions",
  ]);
  await expect(messageFlows.nth(1).locator(".graph-assembly-steps strong")).toHaveText([
    "Turn instruction",
    "JSON output shape",
    "Available tools",
    "Output and orchestration rules",
    "Current session snapshot",
    "Latest user message",
  ]);
  const systemStepDetails = messageFlows.nth(0).locator(".graph-assembly-step");
  const userStepDetails = messageFlows.nth(1).locator(".graph-assembly-step");
  await expect(systemStepDetails).toHaveCount(6);
  await expect(userStepDetails).toHaveCount(6);
  await systemStepDetails.nth(0).locator("summary").click();
  await expect(systemStepDetails.nth(0).locator("pre")).toBeVisible();
  await expect(systemStepDetails.nth(0).locator("pre")).toContainText("Skill Generator v2 - Global System");
  await systemStepDetails.nth(1).locator("summary").click();
  await expect(systemStepDetails.nth(1).locator("pre")).toContainText("Stage: PREPARE");
  await systemStepDetails.nth(4).locator("summary").click();
  await expect(systemStepDetails.nth(4).locator("pre")).toContainText("Runtime State");
  await systemStepDetails.nth(5).locator("summary").click();
  await expect(systemStepDetails.nth(5).locator("pre")).toContainText("Allowed Stage Transitions");
  await userStepDetails.nth(1).locator("summary").click();
  await expect(userStepDetails.nth(1).locator("pre")).toContainText("tool_calls");
  await userStepDetails.nth(4).locator("summary").click();
  await expect(userStepDetails.nth(4).locator("pre")).toContainText('"current_stage": "prepare"');

  const graphResizer = page.getByTestId("agent-graph-resizer");
  await expect(graphResizer).toBeVisible();
  const detailsBefore = await page.getByTestId("agent-graph-details").boundingBox();
  const resizerBox = await graphResizer.boundingBox();
  expect(detailsBefore).not.toBeNull();
  expect(resizerBox).not.toBeNull();
  await page.mouse.move(resizerBox!.x + resizerBox!.width / 2, resizerBox!.y + 80);
  await page.mouse.down();
  await page.mouse.move(resizerBox!.x - 80, resizerBox!.y + 80, { steps: 4 });
  await page.mouse.up();
  const detailsAfter = await page.getByTestId("agent-graph-details").boundingBox();
  expect(detailsAfter).not.toBeNull();
  expect(detailsAfter!.width).toBeGreaterThan(detailsBefore!.width + 40);
  await expect.poll(async () => page.evaluate(() => localStorage.getItem("skill_generator_v2_agent_graph_split"))).not.toBeNull();
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("<!-- Missing:");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("This stage has no transition");
  await expect(page.getByTestId("agent-graph-details")).not.toContainText("Related Runtime Info");
  await page.getByTestId("fit-agent-graph-button").click();
  await page.getByTestId("fullscreen-agent-graph-button").click();
  await expect(page.locator("#agentGraphContent")).toHaveClass(/fullscreen/);
  await expect(page.getByTestId("fullscreen-agent-graph-button")).toHaveText("Exit fullscreen");
  await page.keyboard.press("Escape");
  await expect(page.locator("#agentGraphContent")).not.toHaveClass(/fullscreen/);
});
