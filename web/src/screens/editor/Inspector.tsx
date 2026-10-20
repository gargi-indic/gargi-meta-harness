import { useState, useEffect } from 'react';
import Form from '@rjsf/core';
import validator from '@rjsf/validator-ajv8';
import { BaseInputTemplate, CheckboxWidget, SelectWidget, ObjectFieldTemplate } from './rjsf-theme';
import playbookSchema from '../../generated/playbook.schema.json';
import { Field } from '../../ui/Field';

interface InspectorProps {
  selectedNodeId: string | null;
  playbook: any;
  onUpdateNode: (id: string, patch: any) => void;
  onUpdateGates: (gates: any) => void;
  onUpdateLimits: (limits: any) => void;
  onRenameNode: (oldId: string, newId: string) => void;
}

const widgets = {
  TextWidget: BaseInputTemplate,
  CheckboxWidget: CheckboxWidget,
  SelectWidget: SelectWidget
};

const templates = {
  ObjectFieldTemplate: ObjectFieldTemplate
};

export function Inspector({ selectedNodeId, playbook, onUpdateNode, onUpdateGates, onUpdateLimits, onRenameNode }: InspectorProps) {
  const [localId, setLocalId] = useState(selectedNodeId || '');

  useEffect(() => {
    setTimeout(() => {
      setLocalId(selectedNodeId || '');
    }, 0);
  }, [selectedNodeId]);

  let schema = null;
  if (selectedNodeId) {
    const node = playbook?.nodes?.[selectedNodeId];
    if (node && node.kind) {
      const defs = playbookSchema.$defs as Record<string, any>;
      for (const defSchema of Object.values(defs)) {
        if (defSchema && defSchema.properties && defSchema.properties.kind && defSchema.properties.kind.const === node.kind) {
          schema = {
            ...defSchema,
            $defs: playbookSchema.$defs
          };
          break;
        }
      }
    }
  }

  // Ignore unused params
  void onUpdateGates;
  void onUpdateLimits;

  if (!selectedNodeId) {
    return (
      <div style={{ padding: 16 }}>
        <h4>Playbook Settings</h4>
        {/* Placeholder for global settings like gates/limits */}
        <p style={{ color: 'var(--muted)' }}>Select a node to inspect it.</p>
      </div>
    );
  }

  const node = playbook.nodes?.[selectedNodeId];
  if (!node) return null;

  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 16 }}>
        <Field label="Node ID">
          <input
            value={localId}
            onChange={(e) => setLocalId(e.target.value)}
            onBlur={() => {
              if (localId !== selectedNodeId && localId.trim() !== '') {
                onRenameNode(selectedNodeId, localId.trim());
              } else {
                setLocalId(selectedNodeId);
              }
            }}
            style={{
              width: '100%',
              padding: '8px',
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: '14px',
              border: '1px solid var(--ink)',
              background: 'var(--bg)',
              color: 'var(--ink)'
            }}
          />
        </Field>
      </div>

      {schema ? (
        <Form
          schema={schema}
          validator={validator}
          formData={node}
          onChange={(e) => onUpdateNode(selectedNodeId, e.formData)}
          widgets={widgets}
          templates={templates}
          showErrorList={false}
          liveValidate={true}
        >
          <div /> {/* Hide default submit button */}
        </Form>
      ) : (
        <p style={{ color: 'var(--muted)' }}>No schema available for kind: {node.kind}</p>
      )}
    </div>
  );
}
