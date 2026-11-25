import { useLayoutEffect, useRef, type InputHTMLAttributes, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ChevronRight, Search, X } from 'lucide-react'; // DALTON-HOOK(mobile-web)
import { Loader } from '@/components/ui/loader';
import { focusWithoutRing, lastInputWasPointer } from '@/lib/inputModality'; // DALTON-HOOK(mobile-web)
import { cn } from '@/lib/utils';
import './PaletteField.css';

interface FieldProps {
  query: string;
  onText: (text: string) => void;
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
  placeholder: string;
  label: string;
  /** The scope searched within, named before the text (a workspace, then the kind that narrows it); none at the top level. */
  chips: readonly string[];
  /** Takes the last chip off, for a pointer: the keyboard has Backspace, Shift+Tab and Escape. */
  onLeave: () => void;
  /** The row whose actions are open, or the drilled-in view, named at the field's end. */
  trailing: string | null;
  busy: boolean;
  /** The field holds a value to edit (a rename): its text is selected as it arrives, so typing replaces it. */
  selected: boolean;
  /** The longest text the field takes, while it edits a value. */
  maxLength?: number;
  /** The combobox's wiring to its list. */
  combobox: Pick<InputHTMLAttributes<HTMLInputElement>, 'aria-describedby' | 'aria-controls' | 'aria-activedescendant'>;
  // DALTON-HOOK(mobile-web): optional mobile-only back & cancel buttons (unset on desktop)
  onBack?: () => void;
  onCancel?: () => void;
}

/**
 * The palette's one field. The row owns its field's focus: the keyboard ring
 * goes round the row, and the always-focused field draws no edge of its own.
 */
export function PaletteField({ query, onText, onKeyDown, placeholder, label, chips, onLeave, trailing, busy, selected, maxLength, combobox, onBack, onCancel }: FieldProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  // Once, as the value arrives: later edits leave the caret where it is.
  useLayoutEffect(() => {
    if (selected) inputRef.current?.select();
  }, [selected]);
  return (
    <div className="command-palette-field rings-within owns-its-edge">
      {/* DALTON-HOOK(mobile-web): show touch Back button in nested views on mobile */}
      {onBack ? (
        <button
          type="button"
          className="command-palette-mobile-back tap-target"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            onBack();
            // DALTON-HOOK(mobile-web): return focus to the search input before this button unmounts on keyboard activation
            if (lastInputWasPointer()) focusWithoutRing(inputRef.current);
            else inputRef.current?.focus();
          }}
          aria-label={t('common.back')}
          title={t('common.back')}
          data-testid="command-palette-back"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
        </button>
      ) : (
        <Search className="command-palette-field-icon" aria-hidden />
      )}
      {chips.map((chip, i) => {
        // Only the last comes off: the one before it is where that leaves.
        const last = i === chips.length - 1;
        return (
          // The field's label names the scope for a screen reader; this is for the eye.
          <span key={i} className={cn('command-palette-chip', !last && 'is-plain')} aria-hidden>
            <span className="command-palette-chip-label">{chip}</span>
            {last && (
              <button
                type="button"
                tabIndex={-1}
                title={t('palette.scope.leave')}
                className="command-palette-chip-leave tap-target"
                onMouseDown={(e) => e.preventDefault()}
                onClick={onLeave}
              >
                <X aria-hidden />
              </button>
            )}
          </span>
        );
      })}
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => onText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        maxLength={maxLength}
        aria-label={label}
        role="combobox"
        aria-expanded
        aria-autocomplete="list"
        {...combobox}
        autoComplete="off"
        spellCheck={false}
      />
      {busy && <Loader size={12} className="text-current" />}
      {trailing && (
        <span className="command-palette-scope">
          {trailing}
          <ChevronRight aria-hidden />
        </span>
      )}
      {/* DALTON-HOOK(mobile-web): show touch Cancel button on mobile */}
      {onCancel && (
        <button
          type="button"
          className="command-palette-mobile-cancel tap-target"
          onClick={onCancel}
          data-testid="command-palette-cancel"
        >
          {t('common.cancel')}
        </button>
      )}
    </div>
  );
}
