import { useId, useState } from 'react';

export interface FeedbackProps {
  scope: string;
  message: string;
  output?: string | null;
  tone?: 'info' | 'error';
}

export function Feedback({ scope, message, output, tone = 'info' }: FeedbackProps) {
  const label = useId();
  const outputId = useId();
  const [expanded, setExpanded] = useState(false);
  return <section className={`feedback feedback-${tone}`} aria-labelledby={label}>
    <div role="status" aria-live="polite" aria-atomic="true">
      <strong id={label}>{scope}</strong>{message && <span> — {message}</span>}
    </div>
    {output && <>
      <button type="button" aria-label={`${scope} output`} aria-expanded={expanded} aria-controls={outputId}
        onClick={() => setExpanded(value => !value)}>Details</button>
      <pre id={outputId} hidden={!expanded}>{output}</pre>
    </>}
  </section>;
}
