import { expect, test } from "@playwright/test";
import { openApp, openTab, sendChat, startNewSession } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

test.use({ viewport: { width: 1504, height: 768 } });

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("loads and modifies an existing local skill", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await setScenario(request, "modify_existing");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(page.getByTestId("target-skill-select")).toBeVisible();
  await expect(
    page.getByTestId("target-skill-select").locator("optgroup[label='Capability'] > option[value='e2e-existing-skill']"),
  ).toHaveCount(1);
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("capability");
  await startNewSession(page, "modify");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/e2e-existing-skill/);

  await sendChat(page, "Prepare this existing skill for modification.");
  await sendChat(page, "Modify this existing skill.");
  await expect(page.locator(".conversation-status").last()).toContainText("waiting for your confirmation");
  await expect(page.locator(".conversation-status").last()).toContainText("Skill content unchanged");
  await page.getByTestId("patch-card").last().getByRole("button", { name: "Accept patch" }).click();
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-existing-skill");
  expect(session.current_skill.skill_md).toContain("E2E refined selection boundary");
});

test("picks up the scenario kind from the selected skill", async ({ page, request }) => {
  await seedSkill(request, "e2e-leave-workflow", "scenario");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(
    page.getByTestId("target-skill-select").locator("optgroup[label='Scenario (orchestration)'] > option[value='e2e-leave-workflow']"),
  ).toHaveCount(1);
  // Kind is an output here, so it stays blank until a skill decides it.
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("");
  await page.getByTestId("target-skill-select").selectOption("e2e-leave-workflow");
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("scenario");

  // Regression: the disabled Kind select used to be posted as-is, so starting a
  // modify session on a scenario skill failed the backend's mismatch check.
  await startNewSession(page, "modify");
  // The session id still shows the boot session until the new one renders.
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-leave-workflow");
  expect(session.skill_kind).toBe("scenario");
  expect(session.children).toEqual(["hr-leave-system"]);
});

test("keeps the header controls on one row in modify mode", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(page.getByTestId("target-skill-select")).toBeVisible();

  // The Skill group only exists in modify mode, so this is where the row used to wrap.
  const mode = await page.getByTestId("mode-select").boundingBox();
  const newSession = await page.getByTestId("new-session-button").boundingBox();
  expect(Math.abs((mode?.y ?? 0) - (newSession?.y ?? 0))).toBeLessThan(8);
});

test("keeps form conversion compact and gives the conversation the remaining height", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await startNewSession(page, "modify");
  await expect(page.getByTestId("send-button")).toHaveText("Send");

  const card = page.getByTestId("form-convert-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Inline");
  await expect(card.getByTestId("form-convert-card-button")).toBeVisible();
  const cardBox = await card.boundingBox();
  const chatBox = await page.getByTestId("chat-stream").boundingBox();
  expect(cardBox!.height).toBeLessThan(64);
  expect(chatBox!.height).toBeGreaterThan(cardBox!.height * 2);
  await expect(page.getByTestId("message-input")).toBeInViewport();
});

test("shows skill loading and environment lookup separately with a lasting result", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  let releaseCreate!: () => void;
  let releaseEnvironment!: () => void;
  const createGate = new Promise<void>((resolve) => { releaseCreate = resolve; });
  const environmentGate = new Promise<void>((resolve) => { releaseEnvironment = resolve; });
  await page.route("**/api/sessions", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await createGate;
    await route.continue();
  });
  await page.route("**/api/sessions/*/aca-env", async (route) => {
    const response = await route.fetch();
    const state = await response.json();
    await environmentGate;
    await route.fulfill({ json: { ...state, aca_env_result: null, aca_env_error: "Lookup unavailable" } });
  });

  await page.getByTestId("new-session-button").click();
  await expect(page.locator("#llmStatus")).toContainText("Loading session and skill");
  await expect(page.getByTestId("send-button")).toHaveText("Loading...");
  releaseCreate();
  const activity = page.locator(".conversation-status.running");
  await expect(activity).toContainText("Reading ACA environment settings");
  await expect(page.getByText("Tell me what skill to build")).toHaveCount(0);
  await expect(page.getByTestId("form-convert-card-button")).toBeDisabled();
  await page.screenshot({ path: "test-results/modify-loading-desktop.png" });
  releaseEnvironment();
  const result = page.locator(".conversation-status.failed-status").last();
  await expect(result).toContainText("Loaded skill: e2e-existing-skill");
  await expect(result).toContainText("Skill content unchanged");
  await expect(result).toContainText("Lookup unavailable");
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await expect(page.getByTestId("form-convert-card-button")).toBeEnabled();
  await expect(page.locator("#llmStatus")).toBeHidden();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".chat-panel").scrollIntoViewIfNeeded();
  await expect(page.getByTestId("form-convert-card")).toBeVisible();
  expect((await page.getByTestId("form-convert-card").boundingBox())!.height).toBeLessThan(96);
  await expect(page.getByTestId("send-button")).toBeInViewport();
  await page.screenshot({ path: "test-results/modify-completed-mobile.png", fullPage: true });
});

