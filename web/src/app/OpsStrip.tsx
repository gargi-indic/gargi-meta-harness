import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useOpsStatus, queryKeys } from '../api/queries';
import { killAll, resumeAll, errorText, usd } from '../api/ops';
import { Button } from '../ui/Button';
import { Field } from '../ui/Field';
import { StatusTag } from '../ui/StatusTag';

export function OpsStrip() {
  const { data } = useOpsStatus();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');
  const [error, setError] = useState<string | null>(null);

  const done = () => {
    setConfirming(false);
    setTyped('');
    setError(null);
    queryClient.invalidateQueries({ queryKey: queryKeys.ops });
    queryClient.invalidateQueries({ queryKey: queryKeys.agentList });
  };
  const kill = useMutation({ mutationFn: killAll, onSuccess: done, onError: (e) => setError(errorText(e)) });
  const resume = useMutation({ mutationFn: resumeAll, onSuccess: done, onError: (e) => setError(errorText(e)) });

  if (!data) return null;
  const { budget, halted } = data;
  const spend =
    budget.limit_usd === null ? `SPEND ${usd(budget.spent_usd)}` : `SPEND ${usd(budget.spent_usd)} / ${usd(budget.limit_usd)}`;

  return (
    <div className="ops-strip" aria-label="Operations">
      <span className="mono">{spend}</span>
      {halted && <StatusTag tone="fail">HALTED</StatusTag>}
      {halted ? (
        <Button onClick={() => resume.mutate()} disabled={resume.isPending}>Resume</Button>
      ) : confirming ? (
        <span className="ops-confirm">
          <Field label="Type KILL to confirm" value={typed} onChange={setTyped} autoComplete="off" />
          <Button className="ops-kill" disabled={typed !== 'KILL' || kill.isPending} onClick={() => kill.mutate()}>
            Confirm kill
          </Button>
          <Button onClick={() => { setConfirming(false); setTyped(''); setError(null); }}>Cancel</Button>
        </span>
      ) : (
        <Button className="ops-kill" onClick={() => setConfirming(true)}>Kill all</Button>
      )}
      {error && <span className="field-error mono" role="alert">{error}</span>}
    </div>
  );
}
