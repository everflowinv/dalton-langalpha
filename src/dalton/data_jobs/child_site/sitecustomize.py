"""Chinese glyph fallback for task scripts written against macOS fonts.

The OpenClaw task scripts name macOS fonts (Hiragino, PingFang, STHeiti, or
``/System/Library/Fonts`` paths). None of these exist in the Linux task
container, so matplotlib falls back to DejaVu Sans and every Chinese character
renders as a box. Matplotlib (3.6+) falls back glyph by glyph along the font
list it resolves, so appending the container's CJK fonts to every resolved list
fixes all scripts at once without editing them. The runner wrapper puts this
directory on the child's ``PYTHONPATH``; nothing here may raise.
"""

import importlib.abc
import importlib.util
import sys

_TARGET = "matplotlib.font_manager"
_CJK_FALLBACK = ("WenQuanYi Zen Hei", "Noto Sans CJK JP")


def _patch(fm):
    original = fm.FontManager._find_fonts_by_props
    if getattr(original, "_dalton_cjk_fallback", False):
        return

    def _find_fonts_by_props(self, prop, *args, **kwargs):
        paths = list(original(self, prop, *args, **kwargs))
        for family in _CJK_FALLBACK:
            try:
                path = self.findfont(
                    fm.FontProperties(
                        family=family, weight=prop.get_weight(), style=prop.get_style()
                    ),
                    fallback_to_default=False,
                )
            except ValueError:  # family not installed in this container
                continue
            if path not in paths:
                paths.append(path)
        return paths

    _find_fonts_by_props._dalton_cjk_fallback = True
    fm.FontManager._find_fonts_by_props = _find_fonts_by_props


class _FontManagerHook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != _TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def _exec_module(module):
            exec_module(module)
            try:
                _patch(module)
            except Exception:  # noqa: BLE001, S110 - a font helper must never break the task
                pass

        spec.loader.exec_module = _exec_module
        return spec


try:
    if _TARGET in sys.modules:
        _patch(sys.modules[_TARGET])
    else:
        sys.meta_path.insert(0, _FontManagerHook())
except Exception:  # noqa: BLE001, S110 - a font helper must never break the task
    pass
