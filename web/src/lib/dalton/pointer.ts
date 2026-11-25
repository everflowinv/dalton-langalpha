/**
 * Detects touch-first ("coarse pointer") devices so multiline inputs can treat
 * plain Enter as a newline instead of accidentally submitting on Android Chrome
 * or WeChat XWeb where `interactive-widget=resizes-content` keeps visualViewport
 * overlap at 0.
 */
export function isCoarsePointer(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false;
  }
  return window.matchMedia('(pointer: coarse)').matches;
}
