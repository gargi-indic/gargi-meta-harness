import { useQuery } from '@tanstack/react-query';
import { apiGet } from './client';
import type { ProposedIssue } from '../generated/proposal';

export type { ProposedIssue };

/** A ProposedPlan with a plain array of issues (the generated tuple type is awkward to edit). */
export interface Draft {
  epic_title: string;
  epic_body: string;
  issues: ProposedIssue[];
  notes?: string;
}

export interface PlanApproval {
  status: string;
  via: string | null;
  approved_by: string | null;
  note: string | null;
}

export interface PlanOut {
  repo: string;
  path: string | null;
  issue: number | null;
  ref: string | null;
  sha: string;
  title: string;
  markdown: string;
  headings: string[];
  approval: PlanApproval | null;
}

export interface ProposalProblem {
  code: string;
  key: string | null;
  message: string;
}

export interface ProposalOut {
  id: string;
  repo: string;
  status: string;
  plan_sha: string;
  plan: string;
  rev: number;
  saved: boolean;
  save_error: string | null;
  proposal: Draft | null;
  issues_validation: ProposalProblem[];
}

export interface ValidationOut {
  rev: number;
  ok: boolean;
  issues: ProposalProblem[];
}

export interface DecomposeAccepted {
  proposal_id: string;
  job_id: string;
}

export interface JobResult {
  epic_number: number;
  issues: Record<string, number>;
  created: string[];
  reused: string[];
  links_created: number;
  labels_ensured: string[];
}

export interface JobOut {
  job_id: string;
  kind: string;
  proposal_id: string;
  status: 'running' | 'done' | 'failed';
  result: JobResult | null;
  error: string | null;
  issues: ProposalProblem[];
}

export interface AgentOption {
  name: string;
  enabled: boolean;
  caps: string[];
}

export interface OptionsOut {
  agents: AgentOption[];
  playbooks: string[];
}

/** Where the plan comes from: a file in the repo or an issue. */
export interface PlanSource {
  repo: string;
  kind: 'path' | 'issue';
  value: string;
}

export const plannerKeys = {
  all: ['planner'] as const,
  plan: (s: PlanSource) => ['planner', 'plan', s.repo, s.kind, s.value] as const,
  options: (repo: string) => ['planner', 'options', repo] as const,
  proposal: (id: string) => ['planner', 'proposal', id] as const,
  job: (id: string) => ['planner', 'job', id] as const,
};

export function sourceParams(s: PlanSource): string {
  const params = new URLSearchParams({ repo: s.repo });
  params.set(s.kind, s.value);
  return params.toString();
}

/** The source value is usable: a non-empty path, or a positive issue number. */
export function sourceReady(s: PlanSource): boolean {
  if (!s.repo.trim() || !s.value.trim()) return false;
  return s.kind === 'path' || /^\d+$/.test(s.value.trim());
}

export function usePlan(source: PlanSource | null) {
  return useQuery<PlanOut>({
    queryKey: source ? plannerKeys.plan(source) : ['planner', 'plan', 'none'],
    queryFn: () => apiGet(`/api/planner/plan?${sourceParams(source as PlanSource)}`),
    enabled: !!source,
    retry: false,
  });
}

export function usePlannerOptions(repo: string | null) {
  return useQuery<OptionsOut>({
    queryKey: plannerKeys.options(repo ?? ''),
    queryFn: () => apiGet(`/api/planner/options?repo=${encodeURIComponent(repo as string)}`),
    enabled: !!repo,
    retry: false,
  });
}

export function useProposal(id: string | null) {
  return useQuery<ProposalOut>({
    queryKey: plannerKeys.proposal(id ?? ''),
    queryFn: () => apiGet(`/api/planner/proposals/${encodeURIComponent(id as string)}`),
    enabled: !!id,
    retry: false,
    // SSE planner.updated is the primary signal; this only covers a missed event while work is in flight.
    refetchInterval: (query) => {
      const d = query.state.data;
      if (!d) return false;
      return d.status === 'drafting' || d.status === 'creating' || !d.saved ? 2000 : false;
    },
  });
}

export function useJob(id: string | null) {
  return useQuery<JobOut>({
    queryKey: plannerKeys.job(id ?? ''),
    queryFn: () => apiGet(`/api/planner/jobs/${encodeURIComponent(id as string)}`),
    enabled: !!id,
    retry: false,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false),
  });
}

/** The human text of an API error: the `message` of a JSON detail, else the detail itself. */
export function errorText(err: unknown): string {
  const detail = (err as { detail?: unknown })?.detail;
  if (typeof detail === 'string') {
    try {
      const parsed = JSON.parse(detail);
      if (parsed && typeof parsed.message === 'string') return parsed.message;
    } catch {
      // not JSON
    }
    return detail;
  }
  return err instanceof Error ? err.message : String(err);
}
