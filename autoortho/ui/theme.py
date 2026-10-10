"""Semantic Qt theme tokens and styling for AutoOrtho."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from PySide6.QtCore import QObject
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QColor,
    QFontDatabase,
    QPalette,
)
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget


@dataclass(frozen=True)
class ColorTokens:
    window: str = "#1e1e1e"
    surface: str = "#2a2a2a"
    elevated: str = "#3a3a3a"
    border: str = "#737373"
    divider: str = "#555555"
    primary: str = "#2b75c5"
    hover: str = "#2d75c1"
    pressed: str = "#2b75c5"
    focus: str = "#6da4e3"
    success: str = "#6fbe73"
    warning: str = "#d7a33c"
    error: str = "#dd7373"
    info: str = "#6da4e3"
    text_primary: str = "#e0e0e0"
    text_secondary: str = "#b0b0b0"
    text_disabled: str = "#a4a4a4"


@dataclass(frozen=True)
class SpacingTokens:
    xxs: int = 2
    xs: int = 4
    sm: int = 6
    md: int = 10
    lg: int = 14
    xl: int = 18
    xxl: int = 24


@dataclass(frozen=True)
class RadiusTokens:
    xs: int = 2
    sm: int = 3
    md: int = 4
    lg: int = 4
    pill: int = 4


@dataclass(frozen=True)
class TypographyTokens:
    base: int = 10
    small: int = 9
    caption: int = 9
    heading: int = 11
    title: int = 13
    display: int = 17


@dataclass(frozen=True)
class ControlHeightTokens:
    sm: int = 22
    md: int = 28
    lg: int = 32
    xl: int = 38


@dataclass(frozen=True)
class ThemeTokens:
    colors: ColorTokens = field(default_factory=ColorTokens)
    spacing: SpacingTokens = field(default_factory=SpacingTokens)
    radius: RadiusTokens = field(default_factory=RadiusTokens)
    typography: TypographyTokens = field(default_factory=TypographyTokens)
    control_heights: ControlHeightTokens = field(default_factory=ControlHeightTokens)


THEME = ThemeTokens()


def _asset_path(filename: str) -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "autoortho" / "imgs"
    else:
        root = Path(__file__).resolve().parent.parent / "imgs"
    return (root / filename).as_posix()


def _to_qcolor(value: str | QColor) -> QColor:
    color = value if isinstance(value, QColor) else QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid color value: {value!r}")
    return color


def _blend(top: str | QColor, bottom: str | QColor, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    top_color = _to_qcolor(top)
    bottom_color = _to_qcolor(bottom)
    red = round(top_color.red() * ratio + bottom_color.red() * (1.0 - ratio))
    green = round(top_color.green() * ratio + bottom_color.green() * (1.0 - ratio))
    blue = round(top_color.blue() * ratio + bottom_color.blue() * (1.0 - ratio))
    return QColor(red, green, blue).name()


def contrast_ratio(foreground: str | QColor, background: str | QColor) -> float:
    """Return the WCAG contrast ratio between two colors."""

    fg = _to_qcolor(foreground)
    bg = _to_qcolor(background)

    def channel(value: int) -> float:
        normalized = value / 255.0
        return normalized / 12.92 if normalized <= 0.03928 else ((normalized + 0.055) / 1.055) ** 2.4

    fg_luminance = (
        0.2126 * channel(fg.red())
        + 0.7152 * channel(fg.green())
        + 0.0722 * channel(fg.blue())
    )
    bg_luminance = (
        0.2126 * channel(bg.red())
        + 0.7152 * channel(bg.green())
        + 0.0722 * channel(bg.blue())
    )
    lighter = max(fg_luminance, bg_luminance)
    darker = min(fg_luminance, bg_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def validate_theme_tokens(theme: ThemeTokens = THEME) -> list[str]:
    """Return a list of contrast or range violations for the given theme."""

    issues: list[str] = []
    colors = theme.colors
    backgrounds = [colors.window, colors.surface, colors.elevated]
    text_backgrounds = backgrounds
    boundary_backgrounds = [colors.window, colors.surface]

    for name in colors.__dataclass_fields__:
        try:
            _to_qcolor(getattr(colors, name))
        except ValueError as exc:
            issues.append(f"colors.{name}: {exc}")

    for group_name, group in (
        ("spacing", theme.spacing),
        ("radius", theme.radius),
        ("typography", theme.typography),
        ("control_heights", theme.control_heights),
    ):
        for name in group.__dataclass_fields__:
            value = getattr(group, name)
            if value <= 0:
                issues.append(f"{group_name}.{name}: must be positive")

    for fg_name in ("text_primary", "text_secondary", "text_disabled"):
        fg = getattr(colors, fg_name)
        for bg in text_backgrounds:
            ratio = contrast_ratio(fg, bg)
            if ratio < 4.5:
                issues.append(
                    f"{fg_name} vs {bg}: contrast {ratio:.2f} < 4.5"
                )

    for fg_name in (
        "border",
        "focus",
        "primary",
        "hover",
        "pressed",
        "success",
        "warning",
        "error",
        "info",
    ):
        fg = getattr(colors, fg_name)
        for bg in boundary_backgrounds:
            ratio = contrast_ratio(fg, bg)
            if ratio < 3.0:
                issues.append(
                    f"{fg_name} vs {bg}: contrast {ratio:.2f} < 3.0"
                )
    if contrast_ratio(colors.divider, colors.window) < 2.0:
        issues.append("divider: contrast must be at least 2.0")

    return issues


def _palette(theme: ThemeTokens) -> QPalette:
    colors = theme.colors
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, _to_qcolor(colors.window))
    palette.setColor(QPalette.ColorRole.WindowText, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.Base, _to_qcolor(colors.surface))
    palette.setColor(QPalette.ColorRole.AlternateBase, _to_qcolor(colors.elevated))
    palette.setColor(QPalette.ColorRole.ToolTipBase, _to_qcolor(colors.elevated))
    palette.setColor(QPalette.ColorRole.ToolTipText, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.Text, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.BrightText, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.Button, _to_qcolor(colors.surface))
    palette.setColor(QPalette.ColorRole.ButtonText, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.Mid, _to_qcolor(colors.divider))
    palette.setColor(QPalette.ColorRole.Dark, _to_qcolor(colors.window))
    palette.setColor(QPalette.ColorRole.Light, _to_qcolor(colors.elevated))
    palette.setColor(QPalette.ColorRole.Shadow, _to_qcolor(colors.border))
    palette.setColor(QPalette.ColorRole.Highlight, _to_qcolor(colors.primary))
    palette.setColor(QPalette.ColorRole.HighlightedText, _to_qcolor(colors.text_primary))
    palette.setColor(QPalette.ColorRole.Link, _to_qcolor(colors.info))
    palette.setColor(QPalette.ColorRole.LinkVisited, _to_qcolor(colors.focus))
    palette.setColor(QPalette.ColorRole.PlaceholderText, _to_qcolor(colors.text_secondary))

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.WindowText, _to_qcolor(colors.text_disabled))
    palette.setColor(disabled, QPalette.ColorRole.Text, _to_qcolor(colors.text_disabled))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, _to_qcolor(colors.text_disabled))
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, _to_qcolor(colors.text_disabled))
    return palette


def build_stylesheet(theme: ThemeTokens = THEME) -> str:
    """Return the dark application stylesheet for the theme."""

    colors = theme.colors
    spacing = theme.spacing
    radius = theme.radius
    typography = theme.typography
    control = theme.control_heights

    soft_primary = _blend(colors.primary, colors.window, 0.18)
    soft_success = _blend(colors.success, colors.window, 0.10)
    soft_warning = _blend(colors.warning, colors.window, 0.10)
    soft_error = _blend(colors.error, colors.window, 0.10)
    soft_info = _blend(colors.info, colors.window, 0.10)

    base = f"""
