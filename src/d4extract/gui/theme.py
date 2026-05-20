"""Dark theme colors and QSS stylesheet for D4.Export."""

from __future__ import annotations


class Colors:
    BG = "#1e1e2e"
    SURFACE = "#2a2a3e"
    TEXT = "#cdd6f4"
    SUBTEXT = "#a6adc8"
    OVERLAY = "#6c7086"
    ACCENT = "#89b4fa"
    ERROR = "#f38ba8"
    BORDER = "#45475a"
    LIST_BG = "#181825"
    HOVER = "#262637"
    SELECTED = "#313244"
    # Catppuccin Yellow — the favorite-star fill and the FAVS pill's
    # checked-state background. Picked as the visual partner of the
    # blue ACCENT so a starred row reads as "tagged" rather than
    # "selected".
    FAVORITE = "#f9e2af"
    FAVORITE_HOVER = "#fbebc4"


# Monospace stack for the model list / paths; system font everywhere else.
MONO_FONT_FAMILY = '"Cascadia Code", "Consolas", "Courier New", monospace'


DARK_THEME_QSS = f"""
QMainWindow, QWidget {{
    background-color: {Colors.BG};
    color: {Colors.TEXT};
}}

QFrame#card {{
    background-color: {Colors.SURFACE};
    border: 1px solid {Colors.BORDER};
    border-radius: 12px;
}}

QFrame#card[clickable="true"]:hover {{
    border: 1px solid {Colors.ACCENT};
    background-color: {Colors.HOVER};
}}

/* First-run TACT-keys banner. Sits above the model browser splitter
   when no key file has been loaded and the user hasn't dismissed it.
   The blue surface matches the rest of the accented chrome (selected
   tabs, primary buttons, focused inputs); body text is white per the
   established banner convention. The two buttons intentionally use
   opposite contrast strategies so both read cleanly against the blue:
   the white-filled "Load now" mirrors the inverse of QPushButton#primary
   (dark text on a light surface), while the transparent "Dismiss"
   stays white-on-blue with a solid border and bold weight to keep its
   short label legible at low colour contrast. */
QFrame#tactBanner {{
    background-color: {Colors.ACCENT};
    border: none;
    border-radius: 6px;
}}

QFrame#tactBanner QLabel {{
    background: transparent;
    color: #ffffff;
    border: none;
    padding: 0;
}}

QPushButton#tactBannerPrimary {{
    background-color: #ffffff;
    color: {Colors.BG};
    border: none;
    border-radius: 4px;
    padding: 6px 14px;
    font-weight: 600;
}}

QPushButton#tactBannerPrimary:hover {{
    background-color: #e8efff;
}}

QPushButton#tactBannerPrimary:pressed {{
    background-color: #d0dcf6;
}}

QPushButton#tactBannerGhost {{
    background-color: transparent;
    color: #ffffff;
    border: 1px solid #ffffff;
    border-radius: 4px;
    padding: 6px 14px;
    font-weight: 600;
}}

QPushButton#tactBannerGhost:hover {{
    background-color: rgba(255, 255, 255, 40);
}}

QPushButton#tactBannerGhost:pressed {{
    background-color: rgba(255, 255, 255, 70);
}}

QLabel {{
    background: transparent;
    color: {Colors.TEXT};
}}

QLabel#cardTitle {{
    color: {Colors.TEXT};
    font-size: 22px;
    font-weight: 600;
}}

QLabel#cardSubtitle {{
    color: {Colors.SUBTEXT};
    font-size: 14px;
}}

QLabel#cardMuted {{
    color: {Colors.OVERLAY};
    font-size: 12px;
}}

QLabel#cardError {{
    color: {Colors.ERROR};
    font-size: 13px;
}}

QLabel#cardDetected {{
    color: {Colors.ACCENT};
    font-size: 13px;
}}

QPushButton {{
    background-color: {Colors.SELECTED};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 8px 16px;
}}

QPushButton:hover {{
    background-color: {Colors.HOVER};
    border-color: {Colors.ACCENT};
}}

QPushButton:pressed {{
    background-color: {Colors.BG};
}}

QPushButton#primary {{
    background-color: {Colors.ACCENT};
    color: {Colors.BG};
    border: none;
    font-weight: 600;
}}

QPushButton#primary:hover {{
    background-color: #a4c4ff;
}}

QPushButton#primary:pressed {{
    background-color: #6f9be0;
}}

QPushButton#pill {{
    background-color: {Colors.SELECTED};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    border-radius: 11px;
    padding: 3px 12px;
    font-size: 11px;
    font-weight: 600;
    min-height: 16px;
}}

QPushButton#pill:hover {{
    background-color: {Colors.HOVER};
    border-color: {Colors.ACCENT};
}}

QPushButton#pill:checked {{
    background-color: {Colors.ACCENT};
    color: {Colors.BG};
    border-color: {Colors.ACCENT};
}}

QPushButton#pill:checked:hover {{
    background-color: #a4c4ff;
    border-color: #a4c4ff;
}}

/* Favorites pill — same geometry as #pill, with a yellow checked
   state so it reads as a different filter axis from the ALL/MON/PLR
   blue pills. */
QPushButton#favPill {{
    background-color: {Colors.SELECTED};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    border-radius: 11px;
    padding: 3px 12px;
    font-size: 11px;
    font-weight: 600;
    min-height: 16px;
}}

QPushButton#favPill:hover {{
    background-color: {Colors.HOVER};
    border-color: {Colors.ACCENT};
}}

QPushButton#favPill:checked {{
    background-color: {Colors.FAVORITE};
    color: {Colors.BG};
    border-color: {Colors.FAVORITE};
}}

QPushButton#favPill:checked:hover {{
    background-color: {Colors.FAVORITE_HOVER};
    border-color: {Colors.FAVORITE_HOVER};
}}

QToolButton#exportPrimary {{
    background-color: {Colors.ACCENT};
    color: {Colors.BG};
    border: none;
    border-top-left-radius: 6px;
    border-bottom-left-radius: 6px;
    padding: 6px 14px;
    font-weight: 700;
}}

QToolButton#exportPrimary:hover {{
    background-color: #a4c4ff;
}}

QToolButton#exportPrimary:pressed {{
    background-color: #6f9be0;
}}

QToolButton#exportPrimary:disabled {{
    background-color: {Colors.SELECTED};
    color: {Colors.OVERLAY};
}}

QToolButton#exportArrow {{
    background-color: {Colors.ACCENT};
    color: {Colors.BG};
    border: none;
    border-left: 1px solid rgba(30, 30, 46, 80);
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    padding: 6px 6px;
    font-weight: 700;
}}

/* Hide Qt's auto-rendered menu-indicator triangle — the explicit
   ``▾`` glyph in the button text already conveys the dropdown
   affordance. */
QToolButton#exportArrow::menu-indicator {{
    image: none;
    width: 0;
    height: 0;
}}

QToolButton#exportArrow:hover {{
    background-color: #a4c4ff;
}}

QToolButton#exportArrow:pressed {{
    background-color: #6f9be0;
}}

QToolButton#exportArrow:disabled {{
    background-color: {Colors.SELECTED};
    color: {Colors.OVERLAY};
}}

QLabel#listStatus {{
    color: {Colors.SUBTEXT};
    font-size: 11px;
}}

QLabel#listOverlay {{
    color: {Colors.SUBTEXT};
    font-size: 14px;
    background-color: {Colors.LIST_BG};
}}

QLineEdit {{
    background-color: {Colors.LIST_BG};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: {Colors.ACCENT};
    selection-color: {Colors.BG};
}}

QLineEdit:focus {{
    border-color: {Colors.ACCENT};
}}

QListView, QTreeView {{
    background-color: {Colors.LIST_BG};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    font-family: {MONO_FONT_FAMILY};
    font-size: 12px;
    outline: 0;
}}

QListView::item, QTreeView::item {{
    padding: 4px 6px;
}}

QListView::item:hover, QTreeView::item:hover {{
    background-color: {Colors.HOVER};
}}

QListView::item:selected, QTreeView::item:selected {{
    background-color: {Colors.SELECTED};
    color: {Colors.TEXT};
}}

QMenuBar {{
    background-color: {Colors.SURFACE};
    color: {Colors.TEXT};
    border-bottom: 1px solid {Colors.BORDER};
}}

QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
}}

QMenuBar::item:selected {{
    background-color: {Colors.HOVER};
}}

QMenu {{
    background-color: {Colors.SURFACE};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
}}

QMenu::item {{
    padding: 6px 24px;
}}

QMenu::item:selected {{
    background-color: {Colors.SELECTED};
}}

QMenu::separator {{
    height: 1px;
    background-color: {Colors.BORDER};
    margin: 4px 8px;
}}

QSplitter::handle {{
    background-color: {Colors.BORDER};
}}

QSplitter::handle:horizontal {{
    width: 8px;
    margin: 0 2px;
    border-radius: 1px;
}}

QSplitter::handle:vertical {{
    height: 8px;
    margin: 2px 0;
    border-radius: 1px;
}}

QSplitter::handle:hover {{
    background-color: {Colors.OVERLAY};
}}

QSplitter::handle:pressed {{
    background-color: {Colors.ACCENT};
}}

QStatusBar {{
    background-color: {Colors.SURFACE};
    color: {Colors.SUBTEXT};
    border-top: 1px solid {Colors.BORDER};
    min-height: 38px;
    padding: 4px 6px;
}}

QStatusBar::item {{
    border: none;
}}

QScrollBar:vertical {{
    background: {Colors.BG};
    width: 12px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {Colors.SELECTED};
    border-radius: 4px;
    min-height: 24px;
    margin: 2px;
}}

QScrollBar::handle:vertical:hover {{
    background: {Colors.OVERLAY};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {Colors.BG};
    height: 12px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: {Colors.SELECTED};
    border-radius: 4px;
    min-width: 24px;
    margin: 2px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {Colors.OVERLAY};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* Tabs — used by the main page to switch between the Model Browser and
   the Character Builder. The pane border is intentionally drawn at
   ``top: -1px`` so the selected tab visually merges into the page. */
QTabWidget::pane {{
    border: 1px solid {Colors.BORDER};
    background-color: {Colors.BG};
    top: -1px;
}}

QTabWidget::tab-bar {{
    left: 8px;
}}

QTabBar {{
    background: transparent;
    qproperty-drawBase: 0;
}}

QTabBar::tab {{
    background-color: {Colors.SURFACE};
    color: {Colors.SUBTEXT};
    border: 1px solid {Colors.BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 16px;
    margin-right: 2px;
    min-width: 140px;
    font-size: 12px;
    font-weight: 600;
}}

QTabBar::tab:hover {{
    background-color: {Colors.HOVER};
    color: {Colors.TEXT};
}}

QTabBar::tab:selected {{
    background-color: {Colors.BG};
    color: {Colors.ACCENT};
    border-color: {Colors.BORDER};
}}

/* Character Builder slot card. The selected/hover combination is
   handled with attribute selectors so the SlotPanel can flip a
   dynamic ``selected`` property to switch border colours without
   re-applying a stylesheet per card. */
QFrame#slotCard {{
    background-color: {Colors.SURFACE};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
}}

QFrame#slotCard:hover {{
    background-color: {Colors.HOVER};
    border-color: {Colors.OVERLAY};
}}

QFrame#slotCard[selected="true"] {{
    border-color: {Colors.ACCENT};
    background-color: {Colors.SELECTED};
}}

QFrame#slotCard[selected="true"]:hover {{
    border-color: {Colors.ACCENT};
    background-color: {Colors.SELECTED};
}}

QLabel#slotName {{
    color: {Colors.TEXT};
    font-size: 13px;
    font-weight: 600;
}}

QLabel#slotStatus {{
    color: {Colors.OVERLAY};
    font-size: 12px;
}}

QLabel#slotStatus[filled="true"] {{
    color: {Colors.SUBTEXT};
}}

/* Customization row label — the fixed-width caption to the left of
   each combo box (Class / Gender / Face / …). */
QLabel#customLabel {{
    color: {Colors.SUBTEXT};
    font-size: 12px;
}}

QLabel#customLabel[disabled="true"] {{
    color: {Colors.OVERLAY};
}}

/* Combo boxes used by the Character Builder customization section.
   The dropdown view is themed via the ``QComboBox QAbstractItemView``
   selector; without it Qt falls back to the OS-native popup whose
   colours don't match the rest of the dark UI. */
QComboBox {{
    background-color: {Colors.SURFACE};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 22px;
    selection-background-color: {Colors.ACCENT};
    selection-color: {Colors.BG};
}}

QComboBox:hover {{
    border-color: {Colors.OVERLAY};
}}

QComboBox:focus {{
    border-color: {Colors.ACCENT};
}}

QComboBox:disabled {{
    background-color: {Colors.BG};
    color: {Colors.OVERLAY};
    border-color: {Colors.BORDER};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border: none;
    border-left: 1px solid {Colors.BORDER};
}}

QComboBox QAbstractItemView {{
    background-color: {Colors.LIST_BG};
    color: {Colors.TEXT};
    border: 1px solid {Colors.BORDER};
    selection-background-color: {Colors.SELECTED};
    selection-color: {Colors.TEXT};
    outline: 0;
    padding: 2px;
}}

/* Piece browser context label — sits between the section header and
   the search field, summarising the current class+gender+slot
   selection in muted text. */
QLabel#pieceContext {{
    color: {Colors.SUBTEXT};
    font-size: 12px;
}}
"""
