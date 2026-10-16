import { ReactNode } from 'react';

export type Tone = "live" | "muted" | "ok" | "warn" | "fail";

export const TONE_COLOR: Record<Tone, "--accent" | "--ink" | "--muted"> = {
  live: "--accent",
  fail: "--accent",
  ok: "--ink",
  warn: "--muted",
  muted: "--muted",
};

export interface StatusTagProps {
  tone: Tone;
  children: ReactNode;
}

export function StatusTag({ tone, children }: StatusTagProps) {
  return (
    <span className={`tag tag--${tone}`}>
      {children}
    </span>
  );
}
