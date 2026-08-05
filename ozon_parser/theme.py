"""Light/dark UI themes for the desktop app."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import SESSION_DIR

ThemeName = Literal["dark", "light"]
UI_SETTINGS_PATH = SESSION_DIR / "ui_settings.json"
DEFAULT_THEME: ThemeName = "dark"


@dataclass(frozen=True)
class ThemeColors:
    window_bg: str
    text: str
    text_muted: str
    text_soft: str
    title: str
    status_bg: str
    status_parsing_fg: str
    status_parsing_bg: str
    group_bg: str
    group_border: str
    group_title: str
    input_bg: str
    input_border: str
    input_text: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_soft_bg: str
    button_secondary_bg: str
    button_secondary_border: str
    button_secondary_hover: str
    button_disabled_bg: str
    button_disabled_fg: str
    button_disabled_border: str
    tree_bg: str
    tree_alt: str
    tree_hover: str
    tree_selected: str
    tree_border: str
    indicator_border: str
    link: str
    link_hover: str
    link_pressed: str
    checkbox_fg: str
    scrollbar_bg: str
    scrollbar_handle: str
    log_bg: str
    log_fg: str
    progress_bg: str
    progress_border: str
    placeholder: str


DARK = ThemeColors(
    window_bg="#12151c",
    text="#e8eaed",
    text_muted="#9aa0a6",
    text_soft="#6b7280",
    title="#ffffff",
    status_bg="#1e222b",
    status_parsing_fg="#7dd3fc",
    status_parsing_bg="#1a2836",
    group_bg="#1a1e27",
    group_border="#2d3340",
    group_title="#f3f4f6",
    input_bg="#0f1319",
    input_border="#3d4450",
    input_text="#e8eaed",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    accent_soft="#93c5fd",
    accent_soft_bg="#1e3a5f",
    button_secondary_bg="#2d3748",
    button_secondary_border="#4b5563",
    button_secondary_hover="#374151",
    button_disabled_bg="#2a3140",
    button_disabled_fg="#6b7280",
    button_disabled_border="#2d3340",
    tree_bg="#0f1319",
    tree_alt="#151922",
    tree_hover="#1f2937",
    tree_selected="#1e3a5f",
    tree_border="#3d4450",
    indicator_border="#ffffff",
    link="#60a5fa",
    link_hover="#93c5fd",
    link_pressed="#3b82f6",
    checkbox_fg="#d1d5db",
    scrollbar_bg="#1a1e27",
    scrollbar_handle="#4b5563",
    log_bg="#0f1319",
    log_fg="#cbd5e1",
    progress_bg="#0f1319",
    progress_border="#3d4450",
    placeholder="#6b7280",
)

LIGHT = ThemeColors(
    window_bg="#f4f6f9",
    text="#1f2937",
    text_muted="#6b7280",
    text_soft="#9ca3af",
    title="#111827",
    status_bg="#e8eef7",
    status_parsing_fg="#0369a1",
    status_parsing_bg="#e0f2fe",
    group_bg="#ffffff",
    group_border="#d7dde7",
    group_title="#111827",
    input_bg="#ffffff",
    input_border="#cfd6e1",
    input_text="#1f2937",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    accent_soft="#3b82f6",
    accent_soft_bg="#dbeafe",
    button_secondary_bg="#eef2f7",
    button_secondary_border="#cfd6e1",
    button_secondary_hover="#e2e8f0",
    button_disabled_bg="#e5e7eb",
    button_disabled_fg="#9ca3af",
    button_disabled_border="#d1d5db",
    tree_bg="#ffffff",
    tree_alt="#f8fafc",
    tree_hover="#eef2ff",
    tree_selected="#dbeafe",
    tree_border="#cfd6e1",
    indicator_border="#64748b",
    link="#2563eb",
    link_hover="#1d4ed8",
    link_pressed="#1e40af",
    checkbox_fg="#374151",
    scrollbar_bg="#eef2f7",
    scrollbar_handle="#94a3b8",
    log_bg="#ffffff",
    log_fg="#334155",
    progress_bg="#e8eef7",
    progress_border="#cfd6e1",
    placeholder="#9ca3af",
)

THEMES: dict[ThemeName, ThemeColors] = {
    "dark": DARK,
    "light": LIGHT,
}


def normalize_theme(value: object) -> ThemeName:
    if value == "light":
        return "light"
    return "dark"


def load_theme_preference() -> ThemeName:
    path = UI_SETTINGS_PATH
    if not path.exists():
        return DEFAULT_THEME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_THEME
    return normalize_theme(data.get("theme"))


def save_theme_preference(theme: ThemeName) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if UI_SETTINGS_PATH.exists():
        try:
            loaded = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["theme"] = normalize_theme(theme)
    UI_SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_stylesheet(
    theme: ThemeName,
    *,
    fs: int,
    btn_fs: int,
    cb_fs: int,
    btn_h: int,
    input_fs: int,
    tree_fs: int,
) -> str:
    c = THEMES[normalize_theme(theme)]
    return f"""
            QMainWindow, QWidget {{
                background: {c.window_bg};
                color: {c.text};
            }}
            QLabel#AppTitle {{
                font-size: {fs + 4}px;
                font-weight: 300;
                color: {c.title};
            }}
            QLabel#AppStatus {{
                font-size: {max(16, fs - 2)}px;
                font-weight: 300;
                color: {c.text_muted};
                padding: 6px 12px;
                border-radius: 6px;
                background: {c.status_bg};
            }}
            QLabel#AppStatus[parsing="true"] {{
                color: {c.status_parsing_fg};
                background: {c.status_parsing_bg};
            }}
            QLabel#DetailStatus {{
                color: {c.text_muted};
                font-size: {max(13, fs - 8)}px;
            }}
            QLabel#Footer {{
                color: {c.text_soft};
                font-size: {max(12, fs - 10)}px;
            }}
            QGroupBox {{
                font-size: {fs - 2}px;
                font-weight: 300;
                border: 1px solid {c.group_border};
                border-radius: 10px;
                margin-top: 14px;
                padding-top: 18px;
                background: {c.group_bg};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 8px;
                color: {c.group_title};
            }}
            QPushButton {{
                font-size: {btn_fs}px;
                font-weight: 300;
                background: {c.accent};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 10px 14px;
                min-height: {btn_h}px;
            }}
            QPushButton#SecondaryButton {{
                background: {c.button_secondary_bg};
                color: {c.text};
                border: 1px solid {c.button_secondary_border};
            }}
            QPushButton#SecondaryButton:hover {{
                background: {c.button_secondary_hover};
            }}
            QPushButton:hover {{
                background: {c.accent_hover};
            }}
            QPushButton:disabled {{
                background: {c.button_disabled_bg};
                color: {c.button_disabled_fg};
                border: 1px solid {c.button_disabled_border};
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                font-size: {input_fs}px;
                font-weight: 300;
                padding: 8px 12px;
                border: 1px solid {c.input_border};
                border-radius: 6px;
                background: {c.input_bg};
                color: {c.input_text};
                min-height: 36px;
            }}
            QComboBox QAbstractItemView {{
                background: {c.input_bg};
                color: {c.input_text};
                border: 1px solid {c.input_border};
                selection-background-color: {c.accent_soft_bg};
                selection-color: {c.text};
            }}
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
                border-color: {c.accent};
            }}
            QTreeWidget {{
                font-size: {tree_fs}px;
                font-weight: 300;
                border: 1px solid {c.tree_border};
                border-radius: 8px;
                background: {c.tree_bg};
                color: {c.input_text};
                alternate-background-color: {c.tree_alt};
                outline: 0;
            }}
            QTreeWidget#CategoryTree {{
                border: 1px solid {c.group_border};
                background: {c.window_bg};
                padding: 4px 2px;
                show-decoration-selected: 1;
            }}
            QTreeWidget#CategoryTree::item {{
                padding: 4px 6px 4px 2px;
                min-height: 26px;
                border-radius: 4px;
            }}
            QTreeWidget::item {{
                padding: 5px 2px;
                min-height: 28px;
                border-radius: 4px;
            }}
            QTreeWidget::item:hover {{
                background: {c.tree_hover};
            }}
            QTreeWidget::item:selected {{
                background: {c.tree_selected};
                color: {c.text};
            }}
            QTreeWidget::branch {{
                background: transparent;
            }}
            QTreeWidget::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid {c.indicator_border};
                background: transparent;
            }}
            QTreeWidget::indicator:hover {{
                border-color: {c.accent_soft};
            }}
            QTreeWidget::indicator:checked {{
                background: {c.accent};
                border-color: {c.accent};
                image: none;
            }}
            QTreeWidget::indicator:indeterminate {{
                background: {c.accent};
                border-color: {c.accent};
            }}
            QLabel#CatalogHint {{
                color: {c.text_muted};
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
            }}
            QLabel#SelectedCategoryCount {{
                color: {c.text};
                font-size: {max(13, fs - 7)}px;
                font-weight: 500;
                padding: 4px 8px;
                min-width: 96px;
            }}
            QLabel#ChromeModeHint {{
                color: {c.text_muted};
                font-size: {max(13, fs - 7)}px;
                font-weight: 400;
                padding: 2px 0 6px 0;
            }}
            QPushButton#LinkButton {{
                background: transparent;
                color: {c.link};
                border: none;
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
                padding: 4px 10px;
                min-height: 0;
            }}
            QPushButton#LinkButton:hover {{
                color: {c.link_hover};
                background: transparent;
                text-decoration: underline;
            }}
            QPushButton#LinkButton:pressed {{
                color: {c.link_pressed};
            }}
            QCheckBox {{
                font-size: {cb_fs}px;
                font-weight: 300;
                spacing: 8px;
                color: {c.checkbox_fg};
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid {c.indicator_border};
                background: transparent;
            }}
            QCheckBox::indicator:checked {{
                background: {c.accent};
                border-color: {c.accent};
            }}
            QCheckBox::indicator:disabled {{
                border-color: {c.text_soft};
                background: {c.button_disabled_bg};
            }}
            QLabel {{
                font-size: {tree_fs}px;
                font-weight: 300;
                color: {c.checkbox_fg};
            }}
            QScrollBar:vertical {{
                background: {c.scrollbar_bg};
                width: 10px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {c.scrollbar_handle};
                border-radius: 5px;
                min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QLabel#ProgressValue {{
                font-size: {fs + 8}px;
                font-weight: 300;
                color: {c.status_parsing_fg};
                padding: 8px 0;
            }}
            QPlainTextEdit#LogView {{
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
                font-family: Consolas, "Courier New", monospace;
                background: {c.log_bg};
                color: {c.log_fg};
                border: 1px solid {c.progress_border};
                border-radius: 8px;
                padding: 10px;
            }}
            QProgressBar {{
                background: {c.progress_bg};
                border: 1px solid {c.progress_border};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {c.accent};
                border-radius: 3px;
            }}
            """
