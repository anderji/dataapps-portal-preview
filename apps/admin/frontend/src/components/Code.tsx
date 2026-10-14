/** Monospace identifier (app ID, email, reason code), muted when it is secondary to a name. */
import type { ReactNode } from 'react';

export function Code({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return (
    <code className={muted ? 'tw:font-mono tw:text-xs tw:text-muted-foreground' : 'tw:font-mono tw:text-xs'}>
      {children}
    </code>
  );
}

/** A name with its identifier underneath, the way every table's first column reads. */
export function NameWithId({ name, id, extra }: { name: ReactNode; id: ReactNode; extra?: ReactNode }) {
  return (
    <div className="tw:flex tw:flex-col tw:gap-0.5">
      <span className="tw:flex tw:items-center tw:gap-2">
        {name}
        {extra}
      </span>
      <Code muted>{id}</Code>
    </div>
  );
}

/** Secondary explanatory text. */
export function Muted({ children }: { children: ReactNode }) {
  return <p className="tw:text-sm tw:text-muted-foreground">{children}</p>;
}
