import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("a draft is repaired before the user accepts it once", async ({ page, request }) => {
  await setScenario(request, "lint_failure");
  await openApp(page);
  await createDraftWithFakeAgent(page);

  const chat = page.getByTestId("chat-stream");
  await expect(chat).toContainText("E2E fake agent fixed the lint findings.");
  await expect(chat).not.toContainText("SKILL.md was not saved");
  await expect(page.getByTestId("draft-card")).toHaveCount(1);
  await expect(page.getByTestId("draft-preflight-status")).toContainText("Validation passed");
  let session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBeFalsy();

  await page.getByRole("button", { name: "Accept SKILL.md" }).click();

  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-calendar-skill");
  expect(session.pending_tool_calls.filter((call: { tool: string }) => call.tool === "propose_skill_draft")).toHaveLength(0);
});

test("repeated lint failure stops after one repair and retains the unsaved draft", async ({ page, request }) => {
  await setScenario(request, "lint_failure");
  let checks = 0;
  await page.route("**/api/sessions/*/draft/preflight", async (route) => {
    checks += 1;
    await route.fulfill({ status: 400, json: { detail: {
      kind: "eaa_lint_failed", issues: ["S3: Required Inputs do not match"],
    } } });
  });
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await expect(page.getByTestId("draft-preflight-status")).toContainText("S3: Required Inputs do not match");
  await expect(page.getByRole("button", { name: "Accept SKILL.md" })).toBeDisabled();
  await expect(page.getByTestId("accept-draft-button")).toBeDisabled();
  expect(checks).toBe(2);
  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBeFalsy();
  expect(session.pending_tool_calls.filter((call: { tool: string }) => call.tool === "propose_skill_draft")).toHaveLength(1);
});

test("warnings are visible but do not require a repair or block acceptance", async ({ page }) => {
  let checks = 0;
  await page.route("**/api/sessions/*/draft/preflight", async (route) => {
    checks += 1;
    await route.fulfill({ json: { warnings: ["S3: verify actual argv calls in execution tests"] } });
  });
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await expect(page.getByTestId("draft-preflight-status")).toContainText("S3: verify actual argv");
  expect(checks).toBe(1);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
});

test("neighbor save lint failures survive reload and request a neighbor-only repair", async ({ page, request }) => {
  for (const name of ["source-skill", "neighbor-skill", "other-skill"]) {
    expect((await seedSkill(request, name)).ok()).toBeTruthy();
  }
  const original = await (await request.get("/api/skills/neighbor-skill")).json();
  const source = await (await request.get("/api/skills/source-skill")).json();
  const created = await request.post("/api/sessions", {
    data: { mode: "modify", target_skill_id: "source-skill" },
  });
  const { id } = await created.json();
  expect((await request.post(`/api/sessions/${id}/neighbors`, {
    data: { neighbor_skills: [{ skill: "neighbor-skill" }] },
  })).ok()).toBeTruthy();
  const edited = original.skill_md + "\n- Task routing -> use `other-skill`\n";
  expect((await request.post(`/api/sessions/${id}/neighbor-edits/neighbor-skill/propose`, {
    data: { skill_md: edited, label: "Accepted boundary edit" },
  })).ok()).toBeTruthy();
  let repairMessage = "";
  await page.route(`**/api/sessions/${id}/chat`, async (route) => {
    repairMessage = route.request().postDataJSON().message;
    const state = await readSession(request, id);
    await route.fulfill({ json: { events: [
      { event: "text_delta", data: { delta: "Neighbor repair requested; review its patch before saving." } },
      { event: "state_update", data: state },
    ] } });
  });
  await openApp(page);
  await page.getByTestId("session-list").selectOption(id);
  const openNeighborCard = async () => {
    await openTab(page, "checklist");
    const checkpoint = page.locator('[data-checkpoint="routing_uniqueness_confirmed"]');
    if (await checkpoint.getAttribute("open") === null) await checkpoint.locator(":scope > summary").click();
    const panel = checkpoint.locator(".routing-subpanel").filter({ has: page.locator("[data-neighbor-edits]") });
    if (await panel.getAttribute("open") === null) await panel.locator(":scope > summary").click();
    return checkpoint.locator('[data-nbedit="neighbor-skill"]');
  };
  let card = await openNeighborCard();
  await card.getByRole("button", { name: "Save skill", exact: true }).click();
  await expect(page.getByTestId("chat-stream")).toContainText("Save failed for neighbor skill");
  await expect.poll(() => repairMessage).toContain("propose_neighbor_edit");
  expect(repairMessage).toContain("neighbor skill 'neighbor-skill'");
  expect(repairMessage).toContain("currently SELECTED");
  expect(repairMessage).toContain("Do not use propose_patch or propose_skill_draft");
  let state = await readSession(request, id);
  expect(state.neighbor_edits[0].status).toBe("draft");
  expect(state.neighbor_notice_outcomes).toHaveLength(0);
  expect((await (await request.get("/api/skills/source-skill")).json()).skill_md).toBe(source.skill_md);
  expect((await (await request.get("/api/skills/neighbor-skill")).json()).skill_md).toBe(original.skill_md);

  await page.reload();
  await page.getByTestId("session-list").selectOption(id);
  card = await openNeighborCard();
  await expect(card.locator("[data-nbedit-statustext]")).toContainText("N1:");
  await expect(card.locator("[data-nbedit-textarea]")).toHaveValue(edited);
  await expect(page.getByTestId("chat-stream")).toContainText("Save failed for neighbor skill");

  await card.locator("[data-nbedit-textarea]").fill(original.skill_md + "\nDo not use for unrelated task routing.\n");
  await card.getByRole("button", { name: "Save as new version" }).click();
  await expect(card.locator("[data-nbedit-statustext]")).toBeEmpty();
  await card.getByRole("button", { name: "Save skill", exact: true }).click();
  await expect(card.locator(".nbedit-status")).toHaveText("synced");
  state = await readSession(request, id);
  expect(state.conversation.at(-1).metadata.neighbor_save.status).toBe("saved");
  expect(state.neighbor_notice_outcomes).toHaveLength(0);
  expect((await (await request.get("/api/skills/source-skill")).json()).skill_md).toBe(source.skill_md);
});

