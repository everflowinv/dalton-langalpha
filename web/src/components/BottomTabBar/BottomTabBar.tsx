import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Search } from 'lucide-react'; // DALTON-HOOK(global-search)
import { useCommandPalette } from '@/components/CommandPalette/CommandPaletteProvider'; // DALTON-HOOK(global-search)
import { SETTINGS_ITEM } from '../nav/navItems';
import { useNavActive } from '../nav/useNavActive';
import { useNavItems } from '../nav/useNavItems';
import './BottomTabBar.css';

export default function BottomTabBar() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const isActive = useNavActive();
  const palette = useCommandPalette(); // DALTON-HOOK(global-search)
  // Settings rides at the end of whichever primary items this user can see.
  const navItems = useNavItems();
  const menuItems = [...navItems, SETTINGS_ITEM];
  const handleItemClick = (path: string) => {
    if (location.pathname === path) return;
    navigate(path);
  };

  return (
    <div className="bottom-tab-bar">
      <div className="bottom-tab-bar-pill">
        {menuItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item);

          return (
            <button
              key={item.key}
              className={`bottom-tab-item ${active ? 'active' : ''}`}
              onClick={() => handleItemClick(item.key)}
              aria-label={t(item.labelKey)}
              aria-current={active ? 'page' : undefined}
            >
              <Icon className="bottom-tab-item-icon" />
            </button>
          );
        })}
        {/* DALTON-HOOK(global-search): mobile search button opens Command Palette */}
        <button
          type="button"
          className="bottom-tab-item"
          onClick={() => palette?.open()}
          onPointerDown={palette?.preload}
          aria-label={t('palette.search')}
          title={t('palette.search')}
        >
          <Search className="bottom-tab-item-icon" />
        </button>
      </div>
    </div>
  );
}
