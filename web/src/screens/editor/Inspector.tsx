import { useState } from 'react';
import Form from '@rjsf/core';
import validator from '@rjsf/validator-ajv8';
import { BaseInputTemplate, CheckboxWidget, SelectWidget, ObjectFieldTemplate } from './rjsf-theme';
import playbookSchema from '../../generated/playbook.schema.json';
import { Field } from '../../ui/Field';
import { Button } from '../../ui/Button';

export interface InspectorProps {
  selectedNodeId: string | null;
  playbook: any;
  onUpdateNode: (id: string, patch: any) => void;
  onUpdateGates: (gates: any) => void;
  onUpdateLimits: (limits: any) => void;
  onRenameNode: (oldId: string, newId: string) => void;
}

const widgets = {
  TextWidget: BaseInputTemplate,
  NumberWidget: BaseInputTemplate,
  TextareaWidget: BaseInputTemplate,
  CheckboxWidget,
  SelectWidget,
};

const templates = { ObjectFieldTemplate };

const uiSchema = { kind: { 'ui:widget': 'hidden' } };

function schemaForKind(kind: string | undefined) {
  if (!kind) return null;
  const defs = playbookSchema.$defs as Record<string, any>;
  for (const def of Object.values(defs)) {
    if (def?.properties?.kind?.const === kind) {
      return { ...def, $defs: playbookSchema.$defs };
    }
  }
  return null;
}

function NodeInspector({ nodeId, node, onUpdateNode, onRenameNode }: {
  nodeId: string;
  node: any;
  onUpdateNode: InspectorProps['onUpdateNode'];
  onRenameNode: InspectorProps['onRenameNode'];
}) {
  const [localId, setLocalId] = useState(nodeId);
  const schema = schemaForKind(node.kind);

  const commitRename = () => {
    const next = localId.trim();
    if (next && next !== nodeId) onRenameNode(nodeId, next);
    else setLocalId(nodeId);
  };

  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 16 }}>
        <Field
          label="Node ID"
          value={localId}
          onChange={setLocalId}
          onBlur={commitRename}
          onKeyDown={(e) => { if (e.key === 'Enter') commitRename(); }}
          style={{ fontFamily: '"JetBrains Mono", monospace' }}
        />
      </div>

      {schema ? (
        <Form
          schema={schema}
          uiSchema={uiSchema}
          validator={validator}
          formData={node}
          onChange={(e) => onUpdateNode(nodeId, e.formData)}
          widgets={widgets}
          templates={templates}
          showErrorList={false}
          liveValidate
        >
          <div /> {/* Hide default submit button */}
        </Form>
      ) : (
        <p style={{ color: 'var(--muted)' }}>No schema available for kind: {String(node.kind)}</p>
      )}
    </div>
  );
}

function PlaybookInspector({ playbook, onUpdateGates, onUpdateLimits }: Pick<InspectorProps, 'playbook' | 'onUpdateGates' | 'onUpdateLimits'>) {
  const [newGate, setNewGate] = useState('');
  const gates: Record<string, any> = playbook?.gates ?? {};
  const limits: Record<string, any> = playbook?.limits ?? {};

  const setLimit = (key: 'budget_usd' | 'timeout', value: string) => {
    const next: Record<string, any> = { ...limits };
    if (value === '') delete next[key];
    else next[key] = key === 'budget_usd' ? Number(value) : value;
    onUpdateLimits(Object.keys(next).length ? next : undefined);
  };

  return (
    <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <h4>Playbook settings</h4>
      <p style={{ color: 'var(--muted)', margin: 0 }}>Select a node to inspect it.</p>

      <div>
        <div className="field-label">Gates</div>
        {Object.entries(gates).map(([name, mode]) => (
          <div key={name} style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
            <span className="mono" style={{ flex: 1 }}>{name}</span>
            {typeof mode === 'string' ? (
              <select
                aria-label={`Gate ${name}`}
                className="field-input"
                style={{ width: 120 }}
                value={mode}
                onChange={(e) => onUpdateGates({ ...gates, [name]: e.target.value })}
              >
                <option value="human">human</option>
                <option value="auto">auto</option>
              </select>
            ) : (
              <span className="mono" style={{ color: 'var(--muted)' }}>{JSON.stringify(mode)}</span>
            )}
            <Button
              aria-label={`Remove gate ${name}`}
              onClick={() => {
                const next = { ...gates };
                delete next[name];
                onUpdateGates(Object.keys(next).length ? next : undefined);
              }}
            >
              Remove
            </Button>
          </div>
        ))}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', marginTop: 8 }}>
          <Field label="New gate" value={newGate} onChange={setNewGate} style={{}} />
          <Button
            disabled={!newGate.trim() || newGate.trim() in gates}
            onClick={() => {
              onUpdateGates({ ...gates, [newGate.trim()]: 'human' });
              setNewGate('');
            }}
          >
            Add gate
          </Button>
        </div>
      </div>

      <div>
        <div className="field-label">Limits</div>
        <Field
          label="Budget (USD)"
          type="number"
          value={limits.budget_usd === undefined || limits.budget_usd === null ? '' : String(limits.budget_usd)}
          onChange={(v) => setLimit('budget_usd', v)}
          style={{}}
        />
        <Field
          label="Timeout (e.g. 6h)"
          value={limits.timeout ?? ''}
          onChange={(v) => setLimit('timeout', v)}
          style={{}}
        />
      </div>
    </div>
  );
}

export function Inspector({ selectedNodeId, playbook, onUpdateNode, onUpdateGates, onUpdateLimits, onRenameNode }: InspectorProps) {
  if (!selectedNodeId) {
    return <PlaybookInspector playbook={playbook} onUpdateGates={onUpdateGates} onUpdateLimits={onUpdateLimits} />;
  }
  const node = playbook?.nodes?.[selectedNodeId];
  if (!node) return null;
  return (
    <NodeInspector
      key={selectedNodeId}
      nodeId={selectedNodeId}
      node={node}
      onUpdateNode={onUpdateNode}
      onRenameNode={onRenameNode}
    />
  );
}
