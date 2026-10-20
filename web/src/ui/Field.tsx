import { InputHTMLAttributes, TextareaHTMLAttributes, useId } from 'react';

export interface FieldProps extends Omit<InputHTMLAttributes<HTMLInputElement> & TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'onChange'> {
  label: string;
  error?: string;
  multiline?: boolean;
  value?: string;
  onChange?: (val: string) => void;
}

export function Field({ label, error, id, multiline, value, onChange, ...props }: FieldProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const errorId = `${inputId}-error`;

  const commonProps = {
    id: inputId,
    className: "field-input",
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? errorId : undefined,
    value: value || '',
    onChange: (e: any) => onChange?.(e.target.value)
  };

  return (
    <div className="field">
      <label htmlFor={inputId} className="field-label">{label}</label>
      {multiline ? (
        <textarea {...commonProps} {...(props as any)} />
      ) : (
        <input {...commonProps} {...(props as any)} />
      )}
      {error && (
        <span id={errorId} className="field-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
