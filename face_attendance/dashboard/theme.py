"""Dashboard palette and stylesheet.

Two themes: **dark** (the original terminal-derived palette) and **light**
(the modern dashboard design).  The light theme matches the SV Technologies
mockup: blue primary, green success, orange warning on a cool-gray canvas.
"""

PALETTES = {
    "dark": {
        "bg": "#0F172A",
        "panel": "#1E293B",
        "panel_alt": "#1E293B",
        "card": "#1E293B",
        "border": "#334155",
        "text": "#F1F5F9",
        "text_secondary": "#94A3B8",
        "muted": "#64748B",
        "primary": "#3B82F6",
        "primary_hover": "#2563EB",
        "primary_fg": "#FFFFFF",
        "accent": "#3B82F6",
        "success": "#22C55E",
        "success_bg": "#052E16",
        "danger": "#EF4444",
        "danger_bg": "#450A0A",
        "warn": "#F59E0B",
        "warn_bg": "#451A03",
        "info": "#3B82F6",
        "info_bg": "#172554",
        "video_bg": "#020617",
        "selection": "#1E3A5F",
        "shadow": "rgba(0,0,0,0.4)",
        "sidebar_bg": "#1E293B",
        "sidebar_text": "#94A3B8",
        "sidebar_active_bg": "#3B82F6",
        "sidebar_active_text": "#FFFFFF",
        "sidebar_hover_bg": "#334155",
        "header_bg": "#1E293B",
        "header_border": "#334155",
        "badge_bg": "#EF4444",
        "badge_fg": "#FFFFFF",
        "nav_icon": "#64748B",
        "nav_icon_active": "#FFFFFF",
        "input_bg": "#0F172A",
        "input_border": "#334155",
        "log_bg": "#0F172A",
        "log_border": "#334155",
        "log_tag_info": "#3B82F6",
        "log_tag_debug": "#8B5CF6",
        "log_tag_warn": "#F59E0B",
        "log_tag_error": "#EF4444",
        "online": "#22C55E",
        "stat_icon_blue": "#3B82F6",
        "stat_icon_blue_bg": "#172554",
        "stat_icon_green": "#22C55E",
        "stat_icon_green_bg": "#052E16",
        "stat_icon_orange": "#F59E0B",
        "stat_icon_orange_bg": "#451A03",
        "stat_icon_purple": "#8B5CF6",
        "stat_icon_purple_bg": "#2E1065",
    },
    "light": {
        "bg": "#F1F5F9",
        "panel": "#FFFFFF",
        "panel_alt": "#F8FAFC",
        "card": "#FFFFFF",
        "border": "#E2E8F0",
        "text": "#1E293B",
        "text_secondary": "#475569",
        "muted": "#64748B",
        "primary": "#2563EB",
        "primary_hover": "#1D4ED8",
        "primary_fg": "#FFFFFF",
        "accent": "#2563EB",
        "success": "#16A34A",
        "success_bg": "#F0FDF4",
        "danger": "#DC2626",
        "danger_bg": "#FEF2F2",
        "warn": "#D97706",
        "warn_bg": "#FFFBEB",
        "info": "#2563EB",
        "info_bg": "#EFF6FF",
        "video_bg": "#1E293B",
        "selection": "#DBEAFE",
        "shadow": "rgba(15,23,42,0.08)",
        "sidebar_bg": "#FFFFFF",
        "sidebar_text": "#64748B",
        "sidebar_active_bg": "#2563EB",
        "sidebar_active_text": "#FFFFFF",
        "sidebar_hover_bg": "#F1F5F9",
        "header_bg": "#FFFFFF",
        "header_border": "#E2E8F0",
        "badge_bg": "#EF4444",
        "badge_fg": "#FFFFFF",
        "nav_icon": "#94A3B8",
        "nav_icon_active": "#FFFFFF",
        "input_bg": "#F8FAFC",
        "input_border": "#E2E8F0",
        "log_bg": "#F8FAFC",
        "log_border": "#E2E8F0",
        "log_tag_info": "#2563EB",
        "log_tag_debug": "#7C3AED",
        "log_tag_warn": "#D97706",
        "log_tag_error": "#DC2626",
        "online": "#16A34A",
        "stat_icon_blue": "#2563EB",
        "stat_icon_blue_bg": "#DBEAFE",
        "stat_icon_green": "#16A34A",
        "stat_icon_green_bg": "#DCFCE7",
        "stat_icon_orange": "#D97706",
        "stat_icon_orange_bg": "#FEF3C7",
        "stat_icon_purple": "#7C3AED",
        "stat_icon_purple_bg": "#EDE9FE",
    },
}

