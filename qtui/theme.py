"""Palette, fonts and the global stylesheet, in a light and a dark variant.

The app follows the system appearance. Every colour lives here: widgets style
themselves through object names and dynamic properties in the one global
stylesheet, and painted widgets read these module values at paint time, so a
switch between light and dark is `apply()` plus a repaint, not a rebuild.

One accent (blue) means the same thing everywhere: the spoofed location, the
route line and primary buttons.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# ---- the two palettes --------------------------------------------------------

DARK = {
    "BG": "#0b0f18", "PANEL": "#121826", "ELEV": "#1b2233", "ELEV_HI": "#252e44",
    "BORDER": "#252e42", "TEXT": "#e9eef7", "MUTED": "#98a2b6", "FAINT": "#6f7a91",
    "ACCENT": "#3b82f6", "ACCENT_HI": "#5b9bff", "ACCENT_SOFT": "rgba(59,130,246,0.16)",
    "FLOAT": "rgba(18, 24, 38, 0.93)", "FLOAT_EDGE": "rgba(255, 255, 255, 0.08)",
    "SCRIM": "rgba(6, 9, 15, 0.62)", "MAP_BG": "#0b1120", "MAP_INK": "#c9d4ea",
    "SHADOW_ALPHA": 120, "TRAVELLED": "#7d879b", "REAL": "#aab4c6",
}
LIGHT = {
    "BG": "#f4f5f8", "PANEL": "#ffffff", "ELEV": "#eef0f4", "ELEV_HI": "#e3e7ee",
    "BORDER": "#dfe3ea", "TEXT": "#141a26", "MUTED": "#5a6477", "FAINT": "#7b8597",
    "ACCENT": "#2563eb", "ACCENT_HI": "#3b76f0", "ACCENT_SOFT": "rgba(37,99,235,0.12)",
    "FLOAT": "rgba(255, 255, 255, 0.94)", "FLOAT_EDGE": "rgba(15, 23, 42, 0.08)",
    "SCRIM": "rgba(244, 245, 248, 0.72)", "MAP_BG": "#e8ebef", "MAP_INK": "#1f2937",
    "SHADOW_ALPHA": 46, "TRAVELLED": "#8b95a7", "REAL": "#7b8597",
}

# colours that mean the same thing in both appearances
PIN, PIN_HI = "#ff5a5f", "#ff8488"          # a dropped pin (a candidate, not a location)
GREEN, AMBER, RED, GREY = "#22c55e", "#f59e0b", "#ef4444", "#8a93a5"
DANGER, DANGER_HI = "#e5484d", "#ef5f64"
LIVE, LIVE_HI = "#22d3ee", "#67e8f9"         # "ready" (visible, not yet connected)

scheme = "dark"
# module-level names, rebound by use(); read them at call/paint time
BG = PANEL = ELEV = ELEV_HI = BORDER = TEXT = MUTED = FAINT = ""
ACCENT = ACCENT_HI = ACCENT_SOFT = FLOAT = FLOAT_EDGE = SCRIM = MAP_BG = MAP_INK = ""
TRAVELLED = REAL = ""
SHADOW_ALPHA = 0
# older names, kept so nothing that still says BLUE / GHOST breaks
BLUE = BLUE_HI = GHOST = GHOST_HI = ""


def use(name: str) -> None:
    """Rebind the module colours to the light or dark palette."""
    global scheme, BLUE, BLUE_HI, GHOST, GHOST_HI
    scheme = "light" if name == "light" else "dark"
    globals().update(LIGHT if scheme == "light" else DARK)
    BLUE, BLUE_HI, GHOST, GHOST_HI = ACCENT, ACCENT_HI, ELEV, ELEV_HI


use("dark")


def is_dark() -> bool:
    return scheme == "dark"


def system_scheme(app: QApplication | None = None) -> str:
    app = app or QApplication.instance()
    try:
        if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    return "dark"


# ---- fonts ---------------------------------------------------------------------

_UI_FAMILIES = [".AppleSystemUIFont", "Helvetica Neue"]


def ui_font(size: int = 13, weight: int = 400) -> QFont:
    """The system UI font (SF Pro on macOS). `weight` is CSS-style (400/600/700)."""
    f = QFont()
    f.setFamilies(_UI_FAMILIES)
    f.setPointSize(size)
    f.setWeight(QFont.Weight(int(weight)))
    return f


def num_font(size: int = 13, weight: int = 500) -> QFont:
    """The UI font with tabular (fixed-width) digits, for numbers that update
    live: a distance ticking down must not make its label jitter."""
    f = ui_font(size, weight)
    try:
        f.setFeature(QFont.Tag("tnum"), 1)
    except Exception:
        pass
    return f


def mono_font(size: int = 11) -> QFont:
    f = QFont()
    f.setFamilies(["SF Mono", "Menlo", "Monaco"])
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(size)
    return f


# ---- the stylesheet --------------------------------------------------------

def stylesheet() -> str:
    return f"""
* {{ color: {TEXT}; font-family: ".AppleSystemUIFont", "Helvetica Neue"; }}
QWidget#Root {{ background: {BG}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};
            border-radius: 6px; padding: 5px 8px; }}

