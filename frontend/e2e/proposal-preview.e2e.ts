import { expect, test } from "@playwright/test";

test("proposal examples stay separate from live data and fit narrow viewports", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    calls.push(route.request().url());
    return route.abort();
  });
  await page.goto("/demo/proposals");
  await expect(
    page.getByRole("heading", { name: "Agent proposals" }),
  ).toBeVisible();
  await expect(page.getByLabel("Preview limitations")).toContainText(
    "not connected",
  );
  for (const [tab, title] of [
    ["Validation", "Open a validation finding"],
    ["Monitoring", "Start revalidation"],
    ["Chat", "Propose a use restriction"],
    ["Intake plan", "Review an intake plan"],
  ]) {
    await page.getByRole("button", { name: tab, exact: true }).click();
    await expect(page.getByRole("article", { name: title })).toBeVisible();
    await expect(page.getByLabel("Proposal evidence")).toBeVisible();
  }
  await expect(
    page.getByRole("button", { name: "Preview decision", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText(
      "Gate 0 review and editing are awaiting the backend contract.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Validation", exact: true }).click();
  for (const width of [1440, 960, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/proposal-preview-${width}.png`,
      fullPage: true,
    });
  }
  expect(calls).toEqual([]);
});

test("preview decisions require a reason and fresh confirmation and never execute", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    calls.push(route.request().url());
    return route.abort();
  });
  await page.goto("/demo/proposals");
  const submit = page.getByRole("button", {
    name: "Preview decision",
    exact: true,
  });
  const confirmation = page.getByRole("checkbox", {
    name: "I reviewed the example and confirm this preview choice",
  });
  await expect(submit).toBeDisabled();
  await expect(confirmation).toBeDisabled();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await page
    .getByLabel("Reason for this choice")
    .fill("Evidence inspected in this example");
  await confirmation.check();
  await expect(submit).toBeEnabled();
  await page.getByLabel("Reason for this choice").fill("Revised reason");
  await expect(confirmation).not.toBeChecked();
  await expect(submit).toBeDisabled();
  await confirmation.check();
  await page.getByText("Preview states", { exact: true }).click();
  await page.getByRole("button", { name: "Simulate revised proposal" }).click();
  await expect(confirmation).not.toBeChecked();
  await expect(page.getByLabel("Reason for this choice")).toHaveValue("");
  await expect(submit).toBeDisabled();
  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await page
    .getByLabel("Reason for this choice")
    .fill("Example evidence needs clarification");
  await confirmation.check();
  await submit.click();
  await expect(page.getByRole("status")).toContainText(
    "Nothing was saved or executed",
  );
  await expect(submit).toBeDisabled();
  expect(calls).toEqual([]);
});

test("outdated, decided and read-only proposal examples cannot be approved", async ({
  page,
}) => {
  await page.goto("/demo/proposals");
  await page.getByText("Preview states", { exact: true }).click();
  for (const value of [
    "superseded",
    "expired",
    "approved",
    "rejected",
    "executed",
  ]) {
    await page.getByLabel("Example status").selectOption(value);
    await expect(
      page.getByRole("button", { name: "Approve", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Preview decision", exact: true }),
    ).toBeDisabled();
    if (value === "approved")
      await expect(
        page.getByText("Approved · execution not confirmed", { exact: true }),
      ).toBeVisible();
  }
  await page.getByLabel("Example status").selectOption("pending");
  await page.getByLabel("Read-only reviewer example").check();
  await expect(
    page.getByRole("button", { name: "Approve", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("status")).toContainText("read-only access");
});
