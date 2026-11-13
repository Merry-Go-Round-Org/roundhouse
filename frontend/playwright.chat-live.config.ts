import { defineConfig } from "@playwright/test";

const enabled = process.env.CHAT_LIVE_E2E === "true";
if (enabled) {
  for (const name of [
    "CHAT_LIVE_URL",
    "CHAT_LIVE_USERNAME",
    "CHAT_LIVE_PASSWORD",
  ]) {
    if (!process.env[name])
      throw new Error(`Set ${name} before enabling live chat tests.`);
  }
  const url = new URL(process.env.CHAT_LIVE_URL!);
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  )
    throw new Error(
      "CHAT_LIVE_URL must be a workspace origin without credentials, query or path.",
    );
  if (
    url.protocol !== "https:" &&
    !(
      url.protocol === "http:" &&
      ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    )
  )
    throw new Error(
      "Use HTTPS for hosted chat tests; HTTP is allowed only on loopback.",
    );
}
export default defineConfig({
  testDir: "./e2e",
  testMatch: "chat-provider.live.ts",
  workers: 1,
  retries: 0,
  timeout: 420_000,
  reporter: "list",
  outputDir: "test-results/chat-live",
  use: {
    baseURL: process.env.CHAT_LIVE_URL,
    browserName: "chromium",
    channel: "chrome",
    headless: true,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  // Deliberately no webServer: use the existing, explicitly selected deployment.
});
