import { expect, type Page } from "@playwright/test";

export async function openApp(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Skill Generator V3")).toBeVisible();
  const logo = page.getByAltText("EAA logo");
  await expect(logo).toBeVisible();
  await expect.poll(() => logo.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(page.getByTestId("message-input")).toBeVisible();
}

export async function startNewSession(page: Page, mode = "new") {
  await page.getByTestId("mode-select").selectOption(mode);
  await page.getByTestId("new-session-button").click();
  await expect(page.getByTestId("session-id")).not.toHaveText("No session");
}

export async function sendChat(page: Page, message: string) {
  await page.getByTestId("message-input").fill(message);
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("send-button")).toHaveText("Send");
}

export async function openTab(page: Page, tab: "materials" | "checklist" | "files" | "tests" | "topology" | "inspect" | "agent") {
  await page.getByTestId(`tab-${tab}`).click();
}

export async function createDraftWithFakeAgent(page: Page) {
  await startNewSession(page);
  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-input").fill("E2E material for deterministic skill creation.");
  await page.getByTestId("attach-material-button").click();
  await sendChat(page, "Create a deterministic E2E calendar skill.");
  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/e2e-calendar-skill/);
  await expect(page.getByTestId("draft-card")).toBeVisible();
}
