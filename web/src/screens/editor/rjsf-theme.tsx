import { WidgetProps, ObjectFieldTemplateProps } from '@rjsf/utils';
import { Field } from '../../ui/Field';

// A custom theme for @rjsf that uses our Gargi Labs tokens (square corners, JetBrains Mono for Jinja fields).

const MONO = { fontFamily: '"JetBrains Mono", monospace' } as const;

function isJinjaField(id: string, label: string) {
  return /prompt|message/i.test(id) || /prompt|message/i.test(label);
}

export function BaseInputTemplate(props: WidgetProps) {
  const { id, label, value, required, onChange, rawErrors, schema } = props;
  const jinja = isJinjaField(id, label);
  const isNumber = schema?.type === 'number' || schema?.type === 'integer';

  return (
    <Field
      id={id}
      label={label}
      required={required}
      error={rawErrors?.[0]}
      multiline={jinja}
      type={jinja ? undefined : isNumber ? 'number' : 'text'}
      value={value === undefined || value === null ? '' : String(value)}
      style={jinja ? { ...MONO, minHeight: 100 } : {}}
      onChange={(v) => {
        if (v === '') return onChange(undefined);
        onChange(isNumber ? Number(v) : v);
      }}
    />
  );
}

export function CheckboxWidget(props: WidgetProps) {
  const { id, label, value, onChange } = props;
  return (
    <div className="field">
      <label htmlFor={id} className="field-label">{label}</label>
      <input type="checkbox" id={id} checked={!!value} onChange={(e) => onChange(e.target.checked)} />
    </div>
  );
}

export function SelectWidget(props: WidgetProps) {
  const { id, label, value, required, onChange, options, rawErrors } = props;
  return (
    <div className="field">
      <label htmlFor={id} className="field-label">{label}</label>
      <select
        id={id}
        className="field-input"
        style={{}}
        value={value ?? ''}
        required={required}
        onChange={(e) => onChange(e.target.value === '' ? undefined : e.target.value)}
      >
        <option value="">-- select --</option>
        {(options.enumOptions || []).map((opt) => (
          <option key={String(opt.value)} value={String(opt.value)}>{opt.label}</option>
        ))}
      </select>
      {rawErrors?.[0] && <span className="field-error" role="alert">{rawErrors[0]}</span>}
    </div>
  );
}

export function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {props.properties.map((element) => (
        <div key={element.name}>{element.content}</div>
      ))}
    </div>
  );
}
