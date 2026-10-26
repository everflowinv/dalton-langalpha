/**
 * A labelled radiogroup of bordered option cards: a title, a short
 * description and an optional note (what is left, why it is locked, which one
 * is current).
 *
 * The computer pickers (tier and provider) share it, so a locked option reads
 * and behaves the same in both: faded, unselectable, skipped by the arrows.
 * Each card is a `RadioChoice`, whose native input owns the keyboard.
 */
import React, { useId } from 'react';

import { RadioChoice } from '@/components/ui/choice';
import { cn } from '@/lib/utils';

export interface RadioCardOption<V extends string> {
  value: V;
  title: string;
  /** A sentence under the title. */
  description?: string;
  /**
   * Short facts in place of a sentence, such as a spec's vCPU, RAM and disk:
   * one dotted line on a stacked card, one per line on a narrow column.
   */
  facts?: string[];
  /** Beside the title, or under the text on a narrow column. */
  note?: string | null;
  disabled: boolean;
}

interface RadioCardGroupProps<V extends string> {
  options: RadioCardOption<V>[];
  value: V | null;
  onChange: (value: V) => void;
  /** Shown above the cards, and the group's accessible name. */
  label: string;
  /**
   * Cards side by side once the group is wide enough for that many, stacked
   * below it. Read off the group's own width, not the window's: a dialog is
   * narrower than the screen it sits on.
   */
  columns?: 1 | 2 | 3;
}

// Literal class names, so Tailwind's scanner sees them.
const GRID_COLUMNS = { 1: '', 2: '@sm:grid-cols-2', 3: '@md:grid-cols-3' } as const;

export function RadioCardGroup<V extends string>({
  options,
  value,
  onChange,
  label,
  columns = 1,
}: RadioCardGroupProps<V>) {
  // One id names the native group and prefixes every element id below it.
  const name = useId();
  const labelId = `${name}-label`;
  // Three across leaves a card too narrow for a note beside its title, so
  // there the note drops under the text and the facts stack.
  const narrow = columns === 3;

  return (
    <div className="flex flex-col gap-1.5">
      <span id={labelId} className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
        {label}
      </span>
      <div className="@container">
        <div className={cn('grid gap-2', GRID_COLUMNS[columns])} role="radiogroup" aria-labelledby={labelId}>
          {options.map(({ value: id, title, description, facts, note, disabled }, index) => {
            const selected = value === id;
            const hasFacts = !!facts?.length;
            const hasDetail = hasFacts || !!description;
            const titleId = `${name}-${index}-title`;
            const noteId = `${name}-${index}-note`;
            const detailId = `${name}-${index}-detail`;
            // Named by its title alone, so the note and the detail are read as
            // its description rather than as part of the name.
            const describedBy = [note && noteId, hasDetail && detailId].filter(Boolean).join(' ') || undefined;
            return (
              <RadioChoice
                key={id}
                name={name}
                value={id}
                checked={selected}
                disabled={disabled}
                onSelect={onChange}
                size="md"
                aria-labelledby={titleId}
                aria-describedby={describedBy}
                markClassName="mt-0.5"
                className={cn(
                  'items-start gap-3 rounded-lg border p-3 transition-colors',
                  narrow && '@md:gap-2 @md:p-2.5',
                  !selected && !disabled && 'hover:bg-(--color-bg-subtle)',
                )}
                // Selection is carried by neutrals that hold on any fill, the
                // dialog's elevated one included: a stronger edge, a tint, and
                // the filled mark. Never the accent (DESIGN.md).
                style={{
                  borderColor: selected ? 'var(--color-border-strong)' : 'var(--color-border-muted)',
                  backgroundColor: selected ? 'var(--color-bg-hover)' : undefined,
                }}
              >
                <span className="flex min-w-0 flex-1 flex-wrap items-baseline justify-between gap-x-2">
                  <span id={titleId} className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {title}
                  </span>
                  {note && (
                    <span
                      id={noteId}
                      className={cn(
                        'text-xs whitespace-nowrap',
                        narrow && '@md:order-last @md:mt-1 @md:basis-full @md:whitespace-normal',
                      )}
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {note}
                    </span>
                  )}
                  {hasDetail && (
                    <span
                      id={detailId}
                      className={cn('mt-0.5 basis-full text-xs', hasFacts && 'tabular-nums')}
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {hasFacts
                        ? facts?.map((fact, i) => (
                            <React.Fragment key={i}>
                              {/* Heard on a column too, where the lines alone separate them. */}
                              {i > 0 && <span className={cn(narrow && '@md:sr-only')}> · </span>}
                              <span className={cn(narrow && '@md:block')}>{fact}</span>
                            </React.Fragment>
                          ))
                        : description}
                    </span>
                  )}
                </span>
              </RadioChoice>
            );
          })}
        </div>
      </div>
    </div>
  );
}
