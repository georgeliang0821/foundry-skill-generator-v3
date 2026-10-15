import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { openApp, openTab, sendChat, startNewSession } from "./helpers/app";
import { seedSkill, setScenario } from "./helpers/e2eApi";

test("capture Generator user guide", async ({ page, request }) => {
  test.skip(process.env.SGV2_GENERATOR_GUIDE_SCREENSHOTS !== "1", "Opt-in documentation screenshots");
  test.setTimeout(180_000);
  const output = path.resolve("docs/images/generator");
  await mkdir(output, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1050 });
  const capture = async (name: string, target?: ReturnType<Page["locator"]>) => {
    await expect(page.getByTestId("send-button")).toHaveText("Send");
    await expect(page.locator("#appStatus")).toBeHidden();
    await page.evaluate(() => document.fonts.ready);
    if (target) await target.screenshot({ path: path.join(output, `${name}.png`), animations: "disabled" });
    else await page.screenshot({ path: path.join(output, `${name}.png`), animations: "disabled" });
  };
  expect((await request.post("/api/e2e/reset")).ok()).toBeTruthy();
  await openApp(page);
  await startNewSession(page);
  await expect(page.getByTestId("tab-materials")).toHaveClass(/active/);
  await capture("01-workspace");
  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-input").fill("Calendar availability reference\nFind available meeting times for a team. Read calendar availability only; do not create, update or delete events. Return candidate time slots and explain when no slot is available.");
  await page.getByTestId("attach-material-button").click();
  await expect(page.locator("#materialList")).toContainText("Calendar availability reference");
  await openTab(page, "materials");
  await capture("02-materials");
  await sendChat(page, "Create a calendar availability skill. Use the attached reference, accept a time range, and exclude meeting creation.");
  await openTab(page, "checklist");
  await expect(page.getByTestId("checklist")).toContainText("Skill definition");
  await capture("03-checklist");
  await openTab(page, "files");
  await expect(page.getByTestId("draft-card")).toBeVisible();
  await expect(page.getByTestId("file-editor")).toHaveValue(/e2e-calendar-skill/);
  await page.getByTestId("draft-card").scrollIntoViewIfNeeded();
  await capture("04-draft");
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await capture("05-saved");
  await openTab(page, "tests");
  const positive = page.getByPlaceholder("A query that SHOULD hit this skill").first();
  await positive.fill("Use the E2E calendar skill");
  await page.locator('[data-test-rows="negative"] .test-neg-query').first().fill("Tell me a joke");
  await page.getByTestId("save-test-samples-button").click();
  await expect(page.getByRole("button", { name: "Just update the samples" })).toBeVisible();
  await capture("06-save-samples", page.locator("#saveSamplesModal"));
  await page.getByRole("button", { name: "Just update the samples" }).click();
  await expect(positive).toHaveValue("Use the E2E calendar skill");
  await capture("07-test-samples");
  await page.getByTestId("run-tests-button").click();
  await expect(page.getByTestId("test-results")).toContainText("100%");
  await capture("08-test-results");
  await expect(page.getByTestId("patch-card").last()).toBeVisible();
  await openTab(page, "files");
  await page.getByTestId("patch-card").last().getByText("Review diff").click();
  await page.getByTestId("patch-card").last().scrollIntoViewIfNeeded();
  await capture("09-patch-review");
  await setScenario(request, "default");
  await page.getByRole("button", { name: "Accept patch" }).click();
  await expect(page.getByTestId("patch-card").first()).toContainText("Accepted");
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);
  await capture("10-patch-accepted");
  const sessionId = await page.getByTestId("session-list").inputValue();
  await page.reload();
  await expect(page.getByTestId("session-list")).toBeEnabled();
  await page.getByTestId("session-list").selectOption(sessionId);
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);
  await capture("11-resume-session", page.getByRole("banner"));

  await openTab(page, "agent");
  await expect(page.locator("#agentGraph canvas").first()).toBeVisible();
  await page.getByTestId("fit-agent-graph-button").click();
  await page.getByTestId("fullscreen-agent-graph-button").click();
  await capture("12-agent-graph", page.locator("#agentGraphContent"));
  await page.keyboard.press("Escape");

  await setScenario(request, "question_single");
  await startNewSession(page);
  await openTab(page, "materials");
  await sendChat(page, "Help me confirm the next step for this skill.");
  await expect(page.getByTestId("question-card")).toBeVisible();
  await page.getByLabel("Use deterministic draft").check();
  await page.getByTestId("custom-answer").fill("Read availability only; do not create meetings.");
  await page.getByTestId("question-card").scrollIntoViewIfNeeded();
  await capture("13-question-card");
  await page.getByTestId("submit-questions-button").click();
  await expect(page.getByTestId("question-card")).toContainText("Submitted answers");

  await setScenario(request, "default");
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-calendar-skill");
  await expect(page.getByTestId("edit-scope-select")).toHaveValue("refine");
  await capture("14-modify-options", page.getByRole("banner"));
  await startNewSession(page, "modify");
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);
  await capture("15-quick-edit");

  await page.getByTestId("mode-select").selectOption("new");
  await page.getByTestId("skill-kind-select").selectOption("capability");
  await startNewSession(page);
  await openTab(page, "materials");
  await page.getByTestId("asset-dir").selectOption("references");
  await page.locator("#assetFileInput").setInputFiles({
    name: "response-template.txt", mimeType: "text/plain",
    buffer: Buffer.from("Meeting availability summary\nTime range: {{time_range}}\nCandidate slots: {{slots}}\n"),
  });
  await expect(page.getByTestId("asset-row")).toContainText("references/response-template.txt");
  await capture("16-skill-assets");

  expect((await request.post("/api/e2e/reset")).ok()).toBeTruthy();
  expect((await seedSkill(request, "hr-leave-system")).ok()).toBeTruthy();
  await setScenario(request, "scenario_skill_happy_path");
  await openApp(page);
  await expect(page.getByTestId("session-id")).toHaveText(/[a-f0-9]{32}/);
  await expect(page.locator(".conversation-status").last()).toContainText("Session ready");
  await page.getByTestId("skill-kind-select").selectOption("scenario");
  const previousSession = await page.getByTestId("session-id").textContent();
  const scenarioCreated = page.waitForResponse((response) => response.url().endsWith("/api/sessions") && response.request().method() === "POST");
  await page.getByTestId("new-session-button").click();
  expect((await scenarioCreated).ok()).toBeTruthy();
  await expect(page.getByTestId("session-id")).not.toHaveText(previousSession!);
  await expect(page.locator(".conversation-status").last()).toContainText("Session ready");
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("scenario");
  await openTab(page, "checklist");
  await expect(page.getByTestId("delegation-panel")).toBeVisible();
  await page.getByTestId("delegation-row").locator("select").selectOption("hr-leave-system");
  const childrenSaved = page.waitForResponse((response) => response.url().endsWith("/children") && response.request().method() === "POST");
  await page.getByTestId("save-children-button").click();
  expect((await childrenSaved).ok()).toBeTruthy();
  await expect(page.locator("[data-delegation-status]")).toHaveText("");
  await expect(page.getByTestId("delegation-row").locator("select")).toHaveValue("hr-leave-system");
  await page.getByTestId("delegation-panel").scrollIntoViewIfNeeded();
  await capture("17-scenario-delegation");
  expect((await seedSkill(request, "leave-review-workflow", "scenario", {
    skill_md: "---\nname: leave-review-workflow\ndescription: Coordinate employee leave requests through delegated HR operations.\nmetadata:\n  author: e2e\n  version: '1.0'\n  skill_type: scenario-orchestration\n  children: [hr-leave-system]\n---\n\n## Scenario Layer\nThe host checks calendar availability and delegates HR operations.\nChildren may be absent from list_skills; fetch the declared child directly when the workflow needs it.\n\n## Request Flow\nRead the requested dates, then pass the request to the child.\n\n```python\nfetch_skill(skill_name=\"hr-leave-system\", task=\"Process the leave request\")\n```\n\n## Not Applicable\nDo not handle leave approvals or calendar-only requests.\n",
  })).ok()).toBeTruthy();
  await setScenario(request, "default");
  await openApp(page);
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("leave-review-workflow");
  await startNewSession(page, "modify");
  await expect(page.getByTestId("skill-binding-status")).toContainText("leave-review-workflow");
  await openTab(page, "topology");
  await expect(page.getByTestId("topology-results").locator("[data-topology-rule]")).toHaveCount(19);
  await expect(page.getByTestId("topology-results").locator(".topology-rule.failed")).toHaveCount(0);
  await capture("18-topology");

  const scriptName = "ms-graph-room-finder";
  expect((await request.post("/api/e2e/reset")).ok()).toBeTruthy();
  expect((await seedSkill(request, scriptName, "capability", {
    skill_md: readFileSync(`skills/${scriptName}/SKILL.md`, "utf8"),
    script: readFileSync(`skills/${scriptName}/scripts/${scriptName}.py`, "utf8"),
  })).ok()).toBeTruthy();
  await setScenario(request, "script_flags_on");
  await page.evaluate(() => sessionStorage.removeItem("skill_generator_v2_ui_state"));
  await openApp(page);
  await expect(page.locator(".conversation-status").last()).toContainText("Session ready");
  await page.getByTestId("mode-select").selectOption("modify");
  await expect(page.getByTestId("target-skill-select").locator(`option[value='${scriptName}']`)).toHaveCount(1);
  await page.getByTestId("target-skill-select").selectOption(scriptName);
  await page.getByTestId("new-session-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText(scriptName);
  await openTab(page, "files");
  await page.getByTestId("file-tab-script").click();
  await expect(page.getByTestId("file-editor")).toHaveJSProperty("readOnly", true);
  await expect(page.getByTestId("script-preview")).toContainText("ArgumentParser");
  await capture("19-script-file");
});