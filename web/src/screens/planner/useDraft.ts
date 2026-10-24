import { useCallback, useEffect, useReducer } from 'react';
import { apiPut, ApiError } from '../../api/client';
import { errorText, type Draft, type ProposalOut, type ProposalProblem } from '../../api/planner';

export const SAVE_DEBOUNCE_MS = 600;

export interface DraftState {
  proposalId: string | null;
  draft: Draft | null;
  /** The rev the draft is based on; sent back as If-Match. */
  rev: number;
  editSeq: number;
  savedSeq: number;
  failedSeq: number;
  saving: boolean;
  conflict: boolean;
  saveError: string | null;
  /** The server has the newest rev on GitHub (`saved` of the proposal). */
  serverSaved: boolean;
  status: string;
  issues: ProposalProblem[];
}

export const emptyDraftState: DraftState = {
  proposalId: null,
  draft: null,
  rev: 0,
  editSeq: 0,
  savedSeq: 0,
  failedSeq: -1,
  saving: false,
  conflict: false,
  saveError: null,
  serverSaved: true,
  status: '',
  issues: [],
};

type Action =
  | { type: 'load'; data: ProposalOut; force?: boolean }
  | { type: 'clear' }
  | { type: 'edit'; draft: Draft | ((current: Draft) => Draft) }
  | { type: 'saveStart' }
  | { type: 'saveOk'; data: ProposalOut; seq: number }
  | { type: 'saveConflict' }
  | { type: 'saveFail'; message: string; seq: number };

export function isDirty(s: DraftState): boolean {
  return s.editSeq !== s.savedSeq;
}

export function draftReducer(s: DraftState, a: Action): DraftState {
  switch (a.type) {
    case 'clear':
      return emptyDraftState;
    case 'load': {
      const sameProposal = s.proposalId === a.data.id;
      // Local edits win over a server copy that arrives while they are unsaved or being saved.
      if (sameProposal && !a.force && (isDirty(s) || s.saving)) return s;
      // A response that was in flight while a newer rev was saved must not roll the draft back.
      if (sameProposal && !a.force && a.data.rev < s.rev) return s;
      return {
        ...s,
        proposalId: a.data.id,
        draft: a.data.proposal,
        rev: a.data.rev,
        editSeq: sameProposal && !a.force ? s.editSeq : 0,
        savedSeq: sameProposal && !a.force ? s.savedSeq : 0,
        failedSeq: -1,
        conflict: a.force ? false : s.conflict && sameProposal,
        saveError: a.data.save_error,
        serverSaved: a.data.saved,
        status: a.data.status,
        issues: a.data.issues_validation,
      };
    }
    case 'edit': {
      if (!s.draft) return s;
      const next = typeof a.draft === 'function' ? a.draft(s.draft) : a.draft;
      return { ...s, draft: next, editSeq: s.editSeq + 1 };
    }
    case 'saveStart':
      return { ...s, saving: true, saveError: null };
    case 'saveOk':
      return {
        ...s,
        saving: false,
        rev: a.data.rev,
        savedSeq: a.seq,
        saveError: a.data.save_error,
        serverSaved: a.data.saved,
        status: a.data.status,
        issues: a.data.issues_validation,
      };
    case 'saveConflict':
      return { ...s, saving: false, conflict: true };
    case 'saveFail':
      return { ...s, saving: false, saveError: a.message, failedSeq: a.seq };
  }
}

/**
 * The editable copy of a proposal. Edits are saved with a debounced `PUT /api/planner/proposals/{id}` carrying
 * `If-Match: "<rev>"`; a 412 sets `conflict` (the draft changed elsewhere) and stops saving until `reload`.
 */
export function useDraft(onSaved?: () => void) {
  const [state, dispatch] = useReducer(draftReducer, emptyDraftState);
  const { proposalId, draft, rev, editSeq, savedSeq, failedSeq, saving, conflict } = state;
  const dirty = editSeq !== savedSeq;

  useEffect(() => {
    if (!dirty || saving || conflict || !draft || !proposalId || failedSeq === editSeq) return;
    const seq = editSeq;
    const timer = setTimeout(() => {
      dispatch({ type: 'saveStart' });
      apiPut<ProposalOut>(`/api/planner/proposals/${encodeURIComponent(proposalId)}`, draft, {
        'If-Match': `"${rev}"`,
      })
        .then((data) => {
          dispatch({ type: 'saveOk', data, seq });
          onSaved?.();
        })
        .catch((err: unknown) => {
          if (err instanceof ApiError && err.status === 412) dispatch({ type: 'saveConflict' });
          else dispatch({ type: 'saveFail', message: errorText(err), seq });
        });
    }, SAVE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [dirty, saving, conflict, draft, proposalId, rev, editSeq, failedSeq, onSaved]);

  const load = useCallback((data: ProposalOut, force = false) => dispatch({ type: 'load', data, force }), []);
  const edit = useCallback((next: Draft | ((current: Draft) => Draft)) => dispatch({ type: 'edit', draft: next }), []);
  const clear = useCallback(() => dispatch({ type: 'clear' }), []);

  return { state, dirty, load, edit, clear };
}
