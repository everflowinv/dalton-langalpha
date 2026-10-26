import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { BrandMark } from '@/components/brand/BrandMark';
import { BRAND_NAME } from '@/config/brand';
import { resolveDaltonLoginEmail } from '@/config/daltonAuth';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/contexts/AuthContext';
import { queryKeys } from '@/lib/queryKeys';
import PasswordInput from '@/pages/Login/PasswordInput';
import {
  checkDaltonInviteToken,
  redeemDaltonInviteToken,
} from '@/pages/Settings/utils/daltonApi';
import '@/pages/Login/LoginPage.css';

const USERNAME_RE = /^[a-z0-9._-]{3,32}$/;
const MIN_INVITE_PASSWORD_LEN = 10;

type InviteViewState = 'checking' | 'valid' | 'invalid';

function extractHashToken(routerHash: string): string {
  const raw = routerHash || (typeof window !== 'undefined' ? window.location.hash : '');
  return raw.replace(/^#+/, '').trim();
}

function extractErrorDetail(err: unknown, fallback: string): string {
  if (err && typeof err === 'object') {
    const respData = (err as { response?: { data?: { detail?: unknown; message?: unknown } } })
      .response?.data;
    if (typeof respData?.detail === 'string' && respData.detail.trim()) {
      return respData.detail;
    }
    if (typeof respData?.message === 'string' && respData.message.trim()) {
      return respData.message;
    }
    const msg = (err as { message?: unknown }).message;
    if (typeof msg === 'string' && msg.trim()) {
      return msg;
    }
  }
  return fallback;
}

export default function InviteRedeemPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { loginWithEmail } = useAuth();
  const { t } = useTranslation();

  const [token] = useState<string>(() => extractHashToken(location.hash));
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window !== 'undefined' && window.history?.replaceState) {
      const cleanUrl = `${window.location.pathname}${window.location.search}`;
      window.history.replaceState(null, '', cleanUrl);
    }
  }, []);

  const checkQuery = useQuery({
    queryKey: queryKeys.dalton.inviteCheck(token),
    queryFn: () => checkDaltonInviteToken(token),
    enabled: Boolean(token),
    retry: false,
  });

  const state: InviteViewState = !token
    ? 'invalid'
    : checkQuery.isPending
      ? 'checking'
      : checkQuery.isError || !checkQuery.data?.valid
        ? 'invalid'
        : 'valid';

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);

    const trimmedUsername = username.trim();
    if (!USERNAME_RE.test(trimmedUsername)) {
      setError(t('auth.invite.usernameInvalid'));
      return;
    }
    if (password.length < MIN_INVITE_PASSWORD_LEN) {
      setError(t('auth.invite.passwordTooShort'));
      return;
    }
    if (password !== confirmPassword) {
      setError(t('auth.passwordsDontMatch'));
      return;
    }

    setIsSubmitting(true);
    try {
      const redeemed = await redeemDaltonInviteToken({
        token,
        username: trimmedUsername,
        password,
      });
      const loginEmail = redeemed?.email || resolveDaltonLoginEmail(trimmedUsername);
      const loginResult = await loginWithEmail(loginEmail, password);
      const authError = (loginResult as { error?: unknown } | undefined)?.error;
      if (authError) {
        throw authError;
      }
      navigate('/dashboard', { replace: true });
    } catch (err: unknown) {
      setError(extractErrorDetail(err, t('auth.invite.redeemFailed')));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-page__card">
        <div className="login-page__card-header">
          <BrandMark className="login-page__logo-icon" />
          <h1 className="login-page__title">{BRAND_NAME}</h1>
        </div>

        {state === 'checking' && (
          <p className="login-page__subtitle">{t('auth.invite.checking')}</p>
        )}

        {state === 'invalid' && (
          <div className="login-page__form">
            <div className="login-page__error" role="alert">
              {t('auth.invite.invalidOrExpired')}
            </div>
            <button
              type="button"
              className="login-page__back"
              onClick={() => navigate('/login', { replace: true })}
            >
              {t('auth.backToLogin')}
            </button>
          </div>
        )}

        {state === 'valid' && (
          <form onSubmit={handleSubmit} className="login-page__form">
            <div>
              <h2 className="login-page__view-title">{t('auth.invite.title')}</h2>
              <p className="login-page__view-subtitle">{t('auth.invite.subtitle')}</p>
            </div>

            <div className="login-page__field">
              <label className="login-page__label" htmlFor="invite-username">
                {t('auth.invite.username')}
              </label>
              <Input
                id="invite-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('auth.invite.usernamePlaceholder')}
                className="login-page__input"
                disabled={isSubmitting}
                autoComplete="username"
                required
              />
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('auth.invite.usernameHint')}
              </span>
            </div>

            <div className="login-page__field">
              <label className="login-page__label" htmlFor="invite-password">
                {t('auth.invite.password')}
              </label>
              <PasswordInput
                id="invite-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('auth.invite.passwordPlaceholder')}
                className="login-page__input"
                disabled={isSubmitting}
                autoComplete="new-password"
                required
              />
            </div>

            <div className="login-page__field">
              <label className="login-page__label" htmlFor="invite-confirm-password">
                {t('auth.invite.confirmPassword')}
              </label>
              <PasswordInput
                id="invite-confirm-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder={t('auth.invite.confirmPasswordPlaceholder')}
                className="login-page__input"
                disabled={isSubmitting}
                autoComplete="new-password"
                required
              />
            </div>

            {error && (
              <div className="login-page__error" role="alert">
                {error}
              </div>
            )}

            <button type="submit" className="login-page__submit" disabled={isSubmitting}>
              {isSubmitting ? t('auth.invite.submitting') : t('auth.invite.submit')}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
