"""Palette, fonts, and the global stylesheet for the Qt front-end.

One quiet dark palette: a deep navy canvas (the map is recoloured into the same
ramp, so chrome and map read as one surface), a single blue accent, and colour
used only where it means something: cyan is where the iPhone is, coral is where
you are about to send it, green is a destination or a healthy link.
Everything visual lives here so the rest of the UI stays terse.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# --- palette --------------------------------------------------------------
BG       = "#0b0f18"   # window background
PANEL    = "#111726"   # header, footer, side menu
ELEV     = "#1a2133"   # raised controls, inputs, segmented tracks
ELEV_HI  = "#232c42"   # raised hover
BORDER   = "#222b3d"   # hairline dividers & outlines
BLUE     = "#3b82f6"   # the one accent
BLUE_HI  = "#5b9bff"   # accent hover
GHOST    = "#1a2133"   # secondary fill
GHOST_HI = "#252f46"   # secondary hover
TEXT     = "#e8edf7"   # primary text
MUTED    = "#8b96ac"   # secondary text
FAINT    = "#5b6780"   # tertiary text / placeholders
PIN      = "#ff5a5f"   # staged target pin
PIN_HI   = "#ff8488"
LIVE     = "#22d3ee"   # where the iPhone is
LIVE_HI  = "#67e8f9"
MAP_BG   = "#0b1120"   # empty map / the dark end of the map's colour ramp
MAP_INK  = "#c9d4ea"   # the light end of the map ramp (labels)

# floating panels over the map: nearly opaque, with a faint light edge that
# separates them from the map without a (costly, blurry) drop shadow
FLOAT        = "rgba(17, 23, 38, 0.94)"
FLOAT_EDGE   = "rgba(255, 255, 255, 0.08)"

# status colours
GREY, GREEN, AMBER, RED = "#6b7687", "#2bd07a", "#f4b740", "#ff5c5c"
DANGER, DANGER_HI = "#e5484d", "#ef5f64"

_UI_FAMILIES = [".AppleSystemUIFont", "Helvetica Neue"]


def ui_font(size: int = 13, weight: int = 400) -> QFont:
    """The system UI font (SF Pro on macOS), at a given point size.

    `weight` is a CSS-style integer (400 normal, 600 semibold, 700 bold).
    """
    f = QFont()
    f.setFamilies(_UI_FAMILIES)
    f.setPointSize(size)
    f.setWeight(QFont.Weight(int(weight)))
    return f


def mono_font(size: int = 11) -> QFont:
    f = QFont()
    f.setFamilies(["SF Mono", "Menlo", "Monaco"])
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(size)
    return f


# The global stylesheet. Object names / dynamic properties select variants:
#   QPushButton[variant="primary"|"soft"|"ghost"|"danger"|"icon"]
#   QFrame#Bar, QFrame#Card, QFrame#Float, QFrame#Group, QFrame#Hairline
STYLESHEET = f"""
* {{
    color: {TEXT};
    font-family: ".AppleSystemUIFont", "Helvetica Neue";
}}
QWidget#Root {{ background: {BG}; }}
QToolTip {{
    background: {ELEV}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 5px 8px;
}}

/* ---- surfaces ---- */
QFrame#Bar      {{ background: {PANEL}; }}
QFrame#Card     {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 18px; }}
QFrame#Float    {{ background: {FLOAT}; border: 1px solid {FLOAT_EDGE}; border-radius: 14px; }}
QFrame#Group    {{ background: {ELEV}; border-radius: 12px; }}
QFrame#Hairline {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#VHairline {{ background: {BORDER}; max-width: 1px; min-width: 1px; border: none; }}

/* ---- buttons ---- */
QPushButton {{
    border: none; border-radius: 9px; padding: 0 14px;
    font-weight: 600; font-size: 13px;
}}
QPushButton[variant="primary"] {{ background: {BLUE}; color: #ffffff; }}
QPushButton[variant="primary"]:hover {{ background: {BLUE_HI}; }}
QPushButton[variant="primary"]:pressed {{ background: {BLUE}; }}
QPushButton[variant="primary"]:disabled {{ background: {GHOST}; color: {FAINT}; }}

QPushButton[variant="soft"] {{ background: {GHOST}; color: {TEXT}; }}
QPushButton[variant="soft"]:hover {{ background: {GHOST_HI}; }}
QPushButton[variant="soft"]:disabled {{ color: {FAINT}; }}

QPushButton[variant="ghost"] {{ background: transparent; color: {TEXT}; border: 1px solid {BORDER}; }}
QPushButton[variant="ghost"]:hover {{ background: {GHOST}; }}
QPushButton[variant="ghost"]:disabled {{ color: {FAINT}; }}

QPushButton[variant="danger"] {{ background: {DANGER}; color: #ffffff; }}
QPushButton[variant="danger"]:hover {{ background: {DANGER_HI}; }}
QPushButton[variant="danger"]:disabled {{ background: {GHOST}; color: {FAINT}; }}

QPushButton[variant="icon"] {{ background: transparent; color: {TEXT}; border-radius: 9px; padding: 0; }}
QPushButton[variant="icon"]:hover {{ background: {GHOST_HI}; }}
QPushButton[variant="icon"]:disabled {{ color: {FAINT}; }}

/* ---- inputs ---- */
QLineEdit {{
    background: transparent; border: none; color: {TEXT};
    selection-background-color: {BLUE}; font-size: 14px;
}}

/* ---- sliders ---- */
QSlider::groove:horizontal {{ height: 4px; background: {ELEV_HI}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {BLUE}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #ffffff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
}}

/* ---- scrollbars (thin, unobtrusive) ---- */
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {GHOST_HI}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}

/* ---- dialogs ---- */
QDialog {{ background: {BG}; }}
QInputDialog QLineEdit {{
    background: {ELEV}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
}}
QMessageBox QLabel {{ color: {TEXT}; }}
"""


def apply_theme(app: QApplication) -> None:
    """Install the dark palette + global stylesheet on the application."""
    app.setStyle("Fusion")  # consistent cross-platform base we fully restyle
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.Base, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(BLUE))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    # Qt has no ::placeholder CSS, the palette role is the real mechanism
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(FAINT))
    app.setPalette(pal)
    app.setFont(ui_font(13))
    app.setStyleSheet(STYLESHEET)
