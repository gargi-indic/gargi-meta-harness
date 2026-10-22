import { Playbook } from '../../generated/playbook';

export interface EditorState {
  playbook: Playbook | any;
  yaml: string; // If we want to hold text
  selectedNodeId: string | null;
  isDirty: boolean;
  history: Playbook[];
  future: Playbook[];
}

export type EditorAction =
  | { type: 'SET_PLAYBOOK'; playbook: any }
  | { type: 'ADD_NODE'; kind: string }
  | { type: 'REMOVE_NODE'; id: string }
  | { type: 'RENAME_NODE'; oldId: string; newId: string }
  | { type: 'CONNECT'; source: string; port: string | null; target: string }
  | { type: 'DISCONNECT'; id: string }
  | { type: 'UPDATE_NODE'; id: string; patch: any }
  | { type: 'UPDATE_GATES'; gates: any }
  | { type: 'UPDATE_LIMITS'; limits: any }
  | { type: 'UNDO' }
  | { type: 'REDO' }
  | { type: 'SELECT_NODE'; id: string | null }
  | { type: 'SET_YAML'; yaml: string }
  | { type: 'SYNC_FROM_YAML'; playbook: any }
  | { type: 'MARK_SAVED' };

const MAX_HISTORY = 50;

/** Required-field defaults per node kind (from the playbook JSON schema) so a fresh node is renderable. */
export const NODE_DEFAULTS: Record<string, Record<string, unknown>> = {
  agent: { session: 'main', action: 'plan' },
  review: { target: 'plan', checklist: [] },
  check: { check: 'ci' },
  gate: { gate: 'merge' },
  branch: { cases: [], default: 'else' },
  parallel: { branches: [] },
  join: { mode: 'all' },
  decompose: {},
  merge: {},
  escalate: {},
  notify: { message: '' },
};

function pushHistory(state: EditorState, newPlaybook: any): EditorState {
  const history = [...state.history, state.playbook].slice(-MAX_HISTORY);
  return {
    ...state,
    playbook: newPlaybook,
    history,
    future: [],
    isDirty: true,
  };
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export const initialEditorState = (playbook: any = { apiVersion: 'harness/v1', id: 'new-playbook', nodes: {}, edges: {} }): EditorState => ({
  playbook,
  yaml: '',
  selectedNodeId: null,
  isDirty: false,
  history: [],
  future: [],
});

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case 'SET_PLAYBOOK':
      return {
        ...state,
        playbook: action.playbook,
        history: [],
        future: [],
        isDirty: false,
      };

    case 'SELECT_NODE':
      if (state.selectedNodeId === action.id) return state;
      return {
        ...state,
        selectedNodeId: action.id,
      };

    case 'ADD_NODE': {
      const pb = state.playbook;
      const nodes = pb.nodes || {};

      // find unique id
      let n = 1;
      let newId = `${action.kind}_${n}`;
      while (nodes[newId]) {
        n++;
        newId = `${action.kind}_${n}`;
      }

      const nextPb = {
        ...pb,
        nodes: {
          ...nodes,
          [newId]: { kind: action.kind, ...structuredClone(NODE_DEFAULTS[action.kind] ?? {}) }
        }
      };

      return pushHistory(state, nextPb);
    }

    case 'REMOVE_NODE': {
      const pb = state.playbook;
      if (!pb.nodes || !(action.id in pb.nodes)) return state;
      const nodes = { ...pb.nodes };
      delete nodes[action.id];

      const edges: Record<string, string> = {};
      if (pb.edges) {
        for (const [k, v] of Object.entries(pb.edges)) {
          const source = k.split('.')[0];
          if (source !== action.id && v !== action.id) {
            edges[k] = v as string;
          }
        }
      }

      return {
        ...pushHistory(state, { ...pb, nodes, edges }),
        selectedNodeId: state.selectedNodeId === action.id ? null : state.selectedNodeId,
      };
    }

    case 'RENAME_NODE': {
      const { oldId, newId } = action;
      const pb = state.playbook;
      const nodes = pb.nodes || {};

      if (!nodes[oldId] || nodes[newId]) return state; // ignore if missing or name collision

      const nextNodes: Record<string, any> = {};
      for (const [k, v] of Object.entries(nodes)) {
        if (k === oldId) {
          nextNodes[newId] = v;
        } else {
          nextNodes[k] = v;
        }
      }

      const nextEdges: Record<string, string> = {};
      if (pb.edges) {
        for (const [k, v] of Object.entries(pb.edges)) {
          let [source, port] = [k, ""];
          if (k.startsWith("*.")) {
            port = k.substring(2);
            source = "*.";
          } else if (k.includes(".")) {
            const parts = k.split('.');
            source = parts[0];
            port = parts.slice(1).join('.');
          }

          let nextK = k;
          let nextV = v as string;

          if (source === oldId) {
            nextK = port ? `${newId}.${port}` : newId;
          }
          if (nextV === oldId) {
            nextV = newId;
          }

          nextEdges[nextK] = nextV;
        }
      }

      let selectedNodeId = state.selectedNodeId;
      if (selectedNodeId === oldId) selectedNodeId = newId;

      return {
        ...pushHistory(state, { ...pb, nodes: nextNodes, edges: nextEdges }),
        selectedNodeId
      };
    }

    case 'CONNECT': {
      const { source, port, target } = action;
      const pb = state.playbook;
      const edges = { ...pb.edges };
      if (!port) return state; // edges are keyed `source.port`; a portless source cannot be wired
      const edgeKey = `${source}.${port}`;
      if (edges[edgeKey] === target) return state;
      edges[edgeKey] = target;

      return pushHistory(state, { ...pb, edges });
    }

    case 'DISCONNECT': {
      const pb = state.playbook;
      const edges = { ...pb.edges };
      if (!(action.id in edges)) return state;
      delete edges[action.id];
      return pushHistory(state, { ...pb, edges });
    }

    case 'UPDATE_NODE': {
      const pb = state.playbook;
      const nodes = { ...pb.nodes };
      if (!nodes[action.id]) return state;

      const merged = { ...nodes[action.id], ...action.patch };
      if (same(merged, nodes[action.id])) return state;
      nodes[action.id] = merged;
      return pushHistory(state, { ...pb, nodes });
    }

    case 'UPDATE_GATES': {
      const pb = state.playbook;
      if (same(pb.gates, action.gates)) return state;
      return pushHistory(state, { ...pb, gates: action.gates });
    }

    case 'UPDATE_LIMITS': {
      const pb = state.playbook;
      if (same(pb.limits, action.limits)) return state;
      return pushHistory(state, { ...pb, limits: action.limits });
    }

    case 'UNDO': {
      if (state.history.length === 0) return state;
      const prevPlaybook = state.history[state.history.length - 1];
      const history = state.history.slice(0, -1);
      const future = [state.playbook, ...state.future];

      return {
        ...state,
        playbook: prevPlaybook,
        history,
        future,
        isDirty: true,
      };
    }

    case 'REDO': {
      if (state.future.length === 0) return state;
      const nextPlaybook = state.future[0];
      const future = state.future.slice(1);
      const history = [...state.history, state.playbook];

      return {
        ...state,
        playbook: nextPlaybook,
        history,
        future,
        isDirty: true,
      };
    }

    case 'SET_YAML': {
      return {
        ...state,
        yaml: action.yaml
      };
    }

    case 'SYNC_FROM_YAML': {
      if (same(state.playbook, action.playbook)) return state;
      return pushHistory(state, action.playbook);
    }

    case 'MARK_SAVED':
      return { ...state, isDirty: false };

    default:
      return state;
  }
}
