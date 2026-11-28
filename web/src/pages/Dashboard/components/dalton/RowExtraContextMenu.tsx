/**
 * Dalton helper component that wraps a Dashboard Watchlist/Portfolio row with
 * extra right-click context-menu items when the row lacks a native delete/edit id.
 * Keeps upstream `_holdingsPrimitives.tsx` to a 1-line thin hook.
 */

import type { ReactNode } from 'react';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';

export function RowExtraContextMenu({
  row,
  menuItems,
}: {
  row: ReactNode;
  menuItems?: ReactNode;
}) {
  if (!menuItems) return row;
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{row}</ContextMenuTrigger>
      <ContextMenuContent>{menuItems}</ContextMenuContent>
    </ContextMenu>
  );
}
