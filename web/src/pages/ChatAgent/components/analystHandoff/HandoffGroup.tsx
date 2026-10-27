import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RunGroup } from '../messageList/runCard';
import { AnalystGroupMark } from './AnalystMark';
import { handoffTally, type HandoffView } from './handoffModel';
import { HandoffSlip } from './HandoffSlip';

/**
 * Hand-offs made side by side, as one: a header line with the mark once, how
 * many, and where they stand, over a slip per hand-off with where it stands
 * in its avatar's place, since the same mark stacked would only repeat it.
 * Open until the reader folds it: a row is where its report link lives.
 */
export function HandoffGroup({ views }: { views: readonly HandoffView[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true);
  const { lead, parts } = handoffTally(views.map((v) => v.phase), t);

  return (
    // Narrow, the tally is cut before the header outgrows the reply.
    <div data-handoff-group className="@container">
      <RunGroup
        icon={<AnalystGroupMark />}
        title={t('chat.ptcCard.groupTitle', { count: views.length })}
        lead={lead}
        parts={parts}
        open={open}
        onToggle={() => setOpen((v) => !v)}
        // Until every row has read its run, the tally is the default.
        announce={{ read: views.every((v) => v.read) }}
      >
        <ul>
          {views.map((view) => <HandoffSlip key={view.id} view={view} grouped />)}
        </ul>
      </RunGroup>
    </div>
  );
}
