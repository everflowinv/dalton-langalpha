import React, { useEffect, useState } from 'react';
import { Navigate, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { BrandMark } from '@/components/brand/BrandMark';
import { BRAND_NAME } from '@/config/brand';
import { isDaltonLocalAuth } from '@/config/daltonAuth';
import { APP_ENTRY_PATH } from '@/config/hostMode';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/contexts/AuthContext';
import { fetchBootstrapStatus, LocalAuthError } from '@/lib/dalton/localAuth';
import EdgeGrain from '@/pages/Login/EdgeGrain';
import PasswordInput from '@/pages/Login/PasswordInput';
import PasswordStrength from '@/pages/Login/PasswordStrength';
import '@/pages/Login/AuthPage.css';
import '@/pages/Login/LoginPage.css';

const MIN_ADMIN_PASSWORD_LEN = 10;
const USERNAME_RE = /^[a-z0-9._-]{3,32}$/;

/**
 * First-administrator bootstrap step (`/setup/admin`) for standalone core local auth.
 *
 * Accessible only before initial bootstrap completes (`needsBootstrap === true`).
 * Once the first administrator is created, the backend permanently locks bootstrap (409)
 * and this page redirects to the app or sign-in screen.
 */
export default function AdminBootstrapStep() {
  const {
    isInitialized,
    isLoggedIn,
    needsBootstrap,
    bootstrapAdmin,
    markBootstrapCompleted,
  } = useAuth();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [bootstrapToken, setBootstrapToken] = useState('');
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checkedClosed, setCheckedClosed] = useState(false);

  // Re-verify live status on mount so opening `/setup/admin` after another tab finished
  // bootstrap immediately closes the page.
  useEffect(() => {
    if (!isDaltonLocalAuth || !needsBootstrap) return;
    let active = true;
    fetchBootstrapStatus()
      .then((res) => {
        if (!active) return;
        if (!res.needs_bootstrap) {
          markBootstrapCompleted?.();
          setCheckedClosed(true);
        }
      })
      .catch(() => {
        /* keep current form state; submission still enforces server-side lock */
      });
    return () => {
      active = false;
    };
  }, [needsBootstrap, markBootstrapCompleted]);

  if (!isDaltonLocalAuth) {
    return <Navigate to={APP_ENTRY_PATH} replace />;
  }

  if (!isInitialized) {
    return (
      <div className="login-page owns-its-edge">
        <EdgeGrain centered />
        <div className="login-page__card">
          <p className="login-page__status-text">{t('common.loading')}</p>
        </div>
      </div>
    );
  }

  if (isLoggedIn) {
    return <Navigate to="/dashboard" replace />;
  }

  if (!needsBootstrap || checkedClosed) {
    return <Navigate to={APP_ENTRY_PATH} replace />;
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);

    const trimmedToken = bootstrapToken.trim();
    const trimmedEmail = email.trim();
    const trimmedUsername = username.trim().toLowerCase();

    if (!trimmedToken) {
      setError(t('auth.bootstrap.tokenRequired'));
      return;
    }
    if (!trimmedEmail || !trimmedEmail.includes('@')) {
      setError(t('auth.bootstrap.emailRequired'));
      return;
    }
    if (trimmedUsername && !USERNAME_RE.test(trimmedUsername)) {
      setError(t('auth.invite.usernameInvalid'));
      return;
    }
    if (password.length < MIN_ADMIN_PASSWORD_LEN) {
      setError(t('auth.invite.passwordTooShort'));
      return;
    }
    if (password !== confirmPassword) {
      setError(t('auth.passwordsDontMatch'));
      return;
    }
    if (!bootstrapAdmin) {
      return;
    }

    setIsSubmitting(true);
    try {
      await bootstrapAdmin({
        bootstrap_token: trimmedToken,
        email: trimmedEmail,
        password,
        username: trimmedUsername || undefined,
        name: name.trim() || undefined,
      });
      navigate('/dashboard', { replace: true });
    } catch (err: unknown) {
      if (err instanceof LocalAuthError) {
        if (err.status === 409) {
          markBootstrapCompleted?.();
          setCheckedClosed(true);
          return;
        }
        setError(err.message);
      } else if (err instanceof Error && err.message) {
        setError(err.message);
      } else {
        setError(t('auth.bootstrap.failed'));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="login-page owns-its-edge">
      <EdgeGrain centered />
      <div className="login-page__card">
        <div className="login-page__card-header">
          <BrandMark className="login-page__logo-icon" />
          <h1 className="login-page__title">{BRAND_NAME}</h1>
        </div>

        <form onSubmit={handleSubmit} className="login-page__form" data-testid="admin-bootstrap-form">
          <div>
            <h2 className="login-page__view-title">{t('auth.bootstrap.title')}</h2>
            <p className="login-page__view-subtitle">{t('auth.bootstrap.subtitle')}</p>
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-token">
              {t('auth.bootstrap.tokenLabel')}
            </label>
            <PasswordInput
              id="bootstrap-token"
              value={bootstrapToken}
              onChange={(e) => setBootstrapToken(e.target.value)}
              placeholder={t('auth.bootstrap.tokenPlaceholder')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="off"
              required
            />
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-email">
              {t('common.email')}
            </label>
            <Input
              id="bootstrap-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={t('auth.enterEmail')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="email"
              required
            />
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-username">
              {t('auth.bootstrap.usernameLabel')}
            </label>
            <Input
              id="bootstrap-username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t('auth.invite.usernamePlaceholder')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="username"
            />
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-name">
              {t('auth.bootstrap.nameLabel')}
            </label>
            <Input
              id="bootstrap-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('auth.enterName')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="name"
            />
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-password">
              {t('auth.invite.password')}
            </label>
            <PasswordInput
              id="bootstrap-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('auth.invite.passwordPlaceholder')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="new-password"
              minLength={MIN_ADMIN_PASSWORD_LEN}
              required
            />
            <PasswordStrength password={password} />
          </div>

          <div className="login-page__field">
            <label className="login-page__label" htmlFor="bootstrap-confirm-password">
              {t('auth.invite.confirmPassword')}
            </label>
            <PasswordInput
              id="bootstrap-confirm-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder={t('auth.invite.confirmPasswordPlaceholder')}
              className="login-page__input"
              disabled={isSubmitting}
              autoComplete="new-password"
              minLength={MIN_ADMIN_PASSWORD_LEN}
              required
            />
          </div>

          {error && (
            <div className="login-page__error" role="alert">
              {error}
            </div>
          )}

          <button type="submit" className="login-page__submit" disabled={isSubmitting}>
            {isSubmitting ? t('auth.bootstrap.submitting') : t('auth.bootstrap.submit')}
          </button>
        </form>
      </div>
    </div>
  );
}
