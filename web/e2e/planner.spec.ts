import { test, expect, type Locator } from '@playwright/test';

// The demo server (harness serve --demo) decomposes any plan into the same five issues, has the demo plan
// docs/PLAN.md already approved, and creates issues in an in-memory GitHub (GET /api/demo/planner-issues).
// The scenario runs once per server: creating the issues is not repeatable.

// The canvas fits its view a moment after the nodes appear: read a handle's position only once it stopped moving.
async function settledBox(locator: Locator) {
  let previous = JSON.stringify(await locator.boundingBox());
  for (let i = 0; i < 30; i++) {
    await locator.page().waitForTimeout(100);
    const box = await locator.boundingBox();
    if (JSON.stringify(box) === previous) return box!;
    previous = JSON.stringify(box);
  }
  throw new Error('the canvas did not settle');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/auth?token=smoke');
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeAttached();
});

test('8 planner: decompose, add an edge, validation is clean, approve creates 5 issues', async ({ page }) => {
  // Tall enough that the whole 520px canvas is on screen: a drag cannot end outside the viewport.
  await page.setViewportSize({ width: 1280, height: 1100 });
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Planner' }).click();
  await expect(page).toHaveURL(/\/planner$/);

  await page.getByLabel('Repository').fill('acme/widgets');
  await page.getByLabel('Plan path').fill('docs/PLAN.md');
  await page.getByRole('button', { name: 'Load plan' }).click();
  await expect(page.getByText('PLAN APPROVED')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Widget platform' })).toBeVisible();

  await page.getByRole('button', { name: 'Decompose' }).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(5, { timeout: 15_000 });
  await expect(page.locator('.react-flow__edge')).toHaveCount(3);
  await expect(page.getByText('No validation issues.')).toBeVisible();

  // Draw a dependency: drag from the source handle of "docs" to the target handle of "export".
  const source = page.locator('.react-flow__node[data-id="docs"] .source-handle');
  const target = page.locator('.react-flow__node[data-id="export"] .target-handle');
  const from = await settledBox(source);
  const to = await settledBox(target);
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator('.react-flow__edge')).toHaveCount(4);

  // Saved to the (demo) GitHub, validation still clean, and Approve is enabled.
  await expect(page.getByText('SAVED', { exact: true })).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: 'Validate' }).click();
  await expect(page.getByText('No validation issues.')).toBeVisible();
  const approve = page.getByRole('button', { name: 'Approve and create' });
  await expect(approve).toBeEnabled();

  await approve.click();
  const created = page.getByLabel('Created issues');
  await expect(created).toBeVisible({ timeout: 30_000 });
  await expect(created.getByRole('link')).toHaveCount(6); // the epic and the five issues

  const res = await page.request.get('/api/demo/planner-issues');
  const issues: { number: number; title: string }[] = await res.json();
  expect(issues).toHaveLength(6);
  expect(issues.map((i) => i.title)).toContain('Add widget CRUD endpoints');
});

test('9 planner: no horizontal scroll at 1024px and 860px, and stacked below 860px', async ({ page }) => {
  const overflows = () => page.evaluate(() => document.scrollingElement!.scrollWidth > window.innerWidth);
  for (const width of [1024, 860]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/planner');
    await expect(page.getByRole('button', { name: 'Decompose' })).toBeVisible();
    expect(await overflows()).toBe(false);
  }
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto('/planner');
  await expect(page.getByRole('button', { name: 'Decompose' })).toBeVisible();
  expect(await overflows()).toBe(false);
  const columns = await page.locator('.planner-zones').evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(columns).toBe(1);
});
