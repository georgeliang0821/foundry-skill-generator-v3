import type { APIRequestContext, Page } from "@playwright/test";

export async function resetE2E(request: APIRequestContext) {
  await request.post("/api/e2e/reset");
}

export async function setScenario(request: APIRequestContext, scenario: string) {
  await request.post("/api/e2e/scenario", { data: { scenario } });
}

export async function seedSkill(request: APIRequestContext, name = "e2e-existing-skill") {
  return request.post("/api/e2e/seed-skill", { data: { name } });
}

export async function readSession(request: APIRequestContext, sessionId: string) {
  const response = await request.get(`/api/e2e/session/${sessionId}`);
  return response.json();
}

export async function currentSessionId(page: Page) {
  const text = (await page.getByTestId("session-id").textContent()) || "";
  const match = text.match(/[a-f0-9]{32}/i);
  if (!match) throw new Error(`Could not read session id from: ${text}`);
  return match[0];
}