test("reports ready environment settings when loading and resuming a skill", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await setScenario(request, "script_flags_on");
  await openApp(page);
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await startNewSession(page, "modify");
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await expect(page.locator(".conversation-status").last()).toContainText("ACA environment settings ready");
  const firstSession = await currentSessionId(page);
  await startNewSession(page, "modify");
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await page.getByTestId("session-list").selectOption(firstSession);
  await expect(page.locator(".conversation-status").last()).toContainText("Session resumed. Skill content unchanged. ACA environment settings ready");
  await expect(page.getByTestId("send-button")).toHaveText("Send");
});

test("keeps session creation failures visible and restores the controls", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await page.route("**/api/sessions", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({ status: 503, json: { detail: "Skill store unavailable" } });
  });
  await page.getByTestId("new-session-button").click();
  await expect(page.locator(".conversation-status.failed-status").last()).toContainText("Could not start session");
  await expect(page.locator(".conversation-status.failed-status").last()).toContainText("Skill store unavailable");
  await expect(page.getByTestId("send-button")).toHaveText("Send");
  await expect(page.getByTestId("new-session-button")).toBeEnabled();
  await expect(page.locator("#llmStatus")).toBeHidden();
});

for (const outcome of ["empty", "response", "event-error", "http-error"] as const) {
  test(`shows model progress before the ${outcome} response and preserves its outcome`, async ({ page, request }) => {
    await seedSkill(request, "e2e-existing-skill");
    await openApp(page);
    await page.getByTestId("mode-select").selectOption("modify");
    await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
    await startNewSession(page, "modify");
    await expect(page.getByTestId("send-button")).toHaveText("Send");
    const state = await readSession(request, await currentSessionId(page));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/sessions/*/chat", async (route) => {
      await gate;
      if (outcome === "http-error") {
        await route.fulfill({ status: 503, json: { detail: "Service unavailable" } });
        return;
      }
      const events = outcome === "event-error"
        ? [{ event: "error", data: { message: "Model unavailable" } }]
        : outcome === "response"
          ? [{ event: "text_delta", data: { delta: "Ready to discuss your changes." } }]
          : [];
      await route.fulfill({ json: { events: [...events, { event: "done", data: {} }] } });
    });
    await page.getByTestId("message-input").fill("Review this skill.");
    await page.getByTestId("send-button").click();
    const activity = page.locator(".conversation-status.running").last();
    await expect(activity).toContainText("Model processing your request");
    await expect(page.locator("#llmStatus")).toBeVisible();
    await expect(page.getByTestId("send-button")).toHaveText("Running...");
    await expect(activity).toContainText(/\([1-9]\d*s\)/);
    if (outcome === "empty") {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator(".chat-panel").scrollIntoViewIfNeeded();
      await expect(activity).toBeVisible();
      await expect(page.getByTestId("send-button")).toBeInViewport();
      await page.screenshot({ path: "test-results/modify-running-mobile.png", fullPage: true });
    }
    release();
    await expect(page.getByTestId("send-button")).toHaveText("Send");
    await expect(page.getByTestId("message-input")).toBeEnabled();
    await expect(page.locator("#llmStatus")).toBeHidden();
    const result = page.locator(".conversation-status").last();
    if (outcome.includes("error")) {
      await expect(result).toHaveClass(/failed-status/);
      await expect(result).toContainText("Model request failed");
    } else {
      await expect(result).toContainText(outcome === "empty" ? "without a response or proposed changes" : "Response received");
      await expect(result).toContainText("Skill content unchanged");
    }
    await page.evaluate(() => new Promise((resolve) => window.setTimeout(resolve, 1600)));
    await expect(result).toBeVisible();
    const after = await readSession(request, await currentSessionId(page));
    expect(after.current_skill).toEqual(state.current_skill);
    if (outcome === "response") await page.screenshot({ path: "test-results/modify-completed-desktop.png" });
  });
}
