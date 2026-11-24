import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { isDaltonLocalAuth } from '@/config/daltonAuth';
import { useAuth } from '@/contexts/AuthContext';
import { LocalAuthError } from '@/lib/dalton/localAuth';
import PasswordInput from '@/pages/Login/PasswordInput';
import { SettingsGroup } from '../../components/SettingsLayout';

const MIN_LOCAL_PASSWORD_LEN = 10;

/**
 * Signed-in password change form for standalone core local auth (`VITE_DALTON_AUTH=local`).
 */
export function LocalPasswordChangeSection() {
  const { changePassword } = useAuth();
  const { t } = useTranslation();

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  if (!isDaltonLocalAuth || !changePassword) {
    return null;
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    setSaved(false);

    if (newPassword.length < MIN_LOCAL_PASSWORD_LEN) {
      setError(t('auth.invite.passwordTooShort'));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t('auth.passwordsDontMatch'));
      return;
    }

    setIsSubmitting(true);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setSaved(true);
    } catch (err: unknown) {
      if (err instanceof LocalAuthError) {
        setError(err.message);
      } else if (err instanceof Error && err.message) {
        setError(err.message);
      } else {
        setError(t('auth.changePassword.failed'));
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <SettingsGroup title={t('auth.changePassword.title')}>
      <form
        onSubmit={handleSubmit}
        className="space-y-3 px-4 py-3"
        data-testid="local-change-password-form"
      >
        <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('auth.changePassword.subtitle')}
        </p>
        <div className="grid gap-2.5 max-w-md">
          <div className="space-y-1">
            <label
              className="block text-xs font-medium"
              htmlFor="local-current-password"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('auth.changePassword.currentPassword')}
            </label>
            <PasswordInput
              id="local-current-password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder={t('auth.enterPassword')}
              className="h-9"
              disabled={isSubmitting}
              autoComplete="current-password"
              required
            />
          </div>
          <div className="space-y-1">
            <label
              className="block text-xs font-medium"
              htmlFor="local-new-password"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('auth.newPassword')}
            </label>
            <PasswordInput
              id="local-new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t('auth.invite.passwordPlaceholder')}
              className="h-9"
              disabled={isSubmitting}
              autoComplete="new-password"
              minLength={MIN_LOCAL_PASSWORD_LEN}
              required
            />
          </div>
          <div className="space-y-1">
            <label
              className="block text-xs font-medium"
              htmlFor="local-confirm-password"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('auth.confirmNewPassword')}
            </label>
            <PasswordInput
              id="local-confirm-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder={t('auth.invite.confirmPasswordPlaceholder')}
              className="h-9"
              disabled={isSubmitting}
              autoComplete="new-password"
              minLength={MIN_LOCAL_PASSWORD_LEN}
              required
            />
          </div>
        </div>
        {error && (
          <div className="text-xs" style={{ color: 'var(--color-loss)' }} role="alert">
            {error}
          </div>
        )}
        {saved && (
          <div className="text-xs" style={{ color: 'var(--color-success)' }} role="status">
            {t('auth.changePassword.saved')}
          </div>
        )}
        <Button type="submit" variant="outline" size="sm" className="h-8" disabled={isSubmitting}>
          {isSubmitting ? t('auth.updatingPassword') : t('auth.updatePassword')}
        </Button>
      </form>
    </SettingsGroup>
  );
}
