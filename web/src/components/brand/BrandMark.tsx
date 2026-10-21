import { BRAND_MARK } from '@/config/brand';
import { EverflowMark } from './EverflowMark';
import { LangAlphaMark } from './LangAlphaMark';

/**
 * Selects `EverflowMark` or `LangAlphaMark` based on `BRAND_MARK` (`VITE_BRAND_MARK`).
 */
export function BrandMark({ className = '' }: { className?: string }) {
  return BRAND_MARK === 'everflow' ? (
    <EverflowMark className={className} />
  ) : (
    <LangAlphaMark className={className} />
  );
}
