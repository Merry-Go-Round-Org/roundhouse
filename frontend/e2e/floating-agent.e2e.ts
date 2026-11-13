import { test, expect } from "@playwright/test";

test("floating chat preserves a draft across minimize and navigation without reserving a column", async ({
  page,
}) => {
  const writes: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    if (route.request().method() !== "GET") writes.push(route.request().url());
    if (new URL(route.request().url()).pathname.endsWith("/auth/me"))
      return route.fulfill({ json: { id: "operator", role: "developer" } });
    return route.fulfill({ json: [] });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  const launch = page.getByRole("button", {
    name: "Open agent chat",
    exact: true,
  });
  await expect(launch).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "Agent chat" }),
  ).toHaveCount(0);
  const before = await page.locator(".workspace-main").boundingBox();
  expect(before!.width).toBeGreaterThan(1000);
  await launch.click();
  const message = page.getByLabel("Message the agent", { exact: true });
  await message.fill("Keep my draft while I inspect datasets");
  await page
    .getByRole("button", { name: "Minimize chat", exact: true })
    .click();
  await expect(launch).toBeFocused();
  await page.getByRole("button", { name: "Datasets", exact: true }).click();
  await launch.click();
  await expect(message).toHaveValue("Keep my draft while I inspect datasets");
  const after = await page.locator(".workspace-main").boundingBox();
  expect(after!.width).toBe(before!.width);
  for (const size of [
    { width: 1440, height: 900 },
    { width: 1280, height: 720 },
    { width: 960, height: 540 },
    { width: 390, height: 600 },
  ]) {
    await page.setViewportSize(size);
    for (const expanded of [false, true]) {
      if (expanded)
        await page
          .getByRole("button", { name: "Expand chat", exact: true })
          .click();
      for (const locator of [
        page.locator(".agent-window"),
        message,
        page.getByRole("button", { name: "Send", exact: true }),
      ]) {
        const box = await locator.boundingBox();
        expect(box).not.toBeNull();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.y).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(size.width);
        expect(box!.y + box!.height).toBeLessThanOrEqual(size.height);
      }
      const formBox = await page.locator(".chat-composer-form").boundingBox();
      const sendBox = await page.getByRole("button", { name: "Send", exact: true }).boundingBox();
      expect(sendBox!.y + sendBox!.height).toBeLessThanOrEqual(formBox!.y + formBox!.height);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: `test-results/floating-chat-${size.width}-${expanded ? "expanded" : "normal"}.png`,
      });
      if (expanded)
        await page
          .getByRole("button", { name: "Restore chat size", exact: true })
          .click();
    }
  }
  await message.focus();
  await page.keyboard.press("Escape");
  await expect(launch).toBeFocused();
  await launch.click();
  await expect(message).toHaveValue("Keep my draft while I inspect datasets");
  expect(writes).toEqual([]);
});
