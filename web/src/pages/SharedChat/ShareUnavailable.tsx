import React from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useAuth } from '../../contexts/AuthContext';
import { useTheme } from '../../contexts/ThemeContext';
import { BrandMark } from '@/components/brand/BrandMark'; // DALTON-HOOK(brand-name)
import { BRAND_MARK } from '@/config/brand'; // DALTON-HOOK(brand-name)
import { APP_ENTRY_PATH, isPlatformMode } from '@/config/hostMode';
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(dalton-auth): offer sign-in on private share links when Dalton auth is enabled
// Named for the ink, not the theme: logo.svg is drawn in white for a dark page,
// logo-dark.svg in near-black for a light one.
import logoOnDark from '../../assets/img/logo.svg';
import logoOnLight from '../../assets/img/logo-dark.svg';

interface ShareUnavailableProps {
  /** `unavailable` is the uniform 404; `membersOnly` a signed-out visitor of a
   *  link that opens to members of an organization; `paused` a legacy link whose
   *  workspace auto-publish is paused; `failed` is any other error, with a retry. */
  // DALTON-HOOK(colleague-access): support paused 503 copy distinct from generic load failure
  variant?: 'unavailable' | 'membersOnly' | 'paused' | 'failed';
  onRetry?: () => void;
}

/**
 * The branded end of a link that did not resolve.
 *
 * A 404 says nothing about why, on purpose: an unknown code, a link whose
 * sharing was stopped and a private link seen by someone else all look the
 * same. The one thing the page can offer is a sign-in, since the owner of a
 * private link is only recognised once signed in, and it returns here after.
 */
export default function ShareUnavailable({ variant = 'unavailable', onRetry }: ShareUnavailableProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { pathname, search, hash } = useLocation();
  const { theme } = useTheme();
  const { isLoggedIn, isInitialized } = useAuth();
  const logo = theme === 'light' ? logoOnLight : logoOnDark;
  // DALTON-HOOK(dalton-auth): offer sign-in when Dalton auth is enabled
  const offerSignIn = variant !== 'failed' && variant !== 'paused' && (isPlatformMode || isDaltonAuth) && isInitialized && !isLoggedIn;

  // The whole link comes back, so an app opened at a page reopens there.
  const signIn = () => {
    const params = new URLSearchParams({ redirect: pathname + search + hash });
    navigate(`${APP_ENTRY_PATH}?${params.toString()}`);
  };

  return (
    <div
      className="flex flex-col items-center justify-center min-h-screen gap-4 px-4 text-center"
      style={{ backgroundColor: 'var(--color-bg-page)' }}
    >
      {BRAND_MARK === 'everflow' ? <BrandMark className="h-8 w-8 opacity-60" /> : <img src={logo} alt="LangAlpha" className="h-8 opacity-60" />} {/* DALTON-HOOK(brand-name) */}
      <h1 className="text-lg font-semibold title-font" style={{ color: 'var(--color-text-primary)' }}>
        {variant === 'failed' ? t('shareLink.loadFailedTitle')
          : variant === 'paused' ? t('shareLink.pausedTitle')
            : variant === 'membersOnly' ? t('shareLink.membersOnlyTitle') : t('shareLink.unavailableTitle')}
      </h1>
      <p className="text-sm max-w-sm" style={{ color: 'var(--color-text-secondary)' }}>
        {variant === 'failed'
          ? t('shareLink.loadFailedBody')
          : variant === 'paused' ? t('shareLink.pausedBody')
            : variant === 'membersOnly' ? t('shareLink.membersOnlyBody')
              : offerSignIn ? t('shareLink.unavailableSignInBody') : t('shareLink.unavailableBody')}
      </p>
      <div className="flex items-center gap-3 pt-1">
        {(variant === 'failed' || variant === 'paused') && onRetry && (
          <Button type="button" size="sm" onClick={onRetry}>{t('shareLink.retry')}</Button>
        )}
        {offerSignIn && (
          <Button type="button" size="sm" onClick={signIn}>{t('shareLink.signIn')}</Button>
        )}
        <Link to="/" className="text-sm underline" style={{ color: 'var(--color-text-secondary)' }}>
          {t('shareLink.goToLangAlpha')}
        </Link>
      </div>
    </div>
  );
}
