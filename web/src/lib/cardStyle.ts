/**
 * How the chat's data cards and their detail panels are drawn: Balanced, the
 * default, Quiet, or Terminal. Terminal is the cards' own stylesheets; the
 * other two take marks away from them (charts/termCardStyles.css). Per device,
 * like the theme and the font size, since it is a question of how this screen
 * reads.
 *
 * Stamped on <html> as `data-card-style`, so a change restyles every card
 * already drawn and nothing re-renders. No card is on the first paint (cards
 * come with the thread), so main.tsx stamps it before the app renders rather
 * than index.html's pre-paint script.
 */

import { isDaltonBuild } from '../config/brand'; // DALTON-HOOK(brand-name): default Dalton builds to original terminal card style
import { safeLocalStorage } from './utils';

export const CARD_STYLES = ['quiet', 'balanced', 'terminal'] as const;
export type CardStyle = (typeof CARD_STYLES)[number];

const KEY = 'cardStyle';

// Read before the app boots, so storage a browser refuses (blocked site data)
// falls back to the default rather than stopping the boot.
export function getCardStyle(): CardStyle {
  const stored = safeLocalStorage.getItem(KEY);
  // DALTON-HOOK(brand-name): Dalton builds default to 'terminal' (the pre-#617 card stylesheet).
  const defaultStyle: CardStyle = isDaltonBuild ? 'terminal' : 'balanced';
  return CARD_STYLES.find((s) => s === stored) ?? defaultStyle;
}

export function applyCardStyle(style: CardStyle): void {
  if (style === 'terminal') document.documentElement.removeAttribute('data-card-style');
  else document.documentElement.setAttribute('data-card-style', style);
}

export function setCardStyle(style: CardStyle): void {
  applyCardStyle(style);
  safeLocalStorage.setItem(KEY, style);
}
