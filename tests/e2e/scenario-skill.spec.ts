import { expect, test } from "@playwright/test";
import { openApp, openTab, sendChat } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
  await seedSkill(request, "hr-leave-system");
  await setScenario(request, "scenario_skill_happy_path");
});

test("creates, validates, saves, and tests a scenario skill", async ({ page, request }) => {
  await openApp(page);
  await expect(page.getByTestId("session-id")).not.toHaveText("No session");
  await page.getByTestId("skill-kind-select").selectOption("scenario");
  await page.getByTestId("new-session-button").click();
  await expect(page.getByTestId("session-id")).not.toHaveText("No session");

  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-input").fill("Create a leave workflow that delegates HR operations.");
  await page.getByTestId("attach-material-button").click();
  await sendChat(page, "Create a deterministic E2E leave scenario.");

  await openTab(page, "checklist");
  await expect(page.getByTestId("checklist")).toContainText("Delegation confirmed");
  await expect(page.getByTestId("checklist")).not.toContainText("Variables verified");
  await expect(page.getByTestId("delegation-panel")).toBeVisible();
  await expect(page.getByTestId("delegation-row").locator("select")).toHaveValue("hr-leave-system");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/skill_type: scenario-orchestration/);
  await expect(page.getByTestId("file-editor")).toHaveValue(/children:\s+\- hr-leave-system/);

  await openTab(page, "topology");
  await expect(page.getByTestId("topology-results").locator("[data-topology-rule]")).toHaveCount(21);
  await expect(page.getByTestId("topology-results").locator(".topology-rule.failed")).toHaveCount(0);

  await openTab(page, "files");
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");

  const sessionId = await currentSessionId(page);
  let session = await readSession(request, sessionId);
  expect(session.skill_kind).toBe("scenario");
  expect(session.children).toEqual(["hr-leave-system"]);
  expect(session.remote_skill_id).toBe("e2e-leave-workflow");

  const skillsResponse = await request.get("/api/skills");
  expect(skillsResponse.ok()).toBeTruthy();
  const skills = await skillsResponse.json();
  expect(skills.find((skill: { name: string }) => skill.name === "hr-leave-system")?.is_internal).toBe(true);

  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();
  await expect(page.getByTestId("test-results")).toContainText("L1 - Static topology (T1-T10)");
  await expect(page.getByTestId("test-results")).toContainText("L2 - Parent absence from skills_referenced");
  await expect(page.getByTestId("test-results")).toContainText("L3 - Child reachability");
  await expect(page.getByTestId("test-results")).toContainText("Not verified by this run.");

  session = await readSession(request, sessionId);
  expect(session.test_runs).toHaveLength(1);
  expect(session.test_runs[0].scenario_layers.map((layer: { layer: string }) => layer.layer)).toEqual(["L1", "L2", "L3"]);
  expect(session.test_runs[0].scenario_layers.every((layer: { passed: boolean }) => layer.passed)).toBe(true);
});

test("owner controls are scenario-only and EAA resolution does not claim execution", async ({ page, request }) => {
  await openApp(page);
  await openTab(page, "checklist");
  await expect(page.getByTestId("scenario-owner-panel")).toHaveCount(0);
  await page.getByTestId("skill-kind-select").selectOption("scenario");
  await page.getByTestId("new-session-button").click();
  await openTab(page, "checklist");
  await expect(page.getByTestId("scenario-owner-select")).toHaveValue("host");
  await page.getByTestId("scenario-owner-select").selectOption("eaa");
  await page.getByTestId("save-owner-button").click();
  await expect(page.getByTestId("scenario-owner-panel")).toContainText("Mode B");
  await sendChat(page, "Create an EAA leave scenario.");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/orchestration_owner: eaa/);
  await expect(page.getByTestId("file-editor")).not.toHaveValue(/fetch_skill\(/);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");
  const sid = await currentSessionId(page);
  await page.reload();
  await page.getByRole("combobox", { name: "Session", exact: true }).selectOption(sid);
  await openTab(page, "checklist");
  await expect(page.getByTestId("scenario-owner-select")).toHaveValue("eaa");
  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();
  await expect(page.getByTestId("test-results")).toContainText("EAA scenario resolution");
  await expect(page.getByTestId("test-results")).toContainText("Child execution (not verified)");
  const session = await readSession(request, sid);
  expect(session.test_runs[0].scenario_layers[2].passed).toBeNull();
});

