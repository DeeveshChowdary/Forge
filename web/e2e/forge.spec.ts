import { expect, test } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

const repo = join(process.env.TMPDIR ?? tmpdir(), "forge-e2e", "repo");

test("import project, run a mock task through the UI, approve the merge", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Engine running")).toBeVisible();

  // Import the disposable repo via the dialog.
  await page.getByRole("link", { name: "Projects" }).click();
  await page.getByRole("button", { name: "Add project" }).click();
  await page.getByPlaceholder("/Users/you/code/my-app").fill(repo);
  await page.getByRole("button", { name: "Inspect" }).click();
  await expect(page.getByText("Git repository: yes")).toBeVisible();
  await page.getByPlaceholder("{python} -m pytest -q").fill("{python} -m pytest -q -p no:cacheprovider");
  await page.getByRole("dialog").getByRole("button", { name: "Add project" }).click();
  await expect(page.getByRole("heading", { name: "repo", exact: true, level: 1 })).toBeVisible();

  // Create a task with a scripted mock scenario.
  await page.getByRole("link", { name: "New task" }).first().click();
  await page.getByPlaceholder(/slugify/).fill("Add mul(a, b) with a test");
  await page.getByRole("button", { name: "Preview routing" }).click();
  await expect(page.getByTestId("route-preview")).toContainText("mock/");
  await page.getByText("Mock scenario (testing only)").click();
  await page.getByLabel("Mock scenario JSON").fill(JSON.stringify({
    files: { "calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
             "tests/test_mul.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n" },
    reviews: ["approve"], sleep_s: 0.5,
  }));
  await page.getByRole("button", { name: "Start task" }).click();

  // Live execution page: watch it reach the merge approval.
  await expect(page.getByTestId("event-log")).toContainText("run.started", { timeout: 20_000 });
  await expect(page.getByText("MOCK").first()).toBeVisible();
  const approval = page.getByTestId("approval");
  await expect(approval).toContainText("Merge forge/", { timeout: 60_000 });
  await page.getByRole("tab", { name: /Runs/ }).click();
  await expect(page.locator("table")).toContainText("mock/mock-standard");
  await page.getByRole("tab", { name: "Diff" }).click();
  await expect(page.getByTestId("diff")).toContainText("def mul");
  await approval.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByTestId("state-badge").first()).toHaveText("COMPLETED", { timeout: 15_000 });
  await expect(page.getByText(/merged into main/).first()).toBeVisible();
});

test("agent control center lists only verified mock models here and theme toggles", async ({ page }) => {
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agent control center" })).toBeVisible();
  await expect(page.getByText("mock-strong", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("verified").first()).toBeVisible();
  const toggle = page.getByRole("button", { name: /Switch to (light|dark) theme/ });
  const before = await page.evaluate(() => document.documentElement.classList.contains("dark"));
  await toggle.click();
  expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(!before);
});

test("rejects cross-site writes", async ({ request }) => {
  const r = await request.post("/api/projects", { data: { mode: "create", name: "x" } });
  expect(r.status()).toBe(403);
});