QWidget {{
    color: {colors.text_primary};
}}
QMainWindow, QDialog {{
    background-color: {colors.window};
}}
QWidget#applicationShell,
QWidget#homePage {{
    background-color: {colors.window};
}}
QFrame {{
    border: none;
}}
QFrame#compactHeader {{
    background-color: {colors.window};
    border: none;
    border-bottom: 1px solid {colors.divider};
    border-radius: 0;
}}
QFrame#navigationRail {{
    background-color: {colors.window};
    border: none;
    border-right: 1px solid {colors.divider};
    border-radius: 0;
}}
QFrame#updateBanner {{
    background-color: {colors.surface};
    border: 1px solid {colors.info};
    border-radius: {radius.md}px;
}}
QGroupBox {{
    background-color: {colors.window};
    border: 1px solid {colors.divider};
    border-radius: {radius.lg}px;
    margin-top: {spacing.md}px;
    padding-top: {spacing.sm}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: {spacing.md}px;
    padding: 0 {spacing.sm}px;
    color: {colors.text_primary};
    font-size: {typography.heading + 1}pt;
    font-weight: 600;
}}
QLabel {{
    color: {colors.text_primary};
}}
QLabel[textRole="pageTitle"] {{
    color: {colors.text_primary};
    font-size: {typography.display}pt;
    font-weight: 700;
}}
QLabel[textRole="sectionTitle"] {{
    color: {colors.text_primary};
    font-size: {typography.title}pt;
    font-weight: 650;
}}
QLabel[textRole="secondary"] {{
    color: {colors.text_secondary};
}}
QLabel[textRole="caption"] {{
    color: {colors.text_secondary};
    font-size: {typography.small}pt;
}}
QLabel[textRole="info"] {{
    color: {colors.info};
}}
QLabel[textRole="success"] {{
    color: {colors.success};
}}
QLabel[textRole="warning"] {{
    color: {colors.warning};
}}
QLabel[textRole="error"] {{
    color: {colors.error};
}}
QLabel#appIconLabel {{
    background-color: transparent;
    border: none;
    border-radius: 0;
}}
QLabel#brandImageLabel {{
    border: 1px solid {colors.divider};
    border-radius: {radius.sm}px;
    background-color: {colors.surface};
}}
QFrame#statusCard,
QFrame#SceneryCard {{
    background-color: {colors.surface};
    border: 1px solid {colors.divider};
    border-radius: {radius.lg}px;
}}
QFrame#statusCard {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {colors.divider};
    border-radius: 0;
}}
QFrame#statusCard QLabel,
QFrame#SceneryCard QLabel,
QFrame#compactHeader QLabel,
QFrame#updateBanner QLabel {{
    background-color: transparent;
}}
QFrame#statusCard[state="success"],
QFrame#SceneryCard[state="success"] {{
    border-left: 3px solid {colors.success};
}}
QFrame#statusCard[state="warning"],
QFrame#SceneryCard[state="warning"] {{
    border-left: 3px solid {colors.warning};
}}
QFrame#statusCard[state="error"],
QFrame#SceneryCard[state="error"] {{
    border-left: 3px solid {colors.error};
}}
QFrame#statusCard[state="info"],
QFrame#SceneryCard[state="info"] {{
    border-left: 3px solid {colors.info};
}}
QFrame#statusCard:focus,
QFrame#SceneryCard:focus {{
    border: 2px solid {colors.focus};
}}
QLabel#chipLabel {{
    background-color: {colors.surface};
    border: 1px solid {colors.divider};
    border-radius: {radius.pill}px;
    color: {colors.text_primary};
    min-height: {control.sm}px;
    padding: 1px {spacing.sm}px;
    font-size: {typography.small}pt;
}}
QLabel#chipLabel[state="success"],
QPushButton[chip="true"][state="success"],
QToolButton[chip="true"][state="success"] {{
    background-color: {colors.surface};
    border-color: {colors.success};
    color: {colors.text_primary};
}}
QLabel#chipLabel[state="warning"],
QPushButton[chip="true"][state="warning"],
QToolButton[chip="true"][state="warning"] {{
    background-color: {colors.surface};
    border-color: {colors.warning};
    color: {colors.text_primary};
}}
QLabel#chipLabel[state="error"],
QPushButton[chip="true"][state="error"],
QToolButton[chip="true"][state="error"] {{
    background-color: {colors.surface};
    border-color: {colors.error};
    color: {colors.text_primary};
}}
QLabel#chipLabel[state="info"],
QPushButton[chip="true"][state="info"],
QToolButton[chip="true"][state="info"] {{
    background-color: {colors.surface};
    border-color: {colors.info};
    color: {colors.text_primary};
}}
QAbstractScrollArea,
QScrollArea,
QListView,
QTreeView,
QTableView,
QListWidget,
QTreeWidget {{
    background-color: {colors.surface};
    alternate-background-color: {colors.elevated};
    selection-background-color: {soft_primary};
    selection-color: {colors.text_primary};
    border: 1px solid {colors.border};
    border-radius: {radius.sm}px;
    gridline-color: {colors.divider};
    outline: 0;
}}
QAbstractItemView::item {{
    padding: {spacing.xs}px {spacing.sm}px;
}}
QListWidget#settingsCategoryList::item {{
    min-height: {control.sm}px;
    padding: 1px {spacing.sm}px;
}}
QAbstractItemView::item:selected {{
    background-color: {soft_primary};
    color: {colors.text_primary};
}}
QHeaderView::section {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
    border: none;
    border-right: 1px solid {colors.divider};
    border-bottom: 1px solid {colors.divider};
    padding: {spacing.xs}px {spacing.md}px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background-color: {colors.elevated};
    border: none;
    border-right: 1px solid {colors.divider};
    border-bottom: 1px solid {colors.divider};
}}
QMenuBar {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
}}
QMenuBar::item {{
    padding: {spacing.xs}px {spacing.sm}px;
    border-radius: {radius.sm}px;
}}
QMenuBar::item:selected {{
    background-color: {colors.surface};
    color: {colors.text_primary};
}}
QMenu {{
    background-color: {colors.elevated};
    border: 1px solid {colors.divider};
    padding: {spacing.xs}px;
}}
QMenu::item {{
    padding: {spacing.xs}px {spacing.md}px;
    border-radius: {radius.sm}px;
}}
QMenu::item:selected {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
}}
QMenu::separator {{
    background: {colors.divider};
    height: 1px;
    margin: {spacing.xs}px {spacing.sm}px;
}}
QToolTip {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
    border: 1px solid {colors.focus};
    border-radius: {radius.sm}px;
    padding: {spacing.xs}px {spacing.sm}px;
    font-size: {typography.small}pt;
}}
QStatusBar {{
    background-color: {colors.elevated};
    border-top: 1px solid {colors.divider};
    color: {colors.text_secondary};
}}
QStatusBar::item {{
    border: none;
}}
QTabWidget::pane {{
    background-color: {colors.surface};
    border: 1px solid {colors.border};
    border-radius: {radius.lg}px;
}}
QTabBar::tab {{
    background-color: {colors.elevated};
    color: {colors.text_secondary};
    border: 1px solid {colors.divider};
    border-bottom: none;
    border-top-left-radius: {radius.md}px;
    border-top-right-radius: {radius.md}px;
    margin-right: 2px;
    padding: {spacing.sm}px {spacing.md}px;
}}
QTabBar::tab:selected {{
    background-color: {colors.surface};
    color: {colors.text_primary};
    border-color: {colors.border};
}}
QTabBar::tab:hover {{
    color: {colors.text_primary};
    background-color: {colors.surface};
}}
QTabBar::tab:focus {{
    border-color: {colors.focus};
}}
QCheckBox, QRadioButton {{
    spacing: {spacing.sm}px;
    border: 1px solid transparent;
    border-radius: {radius.sm}px;
    padding: {spacing.xxs}px;
}}
QCheckBox:focus, QRadioButton:focus, QSlider:focus {{
    border: 2px solid {colors.focus};
}}
QCheckBox::indicator,
QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border-radius: {radius.sm}px;
    border: 1px solid {colors.border};
    background-color: {colors.surface};
}}
QCheckBox::indicator:checked,
QRadioButton::indicator:checked {{
    background-color: {colors.primary};
    border-color: {colors.primary};
}}
QCheckBox::indicator:checked {{
    image: url("{_asset_path("checkmark.svg")}");
}}
QCheckBox::indicator:indeterminate {{
    background-color: {colors.focus};
    border-color: {colors.focus};
}}
QCheckBox::indicator:disabled,
QRadioButton::indicator:disabled {{
    background-color: {colors.window};
    border-color: {colors.divider};
}}
QSlider::groove:horizontal {{
    height: 4px;
    border-radius: 2px;
    background: {colors.divider};
}}
QSlider::sub-page:horizontal {{
    background: {colors.primary};
    border-radius: 2px;
}}
QSlider::add-page:horizontal {{
    background: {colors.divider};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {colors.info};
    border: none;
    width: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::groove:vertical {{
    width: 4px;
    border-radius: 2px;
    background: {colors.divider};
}}
QSlider::sub-page:vertical {{
    background: {colors.primary};
    border-radius: 2px;
}}
QSlider::add-page:vertical {{
    background: {colors.divider};
    border-radius: 2px;
}}
QSlider::handle:vertical {{
    background: {colors.text_primary};
    border: 1px solid {colors.focus};
    height: 14px;
    margin: 0 -6px;
    border-radius: 7px;
}}
QProgressBar {{
    background-color: {colors.surface};
    color: {colors.text_primary};
    border: 1px solid {colors.border};
    border-radius: {radius.sm}px;
    min-height: {control.md}px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {colors.primary};
    border-radius: {radius.sm}px;
}}
QScrollBar:vertical {{
    background: {colors.window};
    width: 12px;
    margin: 12px 2px 12px 2px;
    border-radius: 6px;
}}
QScrollBar::handle:vertical {{
    background: {colors.divider};
    min-height: 24px;
    border-radius: 6px;
}}
QScrollBar::handle:vertical:hover {{
    background: {colors.border};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: none;
    border: none;
}}
QScrollBar:horizontal {{
    background: {colors.window};
    height: 12px;
    margin: 2px 12px 2px 12px;
    border-radius: 6px;
}}
QScrollBar::handle:horizontal {{
    background: {colors.divider};
    min-width: 24px;
    border-radius: 6px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {colors.border};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: none;
    border: none;
}}
QSplitter::handle {{
    background: {colors.divider};
}}
QAbstractSpinBox,
QSpinBox,
QDoubleSpinBox,
QComboBox,
QLineEdit,
QTextEdit,
QPlainTextEdit {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
    border: 1px solid {colors.border};
    border-radius: {radius.sm}px;
    min-height: {control.md}px;
    padding: {spacing.xs}px {spacing.sm}px;
}}
QComboBox {{
    padding-right: {spacing.xl}px;
}}
QComboBox::drop-down {{
    border: none;
    width: {control.sm}px;
}}
QComboBox::down-arrow {{
    width: 10px;
    height: 10px;
    margin-right: {spacing.sm}px;
}}
QAbstractSpinBox::up-button,
QAbstractSpinBox::down-button {{
    background-color: {colors.elevated};
    border-left: 1px solid {colors.divider};
    width: {control.sm}px;
}}
QSpinBox::up-button,
QSpinBox::down-button,
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {{
    background-color: {colors.elevated};
    border-left: 1px solid {colors.divider};
    width: {control.sm}px;
}}
QAbstractSpinBox::up-button:hover,
QAbstractSpinBox::down-button:hover {{
    background-color: {colors.surface};
}}
QSpinBox::up-button:hover,
QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover {{
    background-color: {colors.surface};
}}
QLineEdit::placeholderText,
QTextEdit::placeholderText,
QPlainTextEdit::placeholderText {{
    color: {colors.text_secondary};
}}
QLineEdit:focus,
QTextEdit:focus,
QPlainTextEdit:focus,
QComboBox:focus,
QSpinBox:focus,
QDoubleSpinBox:focus,
QAbstractSpinBox:focus,
QPushButton:focus,
QToolButton:focus,
QAbstractItemView:focus {{
    border: 2px solid {colors.focus};
}}
QLineEdit[validationError="true"],
QLineEdit[validationState="error"],
QTextEdit[validationState="error"],
QPlainTextEdit[validationState="error"],
QComboBox[validationState="error"],
QSpinBox[validationState="error"],
QDoubleSpinBox[validationState="error"],
QAbstractSpinBox[validationState="error"],
QWidget[validationState="error"] {{
    background-color: {soft_error};
    border-color: {colors.error};
    color: {colors.text_primary};
}}
QLineEdit[validationState="warning"],
QTextEdit[validationState="warning"],
QPlainTextEdit[validationState="warning"],
QComboBox[validationState="warning"],
QSpinBox[validationState="warning"],
QDoubleSpinBox[validationState="warning"],
QAbstractSpinBox[validationState="warning"],
QWidget[validationState="warning"] {{
    background-color: {soft_warning};
    border-color: {colors.warning};
    color: {colors.text_primary};
}}
QLineEdit[validationState="success"],
QTextEdit[validationState="success"],
QPlainTextEdit[validationState="success"],
QComboBox[validationState="success"],
QSpinBox[validationState="success"],
QDoubleSpinBox[validationState="success"],
QAbstractSpinBox[validationState="success"],
QWidget[validationState="success"] {{
    background-color: {soft_success};
    border-color: {colors.success};
    color: {colors.text_primary};
}}
QLineEdit[validationState="info"],
QTextEdit[validationState="info"],
QPlainTextEdit[validationState="info"],
QComboBox[validationState="info"],
QSpinBox[validationState="info"],
QDoubleSpinBox[validationState="info"],
QAbstractSpinBox[validationState="info"],
QWidget[validationState="info"] {{
    background-color: {soft_info};
    border-color: {colors.info};
    color: {colors.text_primary};
}}
QPushButton,
QToolButton {{
    background-color: {colors.elevated};
    color: {colors.text_primary};
    border: 1px solid {colors.border};
    border-radius: {radius.md}px;
    min-height: {control.md}px;
    padding: {spacing.xs}px {spacing.md}px;
}}
QPushButton:hover,
QToolButton:hover {{
    background-color: #4a4a4a;
    border-color: {colors.focus};
}}
QPushButton:pressed,
QToolButton:pressed {{
    background-color: {colors.surface};
    border-color: {colors.primary};
    padding-top: {spacing.sm}px;
    padding-bottom: {spacing.xs}px;
}}
QPushButton:disabled,
QToolButton:disabled {{
    color: {colors.text_disabled};
    background-color: {colors.window};
    border-color: {colors.divider};
}}
QPushButton[role="primary"],
QToolButton[role="primary"],
QPushButton#startStopButton {{
    background-color: {colors.primary};
    border-color: {colors.primary};
    color: {colors.text_primary};
}}
QPushButton[role="primary"]:hover,
QToolButton[role="primary"]:hover,
QPushButton#startStopButton:hover {{
    background-color: {colors.hover};
    border-color: {colors.hover};
}}
QPushButton[role="primary"]:pressed,
QToolButton[role="primary"]:pressed,
QPushButton#startStopButton:pressed {{
    background-color: {colors.pressed};
    border-color: {colors.pressed};
}}
QPushButton[role="primary"]:disabled,
QToolButton[role="primary"]:disabled,
QPushButton#startStopButton:disabled,
QPushButton[role="destructive"]:disabled,
QToolButton[role="destructive"]:disabled {{
    color: {colors.text_disabled};
    background-color: {colors.window};
    border-color: {colors.divider};
}}
QPushButton[role="secondary"],
QToolButton[role="secondary"] {{
    background-color: {colors.surface};
    border-color: {colors.border};
    color: {colors.text_primary};
}}
QPushButton[role="secondary"]:hover,
QToolButton[role="secondary"]:hover {{
    background-color: {colors.elevated};
    border-color: {colors.focus};
}}
QPushButton[role="destructive"],
QToolButton[role="destructive"] {{
    background-color: {colors.error};
    border-color: {colors.error};
    color: {colors.window};
    font-weight: 650;
}}
QPushButton[role="destructive"]:hover,
QToolButton[role="destructive"]:hover {{
    background-color: {soft_error};
    border-color: {colors.error};
}}
QPushButton[role="quiet"],
QToolButton[role="quiet"] {{
    background-color: transparent;
    border-color: transparent;
    color: {colors.text_secondary};
}}
QPushButton[role="quiet"]:hover,
QToolButton[role="quiet"]:hover {{
    background-color: {colors.elevated};
    border-color: {colors.elevated};
    color: {colors.text_primary};
}}
QPushButton[role="secondary"]:disabled,
QToolButton[role="secondary"]:disabled,
QPushButton[role="quiet"]:disabled,
QToolButton[role="quiet"]:disabled {{
    color: {colors.text_disabled};
    background-color: {colors.window};
    border-color: {colors.divider};
}}
QPushButton[role="primary"]:focus,
QToolButton[role="primary"]:focus,
QPushButton[role="secondary"]:focus,
QToolButton[role="secondary"]:focus,
QPushButton[role="destructive"]:focus,
QToolButton[role="destructive"]:focus,
QPushButton[role="quiet"]:focus,
QToolButton[role="quiet"]:focus,
QPushButton#startStopButton:focus {{
    border: 2px solid {colors.focus};
}}
QFrame#navigationRail QToolButton {{
    text-align: left;
    padding: {spacing.md}px {spacing.lg}px;
    min-height: {control.lg}px;
    font-size: {typography.heading}pt;
    font-weight: 600;
    background-color: transparent;
    border: 1px solid transparent;
    color: {colors.text_secondary};
    border-radius: {radius.md}px;
}}
QFrame#navigationRail QToolButton:hover {{
    background-color: {colors.surface};
    color: {colors.text_primary};
}}
QFrame#navigationRail QToolButton:checked {{
    background-color: {colors.elevated};
    border-color: {colors.divider};
    border-left: 3px solid {colors.primary};
    color: {colors.text_primary};
}}
QFrame#navigationRail QToolButton:focus {{
    border-color: {colors.focus};
}}
QFrame#updateBanner {{
    background-color: {soft_info};
    border-color: {colors.info};
}}
QFrame#updateBanner QLabel {{
    color: {colors.text_primary};
}}
QFrame#updateBanner QPushButton,
QFrame#updateBanner QToolButton {{
    background-color: {colors.surface};
    border-color: {colors.border};
}}
QFrame#updateBanner QPushButton:hover,
QFrame#updateBanner QToolButton:hover {{
    background-color: {colors.elevated};
}}
QPushButton[chip="true"],
QToolButton[chip="true"] {{
    background-color: {colors.elevated};
    border: 1px solid {colors.divider};
    border-radius: {radius.pill}px;
    color: {colors.text_primary};
    min-height: {control.sm}px;
    padding: 2px {spacing.sm}px;
    font-size: {typography.small}pt;
}}
QPushButton[chip="true"]:hover,
QToolButton[chip="true"]:hover {{
    background-color: {colors.surface};
    border-color: {colors.focus};
}}
QPushButton[chip="true"]:checked,
QToolButton[chip="true"]:checked {{
    background-color: {colors.primary};
    border-color: {colors.primary};
}}
"""

    return base.strip()


def apply_theme(app: QApplication, theme: ThemeTokens = THEME) -> None:
    """Apply the semantic theme to a QApplication instance."""

    if not isinstance(app, QApplication):
        raise TypeError("apply_theme expects a QApplication instance")
    signature = repr(theme)
    if app.property("autoorthoThemeSignature") == signature:
        return

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if not font.family():
        font = app.font()
    font.setPointSize(theme.typography.base)
    app.setFont(font)

    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        try:
            fusion.setObjectName("Fusion")
        except Exception:
            pass
        app.setStyle(fusion)
        app.setProperty("autoorthoBaseStyle", "Fusion")
    else:
        app.setProperty("autoorthoBaseStyle", app.style().metaObject().className())

    app.setPalette(_palette(theme))
    app.setStyleSheet(build_stylesheet(theme))
    app.setProperty("autoorthoThemeSignature", signature)


def repolish(widget: QObject | None) -> None:
    """Reapply the current style after changing dynamic properties."""

    if widget is None:
        return

    style = widget.style() if hasattr(widget, "style") else None
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)

    if isinstance(widget, QWidget):
        for child in widget.findChildren(QWidget):
            child_style = child.style()
            if child_style is not None:
                child_style.unpolish(child)
                child_style.polish(child)
            child.update()

    if hasattr(widget, "update"):
        widget.update()


def announce_accessible(widget: QObject | None, description: str = "") -> None:
    """Publish an assistive-technology alert for important state changes."""
    if widget is None:
        return
    if description and hasattr(widget, "setAccessibleDescription"):
        widget.setAccessibleDescription(description)
    try:
        event = QAccessibleEvent(widget, QAccessible.Event.Alert)
        QAccessible.updateAccessibility(event)
    except Exception:
        pass


__all__ = [
    "THEME",
    "ColorTokens",
    "ControlHeightTokens",
    "RadiusTokens",
    "SpacingTokens",
    "ThemeTokens",
    "TypographyTokens",
    "apply_theme",
    "announce_accessible",
    "build_stylesheet",
    "contrast_ratio",
    "repolish",
    "validate_theme_tokens",
]
