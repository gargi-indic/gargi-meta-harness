import { test, expect, type Page } from '@playwright/test';

// The demo server (harness serve --demo) seeds repo acme/widgets and shares one scripted run across scenarios.
const RUN_ID = 'acme/widgets#7@1';
const RUN_PATH = `/runs/${encodeURIComponent(RUN_ID)}`;

test.beforeEach(async ({ page }) => {
  // /auth sets the session cookie (token from HARNESS_DEMO_TOKEN) and redirects to the app.
  await page.goto('/auth?token=smoke');
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeAttached();
});

function inboxPanel(page: Page, title: RegExp) {
  return page.locator('section.panel').filter({ has: page.getByRole('heading', { name: title }) });
}

test('1 campaigns: graph shows 6 nodes and selecting one opens the panel', async ({ page }) => {
  await page.goto('/campaigns');
  await expect(page.getByText('DEMO DATA')).toBeVisible();
  await expect(page.locator('.react-flow__node')).toHaveCount(6);
  await page.getByText('Widget export').click();
  await expect(page.getByRole('complementary').getByRole('heading', { name: '#5 Widget export' })).toBeVisible();
});

test('2 board: the six column headers are visible', async ({ page }) => {
  await page.goto('/board');
  for (const label of ['READY', 'RUNNING', 'NEEDS YOU', 'REVIEW', 'BLOCKED', 'DONE']) {
    await expect(page.getByText(new RegExp(`^${label} \\d+$`))).toBeVisible();
  }
});

test('3 run: start the demo run, a node is RUNNING, activity streams in, the merge gate reaches the Inbox', async ({
  page,
}) => {
  const started = await page.request.post('/api/demo/start-run');
  expect(started.ok()).toBe(true);
  expect(await started.json()).toEqual({ run_id: RUN_ID });

  await page.goto(RUN_PATH);
  await expect(page.locator('.react-flow__node').filter({ hasText: 'RUNNING' }).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect.poll(() => page.locator('.activity-row').count(), { timeout: 10_000 }).toBeGreaterThan(0);

  await page.goto('/inbox');
  await expect(inboxPanel(page, /#7 Widget docs/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('heading', { name: /merge approval/i })).toBeVisible();
});

test('4 inbox: approving the merge gate takes the run to done', async ({ page }) => {
  await page.goto('/inbox');
  const panel = inboxPanel(page, /#7 Widget docs/);
  await expect(panel).toBeVisible({ timeout: 30_000 });
  await panel.getByRole('button', { name: 'Approve' }).click();
  await expect(panel).toBeHidden();

  await expect
    .poll(
      async () => {
        const res = await page.request.get(`/api/runs/${encodeURIComponent(RUN_ID)}`);
        return (await res.json()).run.status;
      },
      { timeout: 30_000 },
    )
    .toBe('done');

  await page.goto(RUN_PATH);
  await expect(page.getByText('done', { exact: true }).first()).toBeVisible();
});

test('5 editor: delete an edge, see the validation error, undo, then Save as PR', async ({ page }) => {
  await page.goto('/playbooks/plan-build-review');
  await expect(page.locator('.react-flow__node')).not.toHaveCount(0);
  await expect(page.getByText('No validation issues.')).toBeVisible();

  // scope.pass -> pr_review: removing it leaves the pass port of scope unrouted.
  const edge = page.locator('.react-flow__edge[data-id="scope.pass"]');
  await edge.locator('.react-flow__edge-interaction').dispatchEvent('click');
  await page.keyboard.press('Backspace');
  await expect(page.getByText('Unrouted port scope.pass')).toBeVisible();

  await page.getByRole('button', { name: 'Undo' }).click();
  await expect(page.getByText('No validation issues.')).toBeVisible();

  await page.getByRole('button', { name: 'Save as PR' }).click();
  await page.getByLabel('Target repository').fill('acme/widgets');
  await page.getByRole('button', { name: 'Confirm' }).click();
  const link = page.getByRole('link', { name: /^PR #\d+$/ });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('href', /^https:\/\/example\.invalid\/pr\/\d+$/);
});

test('6 mobile: no horizontal scroll on /board and /inbox at 375px', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  const overflows = () =>
    page.evaluate(() => document.scrollingElement!.scrollWidth > window.innerWidth);

  await page.goto('/board');
  await expect(page.getByText(/^READY \d+$/)).toBeVisible();
  expect(await overflows()).toBe(false);

  await page.goto('/inbox');
  await expect(page.locator('section.panel').first()).toBeVisible();
  expect(await overflows()).toBe(false);
});

test('7 design guard: square controls and panels, no box-shadow anywhere', async ({ page }) => {
  await page.goto('/inbox');
  await expect(page.locator('section.panel').first()).toBeVisible();
  const violations = await page.evaluate(() => {
    const bad: string[] = [];
    for (const el of document.querySelectorAll('*')) {
      const style = getComputedStyle(el);
      const label = `${el.tagName.toLowerCase()}.${String((el as HTMLElement).className)}`;
      const sampled = el.matches('button, input, select, textarea, .panel');
      if (sampled && style.borderRadius !== '0px') bad.push(`${label} border-radius ${style.borderRadius}`);
      if (style.boxShadow !== 'none') bad.push(`${label} box-shadow ${style.boxShadow}`);
    }
    return bad;
  });
  expect(violations).toEqual([]);
});
