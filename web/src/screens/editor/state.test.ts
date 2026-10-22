import { describe, test, expect } from 'vitest';
import { editorReducer, initialEditorState, EditorState, EditorAction } from './state';

const base = () => ({
  apiVersion: 'harness/v1',
  id: 'pb',
  nodes: {
    a: { kind: 'check', check: 'ci' },
    b: { kind: 'merge', method: 'squash' },
  },
  edges: { 'a.pass': 'b', 'a.fail': '@done', '*.timeout': 'b', 'b.merged': 'a' } as Record<string, string>,
});

function run(state: EditorState, ...actions: EditorAction[]) {
  return actions.reduce(editorReducer, state);
}

describe('editorReducer', () => {
  test('addNode creates unique ids <kind>_<n> and marks dirty', () => {
    let s = initialEditorState(base());
    s = run(s, { type: 'ADD_NODE', kind: 'agent' }, { type: 'ADD_NODE', kind: 'agent' });
    expect(Object.keys(s.playbook.nodes)).toEqual(['a', 'b', 'agent_1', 'agent_2']);
    expect(s.playbook.nodes.agent_1.kind).toBe('agent');
    expect(s.isDirty).toBe(true);
  });

  test('removeNode deletes node and all connected edges', () => {
    const s = run(initialEditorState(base()), { type: 'REMOVE_NODE', id: 'b' });
    expect(s.playbook.nodes.b).toBeUndefined();
    expect(s.playbook.edges).toEqual({ 'a.fail': '@done' });
    expect(s.playbook.edges['a.pass']).toBeUndefined();
    expect(s.playbook.edges['b.merged']).toBeUndefined();
  });

  test('removeNode of unknown id is a no-op', () => {
    const s0 = initialEditorState(base());
    expect(run(s0, { type: 'REMOVE_NODE', id: '@done' })).toBe(s0);
  });

  test('renameNode updates node key, edge keys and edge targets', () => {
    const s = run(initialEditorState(base()), { type: 'SELECT_NODE', id: 'a' }, { type: 'RENAME_NODE', oldId: 'a', newId: 'z' });
    expect(Object.keys(s.playbook.nodes)).toEqual(['z', 'b']);
    expect(s.playbook.edges).toEqual({
      'z.pass': 'b',
      'z.fail': '@done',
      '*.timeout': 'b',
      'b.merged': 'z',
    });
    expect(s.selectedNodeId).toBe('z');
  });

  test('renameNode ignores collisions and unknown nodes', () => {
    const s0 = initialEditorState(base());
    expect(run(s0, { type: 'RENAME_NODE', oldId: 'a', newId: 'b' })).toBe(s0);
    expect(run(s0, { type: 'RENAME_NODE', oldId: 'nope', newId: 'c' })).toBe(s0);
  });

  test('connect adds or updates an edge', () => {
    let s = run(initialEditorState(base()), { type: 'CONNECT', source: 'b', port: 'failed', target: 'a' });
    expect(s.playbook.edges['b.failed']).toBe('a');
    s = run(s, { type: 'CONNECT', source: 'b', port: 'failed', target: '@done' });
    expect(s.playbook.edges['b.failed']).toBe('@done');
  });

  test('disconnect removes an edge', () => {
    const s = run(initialEditorState(base()), { type: 'DISCONNECT', id: 'a.pass' });
    expect('a.pass' in s.playbook.edges).toBe(false);
    expect(s.isDirty).toBe(true);
  });

  test('updateNode merges patch; updateGates / updateLimits set top-level fields', () => {
    const s = run(
      initialEditorState(base()),
      { type: 'UPDATE_NODE', id: 'a', patch: { max_visits: 3 } },
      { type: 'UPDATE_GATES', gates: { merge: 'human' } },
      { type: 'UPDATE_LIMITS', limits: { timeout: '1h' } },
    );
    expect(s.playbook.nodes.a).toEqual({ kind: 'check', check: 'ci', max_visits: 3 });
    expect(s.playbook.gates).toEqual({ merge: 'human' });
    expect(s.playbook.limits).toEqual({ timeout: '1h' });
  });

  test('no-op updates do not mark dirty', () => {
    const s0 = initialEditorState(base());
    expect(run(s0, { type: 'UPDATE_NODE', id: 'a', patch: { check: 'ci' } })).toBe(s0);
  });

  test('undo and redo', () => {
    let s = initialEditorState(base());
    s = run(s, { type: 'ADD_NODE', kind: 'gate' });
    expect(s.playbook.nodes.gate_1).toBeDefined();
    s = run(s, { type: 'UNDO' });
    expect(s.playbook.nodes.gate_1).toBeUndefined();
    expect(s.future).toHaveLength(1);
    s = run(s, { type: 'REDO' });
    expect(s.playbook.nodes.gate_1).toBeDefined();
    expect(s.future).toHaveLength(0);
    // new edit clears redo stack
    s = run(s, { type: 'UNDO' }, { type: 'ADD_NODE', kind: 'notify' });
    expect(s.future).toHaveLength(0);
  });

  test('undo/redo on empty stacks is a no-op', () => {
    const s0 = initialEditorState(base());
    expect(run(s0, { type: 'UNDO' })).toBe(s0);
    expect(run(s0, { type: 'REDO' })).toBe(s0);
  });

  test('history is capped at 50 entries', () => {
    let s = initialEditorState(base());
    for (let i = 0; i < 60; i++) s = run(s, { type: 'ADD_NODE', kind: 'agent' });
    expect(s.history).toHaveLength(50);
    for (let i = 0; i < 60; i++) s = run(s, { type: 'UNDO' });
    expect(s.future).toHaveLength(50);
    expect(Object.keys(s.playbook.nodes)).toHaveLength(2 + 10);
  });

  test('SET_PLAYBOOK and MARK_SAVED clear dirty', () => {
    let s = run(initialEditorState(base()), { type: 'ADD_NODE', kind: 'agent' });
    expect(s.isDirty).toBe(true);
    s = run(s, { type: 'MARK_SAVED' });
    expect(s.isDirty).toBe(false);
    s = run(s, { type: 'ADD_NODE', kind: 'agent' }, { type: 'SET_PLAYBOOK', playbook: base() });
    expect(s.isDirty).toBe(false);
    expect(s.history).toHaveLength(0);
  });
});
