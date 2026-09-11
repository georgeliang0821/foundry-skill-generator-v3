import { test, expect } from "@playwright/test";
import { openApp, startNewSession, openTab } from "./helpers/app";

test.describe("UI refresh — warm palette, icons, clarity", () => {
  test("inline Heroicons sprite is loaded and the brand mark renders an icon", async ({ page }) => {
    await openApp(page);

    // The inline SVG sprite should expose every <symbol> referenced by the UI.
    const symbolIds = [
      "i-sparkles",
      "i-clipboard-check",
      "i-document-text",
      "i-adjustments",
      "i-beaker",
      "i-check-circle",
      "i-paper-clip",
      "i-check-badge",
      "i-document",
      "i-bug",
      "i-code",
      "i-share",
      "i-arrow-up-right",
      "i-bolt",
      "i-expand",
      "i-collapse",
      "i-arrow-path",
      "i-check",
      "i-warning",
      "i-chat",
      "i-info",
      "i-send",
    ];
    for (const id of symbolIds) {
      await expect(page.locator(`svg symbol#${id}`)).toHaveCount(1);
    }

    // The brand mark should render an icon (gradient sparkles)
    await expect(page.locator(".brand-mark svg.icon")).toHaveCount(1);

    // The send button should also surface an icon now
    await expect(page.getByTestId("send-button").locator("svg.icon")).toHaveCount(1);
  });

  test("every context tab carries an icon and Inspect is visually demoted", async ({ page }) => {
    await openApp(page);

    const tabs = ["materials", "checklist", "files", "tests", "agent", "inspect"];
    for (const t of tabs) {
      const btn = page.getByTestId(`tab-${t}`);
      await expect(btn).toBeVisible();
      await expect(btn.locator("svg.icon")).toHaveCount(1);
    }

    // The Inspect tab is for engineers — verify the demotion marker class.
    await expect(page.getByTestId("tab-inspect")).toHaveClass(/developer-tab/);
    await expect(
      page.getByTestId("tab-inspect").locator(".dev-tag")
    ).toContainText(/dev/i);
  });

  test("the four-phase stepper renders all stages with copy and current step is marked", async ({ page }) => {
    await openApp(page);
    await startNewSession(page);

    const items = page.locator(".stage-item");
    await expect(items).toHaveCount(4);

    // Each step should have a marker and a copy block
    await expect(page.locator(".stage-item .stage-number")).toHaveCount(4);
    await expect(page.locator(".stage-item .stage-copy strong")).toHaveCount(4);

    // First step is active right after starting a new session
    const active = page.locator(".stage-item.active");
    await expect(active).toHaveCount(1);
    await expect(active).toHaveAttribute("aria-current", "step");
    await expect(active).toHaveAttribute("data-phase", "PREPARE");
    // Active step shows an icon, not the plain number
    await expect(active.locator(".stage-number svg.icon")).toHaveCount(1);
  });

  test("the agent graph pane includes an intro legend with five phase swatches", async ({ page }) => {
    await openApp(page);
    await openTab(page, "agent");

    const legend = page.locator("#agentGraphLegend");
    await expect(legend).toBeVisible();
    // Five stage swatches: PREPARE / DRAFT / REFINE / TEST / DONE
    const phaseClasses = [
      ".legend-prepare",
      ".legend-draft",
      ".legend-refine",
      ".legend-test",
      ".legend-done",
    ];
    for (const cls of phaseClasses) {
      await expect(legend.locator(cls)).toHaveCount(1);
    }
    // Outgoing and incoming edge markers
    await expect(legend.locator(".legend-edge-out")).toHaveCount(1);
    await expect(legend.locator(".legend-edge-in")).toHaveCount(1);
  });

  test("warm palette is applied and no element ships with font-weight: 800", async ({ page }) => {
    await openApp(page);

    // Body background should match the warm cream (#fdfbf8 → rgb(253, 251, 248))
    const bodyBg = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor
    );
    expect(bodyBg).toBe("rgb(253, 251, 248)");

    // Primary action (Send) should use the brand orange #c4663a → rgb(196, 102, 58)
    const sendBg = await page
      .getByTestId("send-button")
      .evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(sendBg).toBe("rgb(160, 79, 42)");

    // No visible element should keep the old extra-bold 800 weight
    const has800 = await page.evaluate(() => {
      const all = Array.from(document.querySelectorAll<HTMLElement>("*"));
      return all.some((el) => {
        if (!el.offsetParent && el !== document.body) return false;
        return getComputedStyle(el).fontWeight === "800";
      });
    });
    expect(has800).toBe(false);
  });

  test("empty states surface icon + headline + description for clarity", async ({ page }) => {
    await openApp(page);
    await startNewSession(page);
    await openTab(page, "checklist");

    // Checklist empty state should now have icon + headline + description
    const empty = page.locator('[data-context-pane="checklist"] .empty-state');
    await expect(empty.locator("svg.icon")).toHaveCount(1);
    await expect(empty.locator("strong")).toBeVisible();
    await expect(empty.locator("em")).toBeVisible();
  });
  test("session id pill is hidden from users but kept in the DOM", async ({ page }) => {
    await openApp(page);
    await startNewSession(page);
    const pill = page.getByTestId("session-id");
    // Still in DOM (so tests/dev tools can introspect) but visually hidden
    await expect(pill).not.toBeVisible();
    await expect(pill).not.toHaveText("No session");
  });

  test("Blob skill status is a prominent state-aware badge", async ({ page }) => {
    await openApp(page);
    await startNewSession(page);
    const badge = page.getByTestId("skill-binding-status");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveClass(/\bnew\b/);
    // Structured: state copy + skill label + icon
    await expect(badge.locator("svg.icon")).toHaveCount(1);
    await expect(badge.locator(".binding-state")).toBeVisible();
    await expect(badge.locator(".binding-skill")).toContainText("Skill: NEW");
    // It must be readable: tests should still find "Skill: NEW" anywhere inside.
    await expect(badge).toContainText("Skill: NEW");
  });

  test("checklist renders fields, not raw JSON, and uses brand (not green) accents", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-session",
        stage: "VERIFY_CHECKLIST",
        verify_checklist: {
          identity: {
            status: "confirmed",
            content: {
              summary: "Focused on programmer history lookup",
              source: "user_confirmed",
            },
          },
          metadata: {
            status: "confirmed",
            content: {
              name: "history-programmer",
              author: "owner",
              version: "1.0",
            },
          },
          io_spec: {
            status: "pending",
            content: {
              summary: "Input is a name; output is a JSON summary",
            },
          },
        },
        conversation: [],
        pending_tool_calls: [],
      };
      win.__sgv2.renderChecklist();
    });

    await openTab(page, "checklist");
    const checklist = page.getByTestId("checklist");
    await expect(checklist).toBeVisible();
    // No raw JSON braces should be shown to the user.
    await expect(checklist).not.toContainText('"source": "user_confirmed"');
    // Field labels (from labelize) must be visible
    await expect(checklist.locator(".check-field dt", { hasText: /^Summary$/ }).first()).toBeVisible();
    await expect(checklist.locator(".check-field dt", { hasText: /^Name$/ })).toBeVisible();
    // Status pills carry icons
    await expect(checklist.locator(".check-item .check-status svg.icon").first()).toBeVisible();
    // Confirmed items use the warm brand left border, not green.
    const borderColor = await checklist
      .locator(".check-item.confirmed")
      .first()
      .evaluate((el) => getComputedStyle(el).borderLeftColor);
    // brand-500 = #c4663a → rgb(196, 102, 58)
    expect(borderColor).toBe("rgb(196, 102, 58)");
  });

  test("auto-continued system prompts are not rendered as user bubbles on reload", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-session-auto",
        current_stage: "INTAKE",
        conversation: [
          { role: "user", content: "Build a calendar skill.", metadata: {} },
          { role: "assistant", content: "Got it, moving on.", metadata: {} },
          {
            role: "user",
            content:
              "Continue automatically after entering RESEARCH. Start the next stage action now. If this stage requires user confirmation, ask the user with ask_user_input instead of waiting for a manual message.",
            metadata: { auto_continue: true, auto_depth: 1 },
          },
          { role: "assistant", content: "Researching now.", metadata: {} },
        ],
        pending_tool_calls: [],
        verify_checklist: {},
      };
      // Drive the real loader so we exercise the actual code path under test.
      win.__sgv2.renderLoadedSessionContent();
    });

    const stream = page.locator("#chatStream");

    // The auto-continue prompt must NOT appear as a user bubble anywhere in the stream.
    await expect(
      stream.locator(".message.user", { hasText: /Continue automatically after entering RESEARCH/ })
    ).toHaveCount(0);
    // No assistant or other bubble should leak the system-prompt text either.
    await expect(
      stream.locator("text=Continue automatically after entering RESEARCH")
    ).toHaveCount(0);

    // A slim system status line replaces it.
    await expect(
      stream.locator(".conversation-status", { hasText: "System auto-continued" })
    ).toBeVisible();

    // Real user / assistant messages survive.
    await expect(
      stream.locator(".message.user", { hasText: "Build a calendar skill." })
    ).toBeVisible();
    await expect(
      stream.locator(".message.assistant", { hasText: "Researching now." })
    ).toBeVisible();
  });

  test("saved session materials render on reload alongside any pending attachments", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-session-materials",
        current_stage: "INTAKE",
        materials: [
          { kind: "api_spec", content: "GET /events returns list of calendar events." },
          { kind: "text", content: "Field notes captured from the product brief." },
        ],
        conversation: [],
        pending_tool_calls: [],
        verify_checklist: {},
      };
      win.__sgv2.attachedMaterials = [];
      win.__sgv2.renderMaterials();
    });

    const list = page.locator("#materialList");
    await expect(list.locator('.material-section[data-section="saved"]')).toBeVisible();
    await expect(list.locator('[data-testid="saved-material-count"]')).toHaveText("2");
    await expect(list.locator('.material-section[data-section="saved"] .material-section-title')).toContainText("Saved in this session");
    await expect(list).toContainText("GET /events");

    // Empty-state copy must be gone once we have saved materials.
    await expect(list.locator(".empty-state")).toHaveCount(0);

    // Add a pending attachment and confirm both sections render side by side.
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.attachedMaterials = [{ kind: "code", content: "fetch('/api/events')", metadata: {} }];
      win.__sgv2.renderMaterials();
    });
    await expect(list.locator('.material-section[data-section="saved"]')).toBeVisible();
    await expect(list.locator('.material-section[data-section="pending"]')).toBeVisible();
    await expect(list.locator('[data-testid="pending-material-count"]')).toHaveText("1");
    await expect(list.locator('.material-section[data-section="pending"] .material-section-title')).toContainText("Will send next");
  });

  test("session selector labels expose mode, stage, message count, material count", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.savedSessions = [
        {
          id: "spec-session-new",
          mode: "new",
          target_skill_id: null,
          current_stage: "INTAKE",
          title: "Build a calendar skill that lists events",
          message_count: 4,
          material_count: 2,
          updated_at: "2026-05-27T08:30:00Z",
        },
        {
          id: "spec-session-modify",
          mode: "modify",
          target_skill_id: "calendar-v1",
          current_stage: "REFINE",
          title: "calendar-v1",
          message_count: 9,
          material_count: 0,
          updated_at: "2026-05-26T22:00:00Z",
        },
      ];
      win.__sgv2.renderSessionSelector();
    });

    const selector = page.locator("#sessionList");
    const newOption = selector.locator('option[value="spec-session-new"]');
    await expect(newOption).toHaveCount(1);
    const newLabel = (await newOption.textContent()) || "";
    expect(newLabel).toContain("[NEW]");
    expect(newLabel).toContain("Build a calendar skill that lists events");
    expect(newLabel).toContain("INTAKE");
    expect(newLabel).toContain("4 msgs");
    expect(newLabel).toContain("2 materials");

    const modifyOption = selector.locator('option[value="spec-session-modify"]');
    await expect(modifyOption).toHaveCount(1);
    const modifyLabel = (await modifyOption.textContent()) || "";
    expect(modifyLabel).toContain("[MODIFY]");
    expect(modifyLabel).toContain("calendar-v1");
    expect(modifyLabel).toContain("REFINE");
    expect(modifyLabel).toContain("9 msgs");
    // Singular grammar
    expect(modifyLabel).not.toContain("9 msg ");
  });

  test("submitting a stale question card surfaces a friendly status instead of throwing", async ({ page }) => {
    await openApp(page);
    // Intercept the tool-result endpoint and force a 404 to simulate the server having
    // already consumed the call.
    await page.route("**/api/sessions/*/tool-result", (route) =>
      route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Tool call not found" }),
      })
    );
    // Also intercept GET /api/sessions/{id} to return a fresh session shape without
    // pending calls (the refresh path used after a stale 404).
    await page.route(/\/api\/sessions\/[^/]+$/, (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "stale-spec-session",
          mode: "new",
          current_stage: "INTAKE",
          conversation: [],
          pending_tool_calls: [],
          verify_checklist: {},
          materials: [],
          current_skill: { skill_md: "", version_hash: "" },
          patch_history: [],
          test_runs: [],
        }),
      });
    });

    // Plant a fake session that has one ask_user_input pending tool call.
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "stale-spec-session",
        mode: "new",
        current_stage: "INTAKE",
        conversation: [],
        materials: [],
        pending_tool_calls: [
          {
            call_id: "call_stale_42",
            tool: "ask_user_input",
            args: { question: "Do you want to continue?", options: ["yes", "no"] },
            created_at: new Date().toISOString(),
          },
        ],
        verify_checklist: {},
        current_skill: { skill_md: "", version_hash: "" },
        patch_history: [],
        test_runs: [],
      };
      win.__sgv2.renderLoadedSessionContent();
    });

    const card = page.locator('[data-testid="question-card"]');
    await expect(card).toBeVisible();

    // Pick an option and submit.
    await card.locator("input[type='radio']").first().check();
    await card.locator('[data-testid="submit-questions-button"]').click();

    // The friendly status message should appear, and no uncaught error should crash the UI.
    await expect(
      page.locator(".conversation-status", {
        hasText: "already resolved on the server",
      })
    ).toBeVisible({ timeout: 5000 });

    // The question card must be marked resolved so the user cannot resubmit.
    await expect(page.locator('.question-item.resolved, [data-question-id="call_stale_42"].resolved')).toHaveCount(1, { timeout: 5000 });
  });

  test("Materials CRUD — attach saves directly to session and adds an item", async ({ page }) => {
    let postedBody: unknown = null;
    await page.route("**/api/sessions/spec-crud-session/materials", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      postedBody = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "spec-crud-session",
          mode: "new",
          current_stage: "INTAKE",
          conversation: [],
          pending_tool_calls: [],
          verify_checklist: {},
          materials: [
            {
              id: "mat-1",
              kind: "text",
              content: "Hello from spec",
              metadata: {},
              created_at: "2026-05-27T09:00:00Z",
            },
          ],
          current_skill: { skill_md: "", version_hash: "" },
          patch_history: [],
          test_runs: [],
        }),
      });
    });
    await page.route("**/api/sessions", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-crud-session",
        mode: "new",
        current_stage: "INTAKE",
        conversation: [],
        pending_tool_calls: [],
        verify_checklist: {},
        materials: [],
        current_skill: { skill_md: "", version_hash: "" },
        patch_history: [],
        test_runs: [],
      };
      win.__sgv2.attachedMaterials = [];
      win.__sgv2.renderSession();
    });
    await openTab(page, "materials");
    await page.getByTestId("add-material-row-button").click();

    // Only the three kinds that actually differ are offered, and the tier hint tracks them.
    const kindSelect = page.getByTestId("material-kind");
    await expect(kindSelect.locator("option")).toHaveCount(3);
    await expect(page.getByTestId("material-kind-hint")).toContainText("Tier 3");
    await kindSelect.selectOption("code");
    await expect(page.getByTestId("material-kind-hint")).toContainText("Tier 1");
    await kindSelect.selectOption("text");

    await page.getByTestId("material-input").fill("Hello from spec");
    await page.getByTestId("attach-material-button").click();

    const item = page.locator('[data-testid="saved-material-item"]').first();
    await expect(item).toBeVisible();
    await expect(item).toContainText("Hello from spec");
    expect(postedBody).toMatchObject({ kind: "text", content: "Hello from spec" });
  });

  // Regression: startSession() clears the pending list, so sending the very first
  // message used to drop a material attached before the session existed.
  test("Materials CRUD — a material attached before the session survives the first chat turn", async ({ page }) => {
    const sessionBody = (materials: unknown[]) => ({
      id: "spec-pending-session",
      mode: "new",
      current_stage: "INTAKE",
      conversation: [],
      pending_tool_calls: [],
      verify_checklist: {},
      materials,
      current_skill: { skill_md: "", version_hash: "" },
      patch_history: [],
      test_runs: [],
    });
    const savedMaterial = {
      id: "mat-pending",
      kind: "code",
      content: "print('pending')",
      created_at: "2026-05-27T09:00:00Z",
    };
    let chatBody: { materials?: unknown[] } | null = null;

    await page.route("**/api/sessions", (route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      }
      if (method !== "POST") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionBody([])),
      });
    });
    await page.route("**/api/sessions/spec-pending-session/chat", (route) => {
      chatBody = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ events: [{ event: "text_delta", data: { delta: "ok" } }] }),
      });
    });
    await page.route("**/api/sessions/spec-pending-session", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionBody([savedMaterial])),
      });
    });

    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = null;
      win.__sgv2.attachedMaterials = [];
      win.__sgv2.renderSession();
    });
    await openTab(page, "materials");
    await page.getByTestId("add-material-row-button").click();
    await page.getByTestId("material-kind").selectOption("code");
    await page.getByTestId("material-input").fill("print('pending')");
    await page.getByTestId("attach-material-button").click();
    await expect(page.locator('.material-item-pending')).toHaveCount(1);

    await page.getByTestId("message-input").fill("Build a skill from this.");
    await page.getByTestId("send-button").click();
    await expect(page.getByTestId("send-button")).toHaveText("Send");

    expect(chatBody?.materials).toMatchObject([{ kind: "code", content: "print('pending')" }]);
    await openTab(page, "materials");
    await expect(page.locator('[data-testid="saved-material-item"]')).toHaveCount(1);
    await expect(page.locator('[data-testid="saved-material-item"]').first()).toContainText("pending");
  });

  test("Materials CRUD — edit mode replaces preview with form and PUT updates the item", async ({ page }) => {
    await page.route("**/api/sessions", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    let putBody: unknown = null;
    await page.route("**/api/sessions/spec-edit-session/materials/mat-9", async (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      putBody = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "spec-edit-session",
          mode: "new",
          current_stage: "INTAKE",
          conversation: [],
          pending_tool_calls: [],
          verify_checklist: {},
          materials: [
            {
              id: "mat-9",
              kind: "code",
              content: "console.log('updated')",
              metadata: {},
              created_at: "2026-05-27T09:00:00Z",
            },
          ],
          current_skill: { skill_md: "", version_hash: "" },
          patch_history: [],
          test_runs: [],
        }),
      });
    });

    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-edit-session",
        mode: "new",
        current_stage: "INTAKE",
        conversation: [],
        pending_tool_calls: [],
        verify_checklist: {},
        materials: [
          {
            id: "mat-9",
            kind: "text",
            content: "original",
            metadata: {},
            created_at: "2026-05-27T09:00:00Z",
          },
        ],
        current_skill: { skill_md: "", version_hash: "" },
        patch_history: [],
        test_runs: [],
      };
      win.__sgv2.attachedMaterials = [];
      win.__sgv2.renderSession();
    });
    await openTab(page, "materials");

    const item = page.locator('[data-testid="saved-material-item"]').first();
    await expect(item).toBeVisible();
    await item.locator('[data-testid="edit-material-button"]').click();

    const textarea = item.locator("textarea[data-edit-content]");
    await expect(textarea).toBeVisible();
    await textarea.fill("console.log('updated')");
    await item.locator("select[data-edit-kind]").selectOption("code");
    await item.locator('[data-testid="save-material-button"]').click();

    await expect.poll(() => putBody).not.toBeNull();
    expect(putBody).toMatchObject({ kind: "code", content: "console.log('updated')" });
    await expect(page.locator('[data-testid="saved-material-item"]').first()).toContainText("updated");
  });

  test("Materials CRUD — delete confirms and removes the item via DELETE", async ({ page }) => {
    await page.route("**/api/sessions", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    let deleteCalled = false;
    await page.route("**/api/sessions/spec-del-session/materials/mat-42", async (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      deleteCalled = true;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "spec-del-session",
          mode: "new",
          current_stage: "INTAKE",
          conversation: [],
          pending_tool_calls: [],
          verify_checklist: {},
          materials: [],
          current_skill: { skill_md: "", version_hash: "" },
          patch_history: [],
          test_runs: [],
        }),
      });
    });

    await openApp(page);
    page.on("dialog", (dialog) => dialog.accept());
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "spec-del-session",
        mode: "new",
        current_stage: "INTAKE",
        conversation: [],
        pending_tool_calls: [],
        verify_checklist: {},
        materials: [
          {
            id: "mat-42",
            kind: "text",
            content: "delete me",
            metadata: {},
            created_at: "2026-05-27T09:00:00Z",
          },
        ],
        current_skill: { skill_md: "", version_hash: "" },
        patch_history: [],
        test_runs: [],
      };
      win.__sgv2.attachedMaterials = [];
      win.__sgv2.renderSession();
    });
    await openTab(page, "materials");

    const item = page.locator('[data-testid="saved-material-item"]').first();
    await expect(item).toBeVisible();
    await item.locator('[data-testid="delete-material-button"]').click();

    await expect.poll(() => deleteCalled).toBe(true);
    await expect(page.locator('[data-testid="saved-material-item"]')).toHaveCount(0);
  });


  test("env_vars checklist renders OBO tokens and reconciliation as structured UI (no raw JSON)", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => {
      const win = window;
      win.__sgv2.session = {
        id: "env-render-session",
        current_stage: "VERIFY_CHECKLIST",
        aca_env_result: {
          variables: ["OBO_CLIENT_ID", "OBO_CLIENT_SECRET", "OBO_SCOPE_REGISTRY"],
          architectural_config: {
            OBO_SCOPE_REGISTRY: {
              AZURE_SQL_ACCESS_TOKEN: "https://database.windows.net/.default",
              GRAPH_ACCESS_TOKEN: "https://graph.microsoft.com/.default",
              AI_SEARCH_ACCESS_TOKEN: "https://search.azure.com/.default",
              FABRIC_ACCESS_TOKEN: "https://api.fabric.microsoft.com/.default",
            },
          },
        },
        verify_checklist: {
          env_vars: {
            status: "pending",
            content: {
              matched_existing: [
                {
                  required:
                    "optional OBO client configuration when this API deployment uses an existing OBO flow",
                  aca_variable: "OBO_CLIENT_ID",
                  reason: "可沿用既有 OBO client 設定，但不是此 API 的直接 endpoint 或 key",
                },
                {
                  required:
                    "optional OBO client secret when this API deployment uses an existing OBO flow",
                  aca_variable: "OBO_CLIENT_SECRET",
                  reason: "可沿用既有 OBO client secret，但不是此 API 的直接 endpoint 或 key",
                },
              ],
              existing_obo_tokens: {
                AZURE_SQL_ACCESS_TOKEN: "https://database.windows.net/.default",
                GRAPH_ACCESS_TOKEN: "https://graph.microsoft.com/.default",
              },
              needs_to_be_added: [
                {
                  variable: "UAPIS_BASE_URL",
                  reason: "目前 ACA 沒有此 programmer history API 的專用 endpoint/base URL 變數",
                },
                {
                  variable: "UAPIS_API_KEY",
                  reason: "目前 ACA 沒有此 programmer history API 的專用 API key 變數",
                },
              ],
              user_warning:
                "現有 ACA/OBO 設定只能作為可選重用基礎，尚無可直接代表此 API 的既有 token scope。",
            },
          },
        },
        conversation: [],
        pending_tool_calls: [],
      };
      win.__sgv2.renderChecklist();
    });

    await openTab(page, "checklist");
    const checklist = page.getByTestId("checklist");
    await expect(checklist).toBeVisible();

    // No raw JSON braces should be shown to the user.
    await expect(checklist).not.toContainText('"matched_existing"');
    await expect(checklist).not.toContainText('"aca_variable"');
    await expect(checklist).not.toContainText('"needs_to_be_added"');
    await expect(checklist.locator("pre")).toHaveCount(0);

    // Structured rendering surfaces the friendly section titles.
    await expect(checklist).toContainText("Reused from existing ACA");
    await expect(checklist).toContainText("Existing OBO tokens");
    await expect(checklist).toContainText("Needs to be added to ACA");

    // Matched variables show as code chips.
    await expect(checklist.locator(".env-section-matched .env-var-name", { hasText: "OBO_CLIENT_ID" })).toHaveCount(1);
    await expect(checklist.locator(".env-section-matched .env-var-name", { hasText: "OBO_CLIENT_SECRET" })).toHaveCount(1);

    // Missing variables surface in the danger-styled section.
    await expect(checklist.locator(".env-section-needs .env-var-missing", { hasText: "UAPIS_BASE_URL" })).toHaveCount(1);
    await expect(checklist.locator(".env-section-needs .env-var-missing", { hasText: "UAPIS_API_KEY" })).toHaveCount(1);

    // OBO tokens render as a name → scope table inside the reconciliation section.
    const tokens = checklist.locator(".env-section-obo .env-token-row");
    await expect(tokens.first()).toBeVisible();
    await expect(checklist.locator(".env-section-obo")).toContainText("AZURE_SQL_ACCESS_TOKEN");
    await expect(checklist.locator(".env-section-obo")).toContainText("https://database.windows.net/.default");

    // The user warning surfaces as a callout, not a pre block.
    await expect(checklist.locator(".env-warning")).toContainText("尚無可直接代表此 API 的既有 token scope");
  });

});