TONE_COLORS = {"ok": "success", "warn": "warn", "bad": "danger", "idle": "muted", "info": "info"}


def stylesheet(theme="light"):
    c = palette(theme)
    return f"""
/* === Base === */
QWidget {{
    background-color: {c['bg']};
    color: {c['text']};
    font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}}
QDialog, QMainWindow, QScrollArea, QStackedWidget, QSplitter {{
    background-color: {c['bg']};
}}
QLabel {{ background: transparent; }}

/* === Typography === */
QLabel#appTitle {{
    font-size: 18px; font-weight: 700; color: {c['text']};
}}
QLabel#appSubtitle {{
    font-size: 13px; color: {c['text_secondary']};
}}
QLabel#screenTitle {{
    font-size: 20px; font-weight: 700; color: {c['text']};
}}
QLabel#screenSubtitle, QLabel#statHint, QLabel#muted, QLabel#emptyBody {{
    color: {c['muted']}; font-size: 12px;
}}
QLabel#sectionTitle {{
    font-size: 14px; font-weight: 600; color: {c['text']};
}}
QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#welcomeText {{
    font-size: 18px; font-weight: 700; color: {c['text']};
}}
QLabel#welcomeSub {{
    font-size: 13px; color: {c['text_secondary']};
}}
QLabel#headerClock {{
    font-size: 24px; font-weight: 700; color: {c['text']};
}}
QLabel#headerDate {{
    font-size: 12px; color: {c['muted']};
}}
QLabel#versionLabel {{
    font-size: 11px; color: {c['muted']};
}}

/* === Cards === */
QFrame#card, QFrame#statCard {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QFrame#panel {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}

/* === Stat Cards === */
QLabel#statTitle {{
    color: {c['muted']}; font-size: 12px; font-weight: 500;
}}
QLabel#statValue {{
    font-size: 28px; font-weight: 700; color: {c['text']};
}}
QLabel#statValue[tone="ok"] {{ color: {c['success']}; }}
QLabel#statValue[tone="warn"] {{ color: {c['warn']}; }}
QLabel#statValue[tone="bad"] {{ color: {c['danger']}; }}
QLabel#statValue[tone="idle"] {{ color: {c['text']}; }}
QLabel#statSubtext {{
    font-size: 11px; color: {c['muted']};
}}
QLabel#statIcon {{
    border-radius: 10px; font-size: 18px;
}}

/* === Toast === */
QFrame#toast {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-left: 4px solid {c['info']};
    border-radius: 8px;
}}
QFrame#toast[tone="ok"] {{ border-left-color: {c['success']}; }}
QFrame#toast[tone="bad"] {{ border-left-color: {c['danger']}; }}
QFrame#toast[tone="warn"] {{ border-left-color: {c['warn']}; }}

/* === Status Pills === */
QFrame#pill {{
    border-radius: 10px;
    border: 1px solid {c['border']};
    background-color: {c['panel_alt']};
    padding: 2px 8px;
}}
QLabel#pillText {{ font-size: 11px; font-weight: 600; }}
QFrame#pill[tone="ok"] {{ border-color: {c['success']}; background-color: {c['success_bg']}; }}
QFrame#pill[tone="ok"] QLabel#pillText {{ color: {c['success']}; }}
QFrame#pill[tone="warn"] {{ border-color: {c['warn']}; background-color: {c['warn_bg']}; }}
QFrame#pill[tone="warn"] QLabel#pillText {{ color: {c['warn']}; }}
QFrame#pill[tone="bad"] {{ border-color: {c['danger']}; background-color: {c['danger_bg']}; }}
QFrame#pill[tone="bad"] QLabel#pillText {{ color: {c['danger']}; }}
QFrame#pill[tone="idle"] QLabel#pillText {{ color: {c['muted']}; }}
QFrame#pill[tone="info"] {{ border-color: {c['info']}; background-color: {c['info_bg']}; }}
QFrame#pill[tone="info"] QLabel#pillText {{ color: {c['info']}; }}

/* === Buttons === */
QPushButton {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
    color: {c['text']};
}}
QPushButton:hover {{
    border-color: {c['primary']};
    background-color: {c['panel_alt']};
}}
QPushButton:pressed {{ background-color: {c['selection']}; }}
QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
QPushButton#primary {{
    background-color: {c['primary']};
    color: {c['primary_fg']};
    border: none;
    font-weight: 600;
    border-radius: 8px;
    padding: 9px 20px;
}}
QPushButton#primary:hover {{ background-color: {c['primary_hover']}; }}
QPushButton#success {{
    background-color: {c['success']};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
    border-radius: 8px;
    padding: 9px 20px;
}}
QPushButton#danger {{ border-color: {c['danger']}; color: {c['danger']}; }}
QPushButton#danger:hover {{ background-color: {c['danger_bg']}; }}
QPushButton#iconButton {{
    background: transparent;
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px 10px;
}}
QPushButton#iconButton:hover {{ background-color: {c['panel_alt']}; }}

/* === Inputs === */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QPlainTextEdit {{
    background-color: {c['input_bg']};
    border: 1px solid {c['input_border']};
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: {c['primary']};
    selection-color: {c['primary_fg']};
    color: {c['text']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QTimeEdit:focus {{ border-color: {c['primary']}; }}
QComboBox QAbstractItemView {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QComboBox::drop-down {{
    border: none; width: 24px;
}}

/* === Tables === */
QTableView, QTableWidget, QListWidget, QTreeWidget {{
    background-color: {c['card']};
    alternate-background-color: {c['panel_alt']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    gridline-color: {c['border']};
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QHeaderView::section {{
    background-color: {c['panel_alt']};
    border: none;
    border-right: 1px solid {c['border']};
    border-bottom: 1px solid {c['border']};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 12px;
    color: {c['muted']};
}}
QTableCornerButton::section {{
    background-color: {c['panel_alt']}; border: none;
}}

/* === Navigation === */
QListWidget#nav {{
    border: none;
    border-radius: 0px;
    background-color: {c['sidebar_bg']};
    padding: 8px 10px;
    border-right: 1px solid {c['border']};
}}
QListWidget#nav::item {{
    padding: 11px 14px;
    border-radius: 10px;
    margin: 2px 0px;
    color: {c['sidebar_text']};
    font-weight: 500;
    font-size: 13px;
}}
QListWidget#nav::item:selected {{
    background-color: {c['sidebar_active_bg']};
    color: {c['sidebar_active_text']};
    font-weight: 600;
}}
QListWidget#nav::item:hover:!selected {{
    background-color: {c['sidebar_hover_bg']};
    color: {c['text']};
}}

/* === Tabs === */
QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: 10px;
    background-color: {c['card']};
}}
QTabBar::tab {{
    background: transparent;
    padding: 8px 18px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    color: {c['muted']};
    font-weight: 500;
}}
QTabBar::tab:selected {{
    background-color: {c['card']};
    color: {c['primary']};
    font-weight: 600;
}}

/* === Groups === */
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    background-color: {c['card']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {c['text']};
    font-weight: 600;
}}

/* === Progress Bar === */
QProgressBar {{
    border: 1px solid {c['border']};
    border-radius: 6px;
    background-color: {c['panel_alt']};
    text-align: center;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {c['primary']};
    border-radius: 5px;
}}

/* === Scrollbars === */
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['border']}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
QScrollBar:horizontal {{
    background: transparent; height: 8px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c['border']}; border-radius: 4px; min-width: 30px;
}}

/* === Status Bar === */
QStatusBar {{
    background-color: {c['sidebar_bg']};
    border-top: 1px solid {c['border']};
    color: {c['muted']};
    font-size: 12px;
    padding: 2px 12px;
}}

/* === Tooltips === */
QToolTip {{
    background-color: {c['card']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px 8px;
}}

/* === Checkboxes / Radio === */
QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
QSplitter::handle {{ background-color: {c['border']}; }}

/* === Header === */
QFrame#headerBar {{
    background-color: {c['header_bg']};
    border-bottom: 1px solid {c['header_border']};
}}
QFrame#sidebarFooter {{
    background-color: {c['sidebar_bg']};
    border-top: 1px solid {c['border']};
    border-right: 1px solid {c['border']};
}}

/* === System Status Panel === */
QFrame#statusPanel {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QLabel#statusRunning {{
    font-size: 16px; font-weight: 700; color: {c['success']};
}}
QLabel#statusStopped {{
    font-size: 16px; font-weight: 700; color: {c['danger']};
}}
QLabel#statusItem {{
    font-size: 12px; color: {c['text_secondary']};
}}
QLabel#statusDot {{
    font-size: 10px;
}}

/* === Recent Activity === */
QFrame#activityItem {{
    background-color: {c['card']};
    border-bottom: 1px solid {c['border']};
}}
QLabel#activityName {{
    font-size: 13px; font-weight: 600; color: {c['text']};
}}
QLabel#activityDetail {{
    font-size: 11px; color: {c['muted']};
}}
QLabel#activityTime {{
    font-size: 12px; color: {c['muted']};
}}
QFrame#verifiedBadge {{
    background-color: {c['success_bg']};
    border: 1px solid {c['success']};
    border-radius: 4px;
}}
QLabel#verifiedText {{
    color: {c['success']}; font-size: 10px; font-weight: 700;
}}
QFrame#unknownBadge {{
    background-color: {c['danger_bg']};
    border: 1px solid {c['danger']};
    border-radius: 4px;
}}
QLabel#unknownText {{
    color: {c['danger']}; font-size: 10px; font-weight: 700;
}}

/* === System Logs === */
QFrame#logPanel {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QLabel#logTag {{
    font-size: 10px; font-weight: 700; border-radius: 3px;
    padding: 1px 6px;
}}
QLabel#logTimestamp {{
    font-size: 12px; color: {c['muted']}; font-family: "SF Mono", "Consolas", monospace;
}}
QLabel#logMessage {{
    font-size: 12px; color: {c['text']}; font-family: "SF Mono", "Consolas", monospace;
}}

/* === Camera Preview === */
QFrame#cameraFrame {{
    background-color: {c['video_bg']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QLabel#liveBadge {{
    background-color: {c['success']};
    color: #FFFFFF;
    font-size: 10px;
    font-weight: 700;
    border-radius: 4px;
    padding: 2px 8px;
}}
QLabel#cameraInfo {{
    font-size: 12px; color: {c['muted']};
}}
QLabel#faceBadge {{
    background-color: rgba(0,0,0,0.6);
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 600;
    border-radius: 8px;
    padding: 4px 12px;
}}
QLabel#fpsOverlay {{
    font-size: 11px;
    color: rgba(255,255,255,0.8);
    background-color: rgba(0,0,0,0.5);
    border-radius: 6px;
    padding: 3px 8px;
}}
"""


def palette(theme="light"):
    return PALETTES.get(theme, PALETTES["light"])
