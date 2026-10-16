import { ButtonHTMLAttributes } from 'react';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary";
}

export function Button({ variant = "secondary", className = "", ...props }: ButtonProps) {
  const baseClass = `btn btn--${variant}`;
  return (
    <button type="button" className={`${baseClass} ${className}`.trim()} {...props} />
  );
}
