import { WidgetProps, ObjectFieldTemplateProps } from '@rjsf/utils';
import { Field } from '../../ui/Field';

// A custom theme for @rjsf that uses our Gargi Labs tokens (border-radius: 0, etc.)

export function BaseInputTemplate(props: WidgetProps) {
  const { id, label, value, required, onChange, rawErrors } = props;
  const isJinja = id.includes('prompt') || id.includes('message');

  const Component = isJinja ? 'textarea' : 'input';

  return (
    <Field label={label} required={required} error={rawErrors?.[0]}>
      <Component
        id={id}
        value={value || ''}
        required={required}
        onChange={(e: any) => onChange(e.target.value === '' ? undefined : e.target.value)}
        style={{
          width: '100%',
          padding: '8px',
          fontFamily: isJinja ? '"JetBrains Mono", monospace' : 'var(--font-sans)',
          fontSize: '14px',
          border: '1px solid var(--ink)',
          background: 'var(--bg)',
          color: 'var(--ink)',
          outline: 'none',
          minHeight: isJinja ? '100px' : 'auto',
          resize: isJinja ? 'vertical' : 'none'
        }}
      />
    </Field>
  );
}

export function CheckboxWidget(props: WidgetProps) {
  const { id, label, value, required, onChange, rawErrors } = props;
  return (
    <Field label={label} required={required} error={rawErrors?.[0]}>
      <input
        type="checkbox"
        id={id}
        checked={!!value}
        onChange={(e) => onChange(e.target.checked)}
      />
    </Field>
  );
}

export function SelectWidget(props: WidgetProps) {
  const { id, label, value, required, onChange, options, rawErrors } = props;
  return (
    <Field label={label} required={required} error={rawErrors?.[0]}>
      <select
        id={id}
        value={value || ''}
        required={required}
        onChange={(e) => onChange(e.target.value === '' ? undefined : e.target.value)}
        style={{
          width: '100%',
          padding: '8px',
          fontFamily: 'var(--font-sans)',
          fontSize: '14px',
          border: '1px solid var(--ink)',
          background: 'var(--bg)',
          color: 'var(--ink)',
          outline: 'none'
        }}
      >
        <option value="">-- select --</option>
        {(options.enumOptions || []).map((opt: any) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </Field>
  );
}

export function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {props.properties.map(element => (
        <div key={element.content.key}>
          {element.content}
        </div>
      ))}
    </div>
  );
}
