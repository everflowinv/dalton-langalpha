/**
 * Everflow brand mark: three stacked bars with slanted ends, an "E" that reads
 * as flow. Inline and stroked in `currentColor` with the same props contract as
 * `LangAlphaMark`, so it takes the tint of the tile it sits in.
 */
export function EverflowMark({ className = '' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 56 56"
      fill="none"
      stroke="currentColor"
      strokeWidth={3}
      strokeLinejoin="round"
      aria-hidden
      className={className}
    >
      <g transform="translate(1.5 0)">
        <path d="M14 10.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z" />
        <path d="M14 24h19.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z" />
        <path d="M14 37.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z" />
      </g>
    </svg>
  );
}
