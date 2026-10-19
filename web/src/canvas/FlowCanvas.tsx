import React, { useEffect, useState, useMemo, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  Node as ReactFlowNode,
  Connection,
  MarkerType,
  useReactFlow,
  ReactFlowProvider,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './canvas.css';

import { CanvasNode, CanvasEdge } from './types';
import { NodeCard } from './nodes/NodeCard';
import { CustomEdge } from './edges';
import { layoutGraph } from './layout';

export interface FlowCanvasProps {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  mode: "view" | "edit";
  direction?: "RIGHT" | "DOWN";
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  onConnect?: (c: { source: string; target: string; sourcePort: string | null }) => void;
  onNodesDelete?: (ids: string[]) => void;
  onEdgesDelete?: (ids: string[]) => void;
  onNodeMove?: (id: string, pos: { x: number; y: number }) => void;
  positions?: Record<string, { x: number; y: number }>;
  renderNode?: (n: CanvasNode) => React.ReactNode;
  height?: number | string;
}

const nodeTypes = {
  customNode: NodeCard as any,
};

const edgeTypes = {
  customEdge: CustomEdge as any,
};

function FlowCanvasInner(props: FlowCanvasProps) {
  const {
    nodes,
    edges,
    mode,
    direction = "RIGHT",
    selectedId,
    onSelect,
    onConnect,
    onNodesDelete,
    onEdgesDelete,
    onNodeMove,
    positions: controlledPositions,
    height = "100%",
  } = props;

  const [layoutPositions, setLayoutPositions] = useState<Record<string, { x: number; y: number }>>({});
  const { fitView } = useReactFlow();

  useEffect(() => {
    if (controlledPositions) return;

    let isActive = true;
    layoutGraph(nodes, edges, { direction }).then((pos) => {
      if (isActive) {
        setLayoutPositions(pos);
        // Give React Flow a tick to render nodes before fitting view
        setTimeout(() => {
          if (isActive) fitView({ padding: 0.2 });
        }, 50);
      }
    });

    return () => {
      isActive = false;
    };
  }, [nodes, edges, direction, controlledPositions, fitView]);

  const activePositions = controlledPositions || layoutPositions;

  const rfNodes: ReactFlowNode[] = useMemo(() => {
    return nodes.map(n => {
      const pos = activePositions[n.id] || { x: 0, y: 0 };
      return {
        id: n.id,
        type: 'customNode',
        position: pos,
        data: n as any,
        selected: n.id === selectedId,
        draggable: mode === "edit",
      };
    });
  }, [nodes, activePositions, selectedId, mode]);

  const rfEdges: Edge[] = useMemo(() => {
    return edges.map(e => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourcePort,
      type: 'customEdge',
      data: e as any,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: "var(--ink)",
      },
      animated: false, // never animated per spec
    }));
  }, [edges]);

  const handleConnect = useCallback((connection: Connection) => {
    if (mode === "edit" && onConnect) {
      onConnect({
        source: connection.source,
        target: connection.target,
        sourcePort: connection.sourceHandle,
      });
    }
  }, [mode, onConnect]);

  const handleSelectionChange = useCallback(({ nodes: selNodes }: { nodes: ReactFlowNode[] }) => {
    if (onSelect) {
      if (selNodes.length > 0) {
        onSelect(selNodes[0].id);
      } else {
        onSelect(null);
      }
    }
  }, [onSelect]);

  const handleNodesDelete = useCallback((deleted: ReactFlowNode[]) => {
    if (mode === "edit" && onNodesDelete) {
      onNodesDelete(deleted.map(n => n.id));
    }
  }, [mode, onNodesDelete]);

  const handleEdgesDelete = useCallback((deleted: Edge[]) => {
    if (mode === "edit" && onEdgesDelete) {
      onEdgesDelete(deleted.map(e => e.id));
    }
  }, [mode, onEdgesDelete]);

  const handleNodeDragStop = useCallback((_event: React.MouseEvent, node: ReactFlowNode) => {
    if (mode === "edit" && onNodeMove) {
      onNodeMove(node.id, node.position);
    }
  }, [mode, onNodeMove]);

  return (
    <div style={{ width: '100%', height }}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onConnect={handleConnect}
        onSelectionChange={handleSelectionChange}
        onNodesDelete={handleNodesDelete}
        onEdgesDelete={handleEdgesDelete}
        onNodeDragStop={handleNodeDragStop as any}
        nodesDraggable={mode === "edit"}
        nodesConnectable={mode === "edit"}
        elementsSelectable={true}
        deleteKeyCode={mode === "edit" ? ["Backspace", "Delete"] : null}
        minZoom={0.1}
        maxZoom={2}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={2} color="var(--line-soft)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function FlowCanvas(props: FlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <FlowCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
