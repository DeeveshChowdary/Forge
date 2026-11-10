import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  use: { baseURL: "http://127.0.0.1:8799", channel: "chrome", headless: true, trace: "retain-on-failure" },
  webServer: { command: "./e2e/serve.sh", url: "http://127.0.0.1:8799/api/health", timeout: 60_000, reuseExistingServer: false },
});