/* surfaces */
QFrame#Float  {{ background: {FLOAT}; border: 1px solid {FLOAT_EDGE}; border-radius: 14px; }}
QFrame#Card   {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 18px; }}
QFrame#Group  {{ background: {ELEV}; border-radius: 12px; }}
QFrame#Sheet  {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QFrame#Hairline  {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#VHairline {{ background: {BORDER}; max-width: 1px; min-width: 1px; border: none; }}
QFrame#Banner {{ background: {FLOAT}; border: 1px solid {FLOAT_EDGE}; border-radius: 12px; }}
QFrame#Toast  {{ background: {TEXT}; border-radius: 12px; }}
QLabel#ToastText {{ color: {BG}; }}
QFrame#Row:hover {{ background: {ELEV_HI}; border-radius: 10px; }}

/* text roles */
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="faint"] {{ color: {FAINT}; }}
QLabel[role="accent"] {{ color: {ACCENT}; }}
QLabel[role="good"] {{ color: {GREEN}; }}
QLabel[role="warn"] {{ color: {AMBER}; }}
QLabel[role="bad"] {{ color: {RED}; }}

/* buttons */
QPushButton {{ border: none; border-radius: 9px; padding: 0 14px; font-weight: 600; font-size: 13px; }}
QPushButton[variant="primary"] {{ background: {ACCENT}; color: #ffffff; }}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HI}; }}
QPushButton[variant="primary"]:disabled {{ background: {ELEV}; color: {FAINT}; }}
QPushButton[variant="soft"] {{ background: {ELEV}; color: {TEXT}; }}
QPushButton[variant="soft"]:hover {{ background: {ELEV_HI}; }}
QPushButton[variant="soft"]:disabled {{ color: {FAINT}; }}
QPushButton[variant="ghost"] {{ background: transparent; color: {TEXT}; border: 1px solid {BORDER}; }}
QPushButton[variant="ghost"]:hover {{ background: {ELEV}; }}
QPushButton[variant="danger"] {{ background: {DANGER}; color: #ffffff; }}
QPushButton[variant="danger"]:hover {{ background: {DANGER_HI}; }}
QPushButton[variant="dangerlink"] {{ background: transparent; color: {DANGER}; padding: 0 6px; }}
QPushButton[variant="dangerlink"]:hover {{ background: {ELEV}; }}
QPushButton[variant="link"] {{ background: transparent; color: {ACCENT}; padding: 0 6px; }}
QPushButton[variant="link"]:hover {{ background: {ACCENT_SOFT}; }}
QPushButton[variant="icon"] {{ background: transparent; border-radius: 9px; padding: 0; }}
QPushButton[variant="icon"]:hover {{ background: {ELEV_HI}; }}
QPushButton[variant="icon"]:checked {{ background: {ACCENT_SOFT}; }}
QPushButton[variant="row"] {{ background: transparent; text-align: left; padding: 0 12px;
                              font-weight: 500; border-radius: 9px; }}
QPushButton[variant="row"]:hover {{ background: {ELEV_HI}; }}

/* segmented controls + chips */
QFrame#Seg {{ background: {ELEV}; }}
QPushButton#SegBtn {{ background: transparent; color: {MUTED}; padding: 0 6px; }}
QPushButton#SegBtn:hover:!checked {{ color: {TEXT}; }}
QPushButton#SegBtn:checked {{ background: {ACCENT}; color: #ffffff; }}
QPushButton#SegBtn:disabled {{ color: {FAINT}; }}

/* inputs */
QLineEdit {{ background: transparent; border: none; color: {TEXT};
             selection-background-color: {ACCENT}; font-size: 14px; }}
QLineEdit#Field, QSpinBox, QDoubleSpinBox {{
    background: {ELEV}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 3px 8px; color: {TEXT}; selection-background-color: {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {ELEV_HI}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #ffffff; border: 1px solid {BORDER};
                              width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; }}
QProgressBar {{ background: {ELEV_HI}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* lists + scrolling */
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border: none; padding: 0; }}
QListWidget::item:selected {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {ELEV_HI}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* menus + dialogs */
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QDialog, QMessageBox {{ background: {PANEL}; }}
QInputDialog QLineEdit {{ background: {ELEV}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px; }}
QMessageBox QPushButton, QInputDialog QPushButton, QDialogButtonBox QPushButton {{
    background: {ELEV}; min-width: 88px; min-height: 28px; }}
QMessageBox QPushButton:default, QDialogButtonBox QPushButton:default {{ background: {ACCENT}; color: #ffffff; }}
"""


# kept for any caller that reads the constant; apply() always regenerates it
STYLESHEET = stylesheet()


def apply(app: QApplication, name: str | None = None) -> None:
    """Switch the whole app to the light or dark look (default: the system's)."""
    global STYLESHEET
    use(name or system_scheme(app))
    app.setStyle("Fusion")          # a consistent base we fully restyle
    pal = QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, BG), (QPalette.ColorRole.Base, ELEV),
        (QPalette.ColorRole.AlternateBase, PANEL), (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.WindowText, TEXT), (QPalette.ColorRole.Button, ELEV),
        (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, PANEL),
        (QPalette.ColorRole.ToolTipText, TEXT),
        # Qt has no ::placeholder CSS; the palette role is the real mechanism
        (QPalette.ColorRole.PlaceholderText, FAINT),
    ):
        pal.setColor(role, QColor(value))
    app.setPalette(pal)
    app.setFont(ui_font(13))
    STYLESHEET = stylesheet()
    app.setStyleSheet(STYLESHEET)


def apply_theme(app: QApplication) -> None:
    """Backwards-compatible entry point: follow the system appearance."""
    apply(app)
