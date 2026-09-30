import { readFileSync } from "node:fs";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openApp, openTab, sendChat, startNewSession } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

// The golden script-form fixture: it lints clean, so a save gets as far as the EAA flag check.
const NAME = "ms-graph-room-finder";
const SKILL_MD = readFileSync(`skills/${NAME}/SKILL.md`, "utf8");
const SCRIPT = readFileSync(`skills/${NAME}/scripts/${NAME}.py`, "utf8");

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

async function openScriptSkill(page: Page, request: APIRequestContext) {
  await seedSkill(request, NAME, undefined, { skill_md: SKILL_MD, script: SCRIPT });
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);
  await page.getByTestId("mode-select").selectOption("modify");
  const selector = page.getByTestId("target-skill-select");
  await expect(selector.locator(`option[value='${NAME}']`)).toContainText("[script]");
  await expect(selector.locator("option[value='e2e-existing-skill']")).not.toContainText("[script]");
  await selector.selectOption(NAME);
  await startNewSession(page, "modify");
  await expect(page.getByTestId("skill-binding-status")).toContainText(NAME);
}

test("a script skill shows its badge and a read-only script next to SKILL.md", async ({ page, request }) => {
  await openScriptSkill(page, request);
  await expect(page.getByTestId("binding-script-badge")).toBeVisible();

  await openTab(page, "files");
  const editor = page.getByTestId("file-editor");
  await expect(page.getByTestId("file-tab-script")).toHaveText(`scripts/${NAME}.py`);
  await expect(editor).toHaveValue(new RegExp(`name: ${NAME}`));
  await expect(editor).toHaveJSProperty("readOnly", false);

  await page.getByTestId("file-tab-script").click();
  await expect(editor).toHaveJSProperty("readOnly", true);
  await expect(editor).toHaveValue(/add_help=False/);
  await expect(page.getByTestId("script-preview")).toContainText("ArgumentParser");

  await page.getByTestId("file-tab-skill").click();
  await expect(editor).toHaveJSProperty("readOnly", false);
  await expect(editor).toHaveValue(new RegExp(`name: ${NAME}`));
});

test("an inline skill has no script tab or badge", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);
  await page.getByTestId("mode-select").selectOption("modify");
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await startNewSession(page, "modify");
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-existing-skill");

  await expect(page.getByTestId("binding-script-badge")).toHaveCount(0);
  await openTab(page, "files");
  await expect(page.getByTestId("file-switch")).toBeHidden();
});

test("saving while the EAA script flags are off explains why and saves once they are on", async ({ page, request }) => {
  await openScriptSkill(page, request);
  await openTab(page, "files");
  const editor = page.getByTestId("file-editor");
  await editor.fill(`${(await editor.inputValue()).trimEnd()}\n\nE2E edit.\n`);

  await page.getByTestId("accept-draft-button").click();
  const refusal = page.locator(".failed-status", { hasText: "Skill was not saved" });
  await expect(refusal).toContainText("SKILL_SCRIPTS_ENABLED");
  await expect(refusal).not.toContainText("inline");
  await expect(page.locator(".conversation-status", { hasText: `Saved ${NAME}` })).toHaveCount(0);

  await setScenario(request, "script_flags_on");
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByText(`Saved ${NAME} to the skill store.`)).toBeVisible();
  const session = await readSession(request, await currentSessionId(page));
  expect(session.current_skill.skill_md).toContain("E2E edit.");
});

test("test results list the scripts the router requested", async ({ page, request }) => {
  await openScriptSkill(page, request);
  const sessionId = await currentSessionId(page);
  const updated = await request.post(`/api/sessions/${sessionId}/samples`, {
    data: { positive: ["Find a room in CLS", "Find a room with a projector"], negative: [{ query: "Book room 5" }] },
  });
  await page.evaluate((state) => {
    const win = window as unknown as { __sgv2: { session: unknown; renderSession: () => void } };
    win.__sgv2.session = state;
    win.__sgv2.renderSession();
  }, await updated.json());

  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();
  const results = page.getByTestId("test-results");
  await expect(results.getByTestId("requested-scripts")).toHaveCount(2);
  await results.locator("details.sample-result").first().evaluate((node: HTMLDetailsElement) => { node.open = true; });

  const requested = results.getByTestId("requested-script");
  await expect(requested.first()).toContainText(`scripts/${NAME}.py`);
  await expect(requested.first()).toContainText("valid");
  await expect(requested.nth(1)).toContainText("invalid");
  await expect(requested.nth(1)).toContainText("`--e2e-undeclared` is not declared by the script");
});

