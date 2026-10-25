import { test, expect } from '@playwright/test';

// The demo server scripts agent health (jules ok, claude degraded, codex down, copilot disabled) and seeds spend
// for the seeded runs. Kill and Resume go through the real /api/ops routes (the demo has a control issue).

test.beforeEach(async ({ page }) => {
  await page.goto('/auth?token=smoke');
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeAttached();
});

test('10 agents: grid with scripted health, cost table with an ESTIMATED tag', async ({ page }) => {
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Agents' }).click();
  await expect(page).toHaveURL(/\/agents$/);
  const jules = page.getByRole('region', { name: 'jules' });
  await expect(jules.getByText('ok', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'codex' }).getByText('down', { exact: true })).toBeVisible();
  await expect(page.getByText('ESTIMATED')).toBeVisible();
  await expect(page.getByText(/^SPEND \$/)).toBeVisible();
});

test('11 kill switch: needs typing KILL, HALTED shows, Resume clears', async ({ page }) => {
  await page.goto('/agents');
  await page.getByRole('button', { name: 'Kill all' }).click();
  const confirm = page.getByRole('button', { name: 'Confirm kill' });
  await expect(confirm).toBeDisabled();
  await page.getByLabel('Type KILL to confirm').fill('kill');
  await expect(confirm).toBeDisabled();
  await page.getByLabel('Type KILL to confirm').fill('KILL');
  await confirm.click();

  await expect(page.getByText('HALTED', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume' }).click();
  await expect(page.getByText('HALTED', { exact: true })).toBeHidden();
  await expect(page.getByRole('button', { name: 'Kill all' })).toBeVisible();
});

test('12 agents: no horizontal scroll at 1024px', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  await page.goto('/agents');
  await expect(page.getByRole('region', { name: 'jules' })).toBeVisible();
  expect(await page.evaluate(() => document.scrollingElement!.scrollWidth > window.innerWidth)).toBe(false);
});
