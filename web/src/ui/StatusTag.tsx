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
  status?: string;
  tone?: Tone;
  children?: ReactNode;
}

export function StatusTag({ status, tone, children }: StatusTagProps) {
  let resolvedTone: Tone = tone || "muted";
  const content = children || status || "unknown";

  if (status) {
    switch (status.toLowerCase()) {
      case "running":
        resolvedTone = "live";
        break;
      case "needs-you":
        resolvedTone = "live";
        break;
      case "failed":
        resolvedTone = "fail";
        break;
      case "done":
        resolvedTone = "ok";
        break;
      case "waiting":
      case "pending":
      case "skipped":
      case "paused":
        resolvedTone = "muted";
        break;
      default:
        resolvedTone = "muted";
        break;
    }
  }

  return (
    <span className={`tag tag--${resolvedTone}`}>
      {content}
    </span>
  );
}