test("owner conversion confirms before clearing a stored scenario draft", async ({ page, request }) => {
  await seedSkill(request, "e2e-leave-workflow", "scenario");
  const created = await request.post("/api/sessions", {
    data: { mode: "modify", target_skill_id: "e2e-leave-workflow", start_stage: "refine" },
  });
  expect(created.ok()).toBeTruthy();
  const sid = (await created.json()).id;
  await openApp(page);
  await page.getByRole("combobox", { name: "Session", exact: true }).selectOption(sid);
  await openTab(page, "checklist");
  await page.getByTestId("scenario-owner-select").selectOption("eaa");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByTestId("save-owner-button").click();
  await expect(page.getByTestId("scenario-owner-select")).toHaveValue("host");
  expect((await readSession(request, sid)).current_skill.skill_md).not.toBe("");
  await page.getByTestId("scenario-owner-select").selectOption("eaa");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTestId("save-owner-button").click();
  await expect(page.getByTestId("scenario-owner-panel")).toContainText("Reconfirm PREPARE");
  await expect(page.getByTestId("skill-binding-status")).toContainText("unsaved changes");
  let session = await readSession(request, sid);
  expect(session.current_stage).toBe("prepare");
  expect(session.current_skill.skill_md).toBe("");
  expect(session.remote_skill_id).toBe("e2e-leave-workflow");
  await page.reload();
  await page.getByRole("combobox", { name: "Session", exact: true }).selectOption(sid);
  await openTab(page, "checklist");
  await expect(page.getByTestId("scenario-owner-select")).toHaveValue("eaa");
  await expect(page.getByTestId("skill-binding-status")).toContainText("unsaved changes");
  await sendChat(page, "Reconfirm and generate the EAA version.");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/orchestration_owner: eaa/);
  const accepted = page.waitForResponse((response) => response.url().endsWith("/tool-result") && response.request().method() === "POST");
  await page.getByTestId("accept-draft-button").click();
  expect((await accepted).ok()).toBeTruthy();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");
  session = await readSession(request, sid);
  expect(session.owner_regeneration_pending).toBe(false);
});

test("runtime input sources persist without changing authentication fields", async ({ page, request }) => {
  await openApp(page);
  await openTab(page, "checklist");
  const checkpoint = page.locator('[data-checkpoint="variables_ok"]');
  if (await checkpoint.getAttribute("open") === null) {
    await checkpoint.locator(":scope > summary").click();
  }
  const row = page.locator('[data-var-kind="runtime"]').first();
  await expect(row.locator('.var-source option[value="credentials"]')).toHaveText("Credentials (key/value)");
  await expect(row.locator('.var-source option[value="request"]')).toHaveText("Request");
  await row.locator(".var-name").fill("description");
  await row.locator(".var-desc").fill("Complete original report text");
  await row.locator(".var-source").selectOption("request");
  await expect(row.locator(".var-credentials-key")).toBeHidden();
  const savedResponse = page.waitForResponse((response) => response.url().endsWith("/variables") && response.request().method() === "POST");
  await page.locator("[data-var-save]").click();
  expect((await savedResponse).ok()).toBe(true);
  await expect(row.locator(".var-source")).toHaveValue("request");
  const sessionId = await currentSessionId(page);
  const session = await readSession(request, sessionId);
  expect(session.prepare_brief.variables).toContainEqual(expect.objectContaining({
    name: "description", source: "request", credentials_key: "", payload_field: "",
  }));
  const withAuth = await request.post(`/api/sessions/${sessionId}/variables`, { data: {
    variables: [...session.prepare_brief.variables, { name: "API_TOKEN", kind: "obo_token", description: "Service authentication" }],
  } });
  expect(withAuth.ok()).toBe(true);
  await page.reload();
  await page.getByRole("combobox", { name: "Session", exact: true }).selectOption(sessionId);
  await openTab(page, "checklist");
  await expect(page.locator('[data-var-kind="runtime"] .var-source').first()).toHaveValue("request");
  await expect(page.locator('[data-var-kind="obo_token"] .var-source')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  const source = page.locator('[data-var-kind="runtime"] .var-source').first();
  await source.evaluate((element) => element.scrollIntoView({ block: "center" }));
  const bounds = await source.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "test-results/input-sources-mobile.png", fullPage: true });
});