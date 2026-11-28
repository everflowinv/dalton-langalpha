import { useRef } from 'react'; // DALTON-HOOK(mobile-web)
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { MoreHorizontal, Search } from 'lucide-react'; // DALTON-HOOK(global-search) DALTON-HOOK(mobile-web)
import { useCommandPalette } from '@/components/CommandPalette/CommandPaletteProvider'; // DALTON-HOOK(global-search)
// DALTON-HOOK(mobile-web): use Radix DropdownMenu so "More" gets arrow-key roving focus, Escape, and aria-haspopup natively
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { NavItem } from '../nav/navItems';
import { useNavActive } from '../nav/useNavActive';
import { useNavItems } from '../nav/useNavItems';
import { useMobileBottomNavItems } from '@/lib/dalton/mobileBottomNav'; // DALTON-HOOK(mobile-web)
import './BottomTabBar.css';

export default function BottomTabBar() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const isActive = useNavActive();
  const palette = useCommandPalette(); // DALTON-HOOK(global-search)
  // DALTON-HOOK(mobile-web): anchor CommandPalette return-focus to the persistent More trigger when Search is launched from the menu
  const moreTriggerRef = useRef<HTMLButtonElement>(null);
  const openingPaletteRef = useRef(false);
  // Settings rides at the end of whichever primary items this user can see.
  const navItems = useNavItems();
  const { primaryItems, overflowItems } = useMobileBottomNavItems(navItems); // DALTON-HOOK(mobile-web)
  const hasMoreMenu = overflowItems.length > 0 || Boolean(palette);
  const moreActive = overflowItems.some((item) => isActive(item));

  const handleItemClick = (item: NavItem) => {
    if (location.pathname === item.key) return;
    navigate(item.key, { state: item.state });
  };

  return (
    <div className="bottom-tab-bar">
      <div className="bottom-tab-bar-pill">
        {primaryItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item);

          return (
            <button
              key={item.key}
              className={`bottom-tab-item ${active ? 'active' : ''}`}
              onClick={() => handleItemClick(item)}
              aria-label={t(item.labelKey)}
              aria-current={active ? 'page' : undefined}
            >
              <Icon className="bottom-tab-item-icon" />
            </button>
          );
        })}
        {/* DALTON-HOOK(mobile-web): overflow secondary nav items and global search into a Radix DropdownMenu */}
        {hasMoreMenu && (
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <button
                ref={moreTriggerRef}
                type="button"
                className={`bottom-tab-item ${moreActive ? 'active' : ''}`}
                aria-label={t('common.more', 'More')}
                aria-current={moreActive ? 'page' : undefined}
                data-testid="bottom-tab-more"
              >
                <MoreHorizontal className="bottom-tab-item-icon" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              side="top"
              align="end"
              sideOffset={12}
              className="w-48 p-1.5"
              data-testid="bottom-tab-more-menu"
              onCloseAutoFocus={(e) => {
                // DALTON-HOOK(mobile-web): let CommandPalette claim focus when launched from Search; palette restores to moreTriggerRef on close
                if (openingPaletteRef.current) {
                  openingPaletteRef.current = false;
                  e.preventDefault();
                }
              }}
            >
              {overflowItems.map((item) => {
                const Icon = item.icon;
                const active = isActive(item);
                return (
                  <DropdownMenuItem
                    key={item.key}
                    className="min-h-11 gap-2.5 px-2.5 py-2 text-sm"
                    style={{
                      color: active ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                      fontWeight: active ? 600 : 400,
                    }}
                    onSelect={() => handleItemClick(item)}
                    aria-current={active ? 'page' : undefined}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="truncate">{t(item.labelKey)}</span>
                  </DropdownMenuItem>
                );
              })}
              {/* DALTON-HOOK(global-search): mobile search action inside More menu opens Command Palette */}
              {palette && (
                <DropdownMenuItem
                  className="min-h-11 gap-2.5 px-2.5 py-2 text-sm"
                  style={{ color: 'var(--color-text-secondary)' }}
                  onSelect={() => {
                    openingPaletteRef.current = true;
                    palette.open(undefined, { returnFocusTo: moreTriggerRef.current });
                  }}
                  onPointerDown={palette.preload}
                  aria-label={t('palette.search')}
                >
                  <Search className="h-4 w-4 shrink-0" />
                  <span className="truncate">{t('palette.search')}</span>
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  );
}
