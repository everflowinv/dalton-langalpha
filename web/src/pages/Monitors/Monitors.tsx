import React from 'react';
import { useParams } from 'react-router';
import { MonitorsList } from './MonitorsList';
import { MonitorViewer } from './MonitorViewer';
import './Monitors.css';

export default function Monitors(): React.ReactElement {
  const { slug } = useParams<{ slug?: string }>();
  if (slug) {
    return <MonitorViewer slug={slug} />;
  }
  return <MonitorsList />;
}