test("neighbor service and conflict failures show their blocker without requesting content repairs", async ({ page, request }) => {
  expect((await seedSkill(request, "neighbor-skill")).ok()).toBeTruthy();
  const original = await (await request.get("/api/skills/neighbor-skill")).json();
  const { id } = await (await request.post("/api/sessions", { data: { mode: "new", materials: [] } })).json();
  expect((await request.post(`/api/sessions/${id}/neighbor-edits/neighbor-skill/propose`, {
    data: { skill_md: original.skill_md + "\nUpdated boundary.\n" },
  })).ok()).toBeTruthy();
  let chatCalls = 0;
  page.on("request", (req) => {
    if (req.url().endsWith(`/api/sessions/${id}/chat`)) chatCalls += 1;
  });
  await openApp(page);
  await page.getByTestId("session-list").selectOption(id);
  await openTab(page, "checklist");
  const checkpoint = page.locator('[data-checkpoint="routing_uniqueness_confirmed"]');
  if (await checkpoint.getAttribute("open") === null) await checkpoint.locator(":scope > summary").click();
  const panel = checkpoint.locator(".routing-subpanel").filter({ has: page.locator("[data-neighbor-edits]") });
  if (await panel.getAttribute("open") === null) await panel.locator(":scope > summary").click();
  const card = checkpoint.locator('[data-nbedit="neighbor-skill"]');
  for (const blocker of [
    { status: 503, detail: "EAA lint is unavailable; not saved." },
    { status: 409, detail: { kind: "version_conflict", recoverable: true, message: "Neighbor changed on Blob. Reopen it." } },
  ]) {
    await page.route(`**/api/sessions/${id}/neighbor-edits/neighbor-skill/save`, (route) =>
      route.fulfill({ status: blocker.status, json: { detail: blocker.detail } }));
    await card.getByRole("button", { name: "Save skill", exact: true }).click();
    const message = typeof blocker.detail === "string" ? blocker.detail : blocker.detail.message;
    await expect(card.locator("[data-nbedit-statustext]")).toContainText(message);
    await expect(page.getByTestId("chat-stream")).toContainText(message);
    expect(chatCalls).toBe(0);
    await expect(card.locator(".nbedit-status")).toHaveText("unsaved");
  }
});
