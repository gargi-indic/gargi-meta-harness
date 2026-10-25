import { apiPost } from './client';
import type { BudgetStatusOut } from './types';

export function killAll() {
  return apiPost<{ halted: boolean; cancels_queued: number }>('/api/ops/kill', { confirm: 'KILL' });
}

export function resumeAll() {
  return apiPost<{ resumed_runs: string[] }>('/api/ops/resume', {});
}

export function setDailyBudget(dailyUsd: number | null) {
  return apiPost<BudgetStatusOut>('/api/ops/budget', { daily_usd: dailyUsd });
}

export function errorText(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err) return String((err as { detail: unknown }).detail);
  return err instanceof Error ? err.message : 'Request failed';
}

export function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}