test("a code material shows its form and the conditions it still misses", async ({ page, request }) => {
  await openApp(page);
  await startNewSession(page);
  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-kind").selectOption("code");
  await expect(page.getByTestId("material-kind-hint")).toContainText("scripts/<name>.py");
  await page.getByTestId("material-input").fill(SCRIPT);
  await page.getByTestId("attach-material-button").click();

  // Default fake ACA lookup: the flags are off, which is the only reason worth showing.
  await openTab(page, "materials");
  await expect(page.getByTestId("material-form-badge")).toHaveText("inline");
  const unmet = page.getByTestId("skill-form-unmet");
  await expect(unmet).toContainText("eaa_flags");
  await expect(unmet).not.toContainText("covers_operations");

  await openTab(page, "checklist");
  const row = page.getByTestId("skill-form-row");
  await expect(row.getByTestId("skill-form-state")).toHaveText("inline");
  await expect(row.getByTestId("script-covers-checkbox")).toHaveCount(0);
});

test("a confirmed code material becomes the bundled script at DRAFT", async ({ page, request }) => {
  await setScenario(request, "script_flags_on");
  await openApp(page);
  await startNewSession(page);
  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-kind").selectOption("code");
  await page.getByTestId("material-input").fill(SCRIPT);
  await page.getByTestId("attach-material-button").click();

  await openTab(page, "materials");
  await expect(page.getByTestId("skill-form-unmet")).toContainText("covers_operations");
  await expect(page.getByTestId("skill-form-unmet")).not.toContainText("eaa_flags");

  await openTab(page, "checklist");
  const row = page.getByTestId("skill-form-row");
  await row.getByTestId("script-covers-checkbox").check();
  await page.locator("[data-var-save]").click();
  await expect.poll(async () => (await readSession(request, await currentSessionId(page))).prepare_brief.script_covers_operations).toBe(true);

  await sendChat(page, "Create a deterministic E2E calendar skill.");
  await expect(page.getByTestId("draft-card")).toBeVisible();
  await expect(page.getByTestId("draft-script-note")).toContainText("scripts/e2e-calendar-skill.py");
  await expect(page.getByTestId("binding-script-badge")).toBeVisible();

  await openTab(page, "checklist");
  await expect(page.getByTestId("skill-form-row").getByTestId("skill-form-state")).toHaveText("script · locked");
  await expect(page.getByTestId("script-covers-checkbox")).toBeDisabled();

  await openTab(page, "files");
  await page.getByTestId("file-tab-script").click();
  await expect(page.getByTestId("file-editor")).toHaveValue(/add_help=False/);
});

test("an accepted material patch replaces the code material and marks it as not run", async ({ page, request }) => {
  await setScenario(request, "material_patch");
  await openApp(page);
  await startNewSession(page);
  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-kind").selectOption("code");
  await page.getByTestId("material-input").fill(
    'import requests\n\nROOM_ID = "room-1"\nresponse = requests.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)\nprint(response.text)\n',
  );
  await page.getByTestId("attach-material-button").click();

  await sendChat(page, "Can this ship as a script?");
  const card = page.getByTestId("material-patch-card");
  await expect(card).toBeVisible();
  await expect(card.locator(".patch-diff")).toContainText("--room-id");
  await card.getByRole("button", { name: "Accept edit" }).click();
  await expect(card).toContainText("Accepted");

  const session = await readSession(request, await currentSessionId(page));
  const material = session.materials[0];
  expect(material.origin).toBe("agent_patch");
  expect(material.content).toContain('parser.add_argument("--room-id", default="")');
  expect(material.user_content).toContain('ROOM_ID = "room-1"');
  await openTab(page, "materials");
  await expect(page.getByTestId("material-origin-badge")).toHaveText(/edited by agent/i);
});
