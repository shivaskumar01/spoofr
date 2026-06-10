"""Palette, fonts, and the global stylesheet for the Qt front-end.

Colours mirror the established Spoofr brand (near-black canvas · deep-blue
surfaces · crisp blue accent) so the native app looks like the same product,
just smoother. Everything visual lives here so the rest of the UI stays terse.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# --- palette --------------------------------------------------------------
BG       = "#0a0e17"   # app background
PANEL    = "#111a2c"   # bars / map frame
ELEV     = "#1a2440"   # raised pills, inputs, controls
ELEV_HI  = "#222e52"   # elevated hover
BORDER   = "#27344f"   # hairline dividers & outlines
BLUE     = "#3b82f6"   # primary accent
BLUE_HI  = "#5c9bff"   # primary hover
GHOST    = "#1c2740"   # secondary fill
GHOST_HI = "#283655"   # secondary hover
TEXT     = "#eaf0fb"   # primary text
MUTED    = "#8a97b4"   # secondary text
FAINT    = "#5d6b8a"   # tertiary text / placeholders
PIN      = "#ff5a5f"   # staged target pin
PIN_HI   = "#ff8488"
LIVE     = "#22d3ee"   # live (current) location marker
LIVE_HI  = "#67e8f9"
MAP_BG   = "#0b0f19"   # tile placeholder / canvas behind the map

# connection dot
GREY, GREEN, AMBER, RED = "#6b7687", "#2bd07a", "#f4b740", "#ff5c5c"


def ui_font(size: int = 13, weight: int = 400) -> QFont:
    """The system UI font (SF Pro on macOS), at a given point size.

    `weight` is a CSS-style integer (400 normal, 600 semibold, 700 bold).
    """
    f = QFont()
    f.setFamilies([".AppleSystemUIFont", "Helvetica Neue"])
    f.setPointSize(size)
    f.setWeight(QFont.Weight(int(weight)))
    return f


def mono_font(size: int = 11) -> QFont:
    f = QFont()
    f.setFamilies(["SF Mono", "Menlo", "JetBrains Mono", "monospace"])
    f.setPointSize(size)
    return f


# The global stylesheet. Object names / dynamic properties select variants:
#   QPushButton[variant="primary"|"soft"|"ghost"]
#   QFrame#Card, QFrame#Bar, QFrame#Pill, QFrame#Hairline
STYLESHEET = f"""
* {{
    color: {TEXT};
    font-family: ".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue";
}}
QWidget#Root {{ background: {BG}; }}
QToolTip {{
    background: {ELEV}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 5px 8px;
}}

/* ---- surfaces ---- */
QFrame#Bar    {{ background: {PANEL}; }}
QFrame#Card   {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 16px; }}
QFrame#Pill   {{ background: {ELEV};  border-radius: 15px; }}
QFrame#Hairline {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#MapFrame {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}

/* ---- buttons ---- */
QPushButton {{
    border: none; border-radius: 10px; padding: 0 12px;
    font-weight: 600; font-size: 13px;
}}
QPushButton[variant="primary"] {{ background: {BLUE}; color: #ffffff; }}
QPushButton[variant="primary"]:hover {{ background: {BLUE_HI}; }}
QPushButton[variant="primary"]:pressed {{ background: {BLUE}; }}
QPushButton[variant="primary"]:disabled {{ background: {GHOST}; color: {FAINT}; }}

QPushButton[variant="soft"] {{ background: {GHOST}; color: {TEXT}; }}
QPushButton[variant="soft"]:hover {{ background: {GHOST_HI}; }}

QPushButton[variant="ghost"] {{ background: transparent; color: {TEXT}; border: 1px solid {BORDER}; }}
QPushButton[variant="ghost"]:hover {{ background: {GHOST}; }}
QPushButton[variant="ghost"]:disabled {{ color: {FAINT}; border-color: {BORDER}; }}

QPushButton[variant="danger"] {{ background: #e5484d; color: #ffffff; }}
QPushButton[variant="danger"]:hover {{ background: #ec5d62; }}
QPushButton[variant="danger"]:disabled {{ background: {GHOST}; color: {FAINT}; }}

QPushButton[variant="icon"] {{ background: transparent; color: {TEXT}; border-radius: 10px; padding: 0; }}
QPushButton[variant="icon"]:hover {{ background: {GHOST}; }}

/* ---- inputs ---- */
QLineEdit {{
    background: transparent; border: none; color: {TEXT};
    selection-background-color: {BLUE}; font-size: 14px;
}}

/* ---- scrollbars (thin, unobtrusive) ---- */
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {GHOST_HI}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}
"""


def apply_theme(app: QApplication) -> None:
    """Install the dark palette + global stylesheet on the application."""
    app.setStyle("Fusion")  # consistent cross-platform base we fully restyle
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.Base, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(BLUE))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(ELEV))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    # Qt has no ::placeholder CSS — the palette role is the real mechanism
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(FAINT))
    app.setPalette(pal)
    app.setFont(ui_font(13))
    app.setStyleSheet(STYLESHEET)
