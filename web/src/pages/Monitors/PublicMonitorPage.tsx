import React from 'react';
import { useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { MonitorViewer } from './MonitorViewer';

export default function PublicMonitorPage(): React.ReactElement {
  const { slug } = useParams<{ slug?: string }>();
  const { t } = useTranslation();

  if (!slug) {
    return (
      <div className="monitors-page">
        <div className="monitors-scroll">
          <div className="monitors-container">
            <h1 className="text-base font-semibold" style={{ color: 'var(--color-text-primary)' }}>
              {t('monitors.notFoundTitle')}
            </h1>
            <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('monitors.notFoundDesc')}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return <MonitorViewer slug={slug} publicMode />;
}
