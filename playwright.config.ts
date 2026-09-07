import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.E2E_PORT || 6275);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: `.\\.venv\\Scripts\\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      SGV2_E2E_MODE: "1",
      SGV2_E2E_FAKE_AGENT: "1",
      SGV2_E2E_FAKE_TEST_RUNNER: "1",
      // The browser cannot send X-Test-UPN, so give it a standing identity.
      E2E_MODE: "1",
      E2E_DEFAULT_UPN: "e2e@example.com",
      SGV2_SKILL_STORE: "local",
      SGV2_SESSION_DIR: ".e2e-sessions",
      FOUNDRY_PROJECT_ENDPOINT: "https://example.test/foundry",
      FOUNDRY_MODEL: "e2e-fake-model",
      MICROSOFT_TENANT_ID: "e2e",
      MICROSOFT_CLIENT_ID: "e2e",
      MICROSOFT_CLIENT_SECRET: "e2e",
      MICROSOFT_OBO_SCOPE: "api://e2e/.default",
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
