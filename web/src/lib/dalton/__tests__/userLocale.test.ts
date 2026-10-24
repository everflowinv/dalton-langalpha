import { describe, it, expect } from 'vitest';
import { DALTON_DEFAULT_USER_LOCALE, daltonUserLocale } from '../userLocale';

describe('daltonUserLocale', () => {
  it('defaults to zh-CN', () => {
    expect(DALTON_DEFAULT_USER_LOCALE).toBe('zh-CN');
  });

  it('keeps a supported saved locale', () => {
    expect(daltonUserLocale('en-US')).toBe('en-US');
    expect(daltonUserLocale('zh-CN')).toBe('zh-CN');
  });

  it('falls back to zh-CN for no / unsupported value', () => {
    expect(daltonUserLocale(null)).toBe('zh-CN');
    expect(daltonUserLocale(undefined)).toBe('zh-CN');
    expect(daltonUserLocale('')).toBe('zh-CN');
    expect(daltonUserLocale('fr-FR')).toBe('zh-CN');
  });
});
