"""Dashboard palette and stylesheet.

Two themes: **dark** (terminal-derived) and **light** (the modern face-ID
dashboard design: blue primary, green success, orange warning on a cool gray
canvas).  Every colour the screens need lives in ``PALETTES`` so a widget can
look a token up at paint time instead of hard-coding a hex value.
"""

PALETTES = {
    "dark": {
        # surfaces
        "bg": "#0B1220",
        "bg_soft": "#111C33",
        "panel": "#152238",
        "panel_alt": "#1B2A44",
        "card": "#152238",
        "card_alt": "#1B2A44",
        "border": "#26395C",
        "border_soft": "#1E2E4C",
        "hover": "#1E2E4C",
        "selection": "#1D3A66",
        "ring": "rgba(59,130,246,0.45)",
        "shadow": "rgba(0,0,0,0.45)",
        # text
        "text": "#F1F5F9",
        "text_secondary": "#B6C2D6",
        "muted": "#7C8DA8",
        # accents
        "primary": "#3B82F6",
        "primary_hover": "#2563EB",
        "primary_fg": "#FFFFFF",
        "primary_soft": "#16294A",
        "primary_soft_fg": "#93C5FD",
        "accent": "#3B82F6",
        "success": "#22C55E",
        "success_bg": "#0B2E1C",
        "success_border": "#1B5E37",
        "danger": "#F87171",
        "danger_bg": "#3A1214",
        "danger_border": "#7F2A2E",
        "warn": "#FBBF24",
        "warn_bg": "#3A2A08",
        "warn_border": "#7A5A13",
        "info": "#60A5FA",
        "info_bg": "#152C4F",
        "info_border": "#274F86",
        "online": "#22C55E",
        # video canvas
        "video_bg": "#070C17",
        "video_bg_2": "#101B2E",
        "bracket": "#22C55E",
        # chrome
        "sidebar_bg": "#0F1B2E",
        "sidebar_text": "#93A3BC",
        "sidebar_active_bg": "#3B82F6",
        "sidebar_active_text": "#FFFFFF",
        "sidebar_active_soft": "#16294A",
        "sidebar_hover_bg": "#1B2A44",
        "header_bg": "#0F1B2E",
        "header_border": "#26395C",
        "chip_bg": "#1B2A44",
        "chip_fg": "#B6C2D6",
        "badge_bg": "#EF4444",
        "badge_fg": "#FFFFFF",
        "nav_icon": "#7C8DA8",
        "nav_icon_active": "#FFFFFF",
        "nav_section": "#5D708F",
        "avatar_bg": "#1D4ED8",
        "avatar_fg": "#FFFFFF",
        # inputs & logs
        "input_bg": "#111C33",
        "input_border": "#26395C",
        "log_bg": "#101A2C",
        "log_border": "#1E2E4C",
        "log_tag_info": "#3B82F6",
        "log_tag_debug": "#8B5CF6",
        "log_tag_warn": "#F59E0B",
        "log_tag_error": "#EF4444",
        "table_header_bg": "#1B2A44",
        "row_hover": "#1B2A44",
        # stat icon tiles
        "stat_icon_blue": "#60A5FA",
        "stat_icon_blue_bg": "#16294A",
        "stat_icon_green": "#4ADE80",
        "stat_icon_green_bg": "#0B2E1C",
        "stat_icon_orange": "#FBBF24",
        "stat_icon_orange_bg": "#3A2A08",
        "stat_icon_purple": "#A78BFA",
        "stat_icon_purple_bg": "#26184A",
        "stat_icon_red": "#F87171",
        "stat_icon_red_bg": "#3A1214",
    },
    "light": {
        # surfaces
        "bg": "#F4F6FB",
        "bg_soft": "#EDF1F8",
        "panel": "#FFFFFF",
        "panel_alt": "#F8FAFC",
        "card": "#FFFFFF",
        "card_alt": "#FBFCFE",
        "border": "#E5EAF2",
        "border_soft": "#EFF3F8",
        "hover": "#F3F6FC",
        "selection": "#DBEAFE",
        "ring": "rgba(37,99,235,0.35)",
        "shadow": "rgba(15,23,42,0.06)",
        # text
        "text": "#0F172A",
        "text_secondary": "#475569",
        "muted": "#7A8AA3",
        # accents
        "primary": "#2563EB",
        "primary_hover": "#1D4ED8",
        "primary_fg": "#FFFFFF",
        "primary_soft": "#EAF1FE",
        "primary_soft_fg": "#1D4ED8",
        "accent": "#2563EB",
        "success": "#16A34A",
        "success_bg": "#ECFDF3",
        "success_border": "#BBEFC9",
        "danger": "#DC2626",
        "danger_bg": "#FEF2F2",
        "danger_border": "#FDCFCF",
        "warn": "#D97706",
        "warn_bg": "#FFF8EB",
        "warn_border": "#FBE0B0",
        "info": "#2563EB",
        "info_bg": "#EFF6FF",
        "info_border": "#C7DBFE",
        "online": "#16A34A",
        # video canvas
        "video_bg": "#0B1220",
        "video_bg_2": "#17233A",
        "bracket": "#22C55E",
        # chrome
        "sidebar_bg": "#FFFFFF",
        "sidebar_text": "#64748B",
        "sidebar_active_bg": "#2563EB",
        "sidebar_active_text": "#FFFFFF",
        "sidebar_active_soft": "#EAF1FE",
        "sidebar_hover_bg": "#F4F7FD",
        "header_bg": "#FFFFFF",
        "header_border": "#E9EDF4",
        "chip_bg": "#F1F5F9",
        "chip_fg": "#475569",
        "badge_bg": "#EF4444",
        "badge_fg": "#FFFFFF",
        "nav_icon": "#94A3B8",
        "nav_icon_active": "#FFFFFF",
        "nav_section": "#9AA7BC",
        "avatar_bg": "#DBEAFE",
        "avatar_fg": "#1D4ED8",
        # inputs & logs
        "input_bg": "#F8FAFC",
        "input_border": "#E5EAF2",
        "log_bg": "#F8FAFC",
        "log_border": "#EFF3F8",
        "log_tag_info": "#2563EB",
        "log_tag_debug": "#7C3AED",
        "log_tag_warn": "#D97706",
        "log_tag_error": "#DC2626",
        "table_header_bg": "#F8FAFC",
        "row_hover": "#F7FAFF",
        # stat icon tiles
        "stat_icon_blue": "#2563EB",
        "stat_icon_blue_bg": "#E6EEFE",
        "stat_icon_green": "#16A34A",
        "stat_icon_green_bg": "#E4F8EC",
        "stat_icon_orange": "#D97706",
        "stat_icon_orange_bg": "#FEF3E2",
        "stat_icon_purple": "#7C3AED",
        "stat_icon_purple_bg": "#F0EAFE",
        "stat_icon_red": "#DC2626",
        "stat_icon_red_bg": "#FDECEC",
    },
}

TONE_COLORS = {"ok": "success", "warn": "warn", "bad": "danger", "idle": "muted", "info": "info"}

STAT_TONES = ("blue", "green", "orange", "purple", "red")


def stylesheet(theme="light"):
    c = palette(theme)
    return f"""
/* === Base =============================================================== */
QWidget {{
    background-color: {c['bg']};
    color: {c['text']};
    font-family: "Inter", "SF Pro Display", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}}
QDialog, QMainWindow, QScrollArea, QStackedWidget, QSplitter {{
    background-color: {c['bg']};
}}
QLabel {{ background: transparent; }}
QToolTip {{
    background-color: {c['text']};
    color: {c['card']};
    border: none;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
}}

/* === Typography ========================================================= */
QLabel#appTitle {{ font-size: 16px; font-weight: 700; color: {c['text']}; }}
QLabel#appSubtitle {{ font-size: 12px; color: {c['text_secondary']}; }}
QLabel#brandMark {{ font-size: 11px; color: {c['muted']}; letter-spacing: 1px; }}
QLabel#onlineStatus {{ font-size: 12px; font-weight: 600; color: {c['online']}; }}
QLabel#onlineStatus[offline="true"] {{ color: {c['danger']}; }}
QLabel#pageTitle, QLabel#screenTitle {{
    font-size: 22px; font-weight: 700; color: {c['text']};
}}
QLabel#pageSubtitle, QLabel#screenSubtitle, QLabel#muted, QLabel#statHint,
QLabel#emptyBody, QLabel#fieldNote, QLabel#cardSubtitle {{
    color: {c['muted']}; font-size: 12px;
}}
QLabel#sectionTitle, QLabel#cardTitle {{
    font-size: 14px; font-weight: 600; color: {c['text']};
}}
QLabel#cardEyebrow {{
    font-size: 11px; font-weight: 700; color: {c['muted']}; letter-spacing: 1px;
}}
QLabel#emptyTitle {{ font-size: 15px; font-weight: 600; color: {c['text']}; }}
QLabel#welcomeText {{ font-size: 17px; font-weight: 700; color: {c['text']}; }}
QLabel#welcomeSub {{ font-size: 12px; color: {c['muted']}; }}
QLabel#headerClock {{
    font-size: 20px; font-weight: 700; color: {c['text']};
    font-family: "SF Mono", "JetBrains Mono", "Consolas", monospace;
}}
QLabel#headerDate {{ font-size: 11px; color: {c['muted']}; }}
QLabel#versionLabel {{ font-size: 11px; color: {c['muted']}; }}
QLabel#legalLabel {{ font-size: 10px; color: {c['muted']}; }}

/* === Cards ============================================================== */
QFrame#card, QFrame#statCard, QFrame#panel, QFrame#formCard, QFrame#tableCard {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 14px;
}}
QFrame#card[tone="ok"], QFrame#panel[tone="ok"] {{ border-color: {c['success_border']}; }}
QFrame#card[tone="bad"], QFrame#panel[tone="bad"] {{ border-color: {c['danger_border']}; }}
QFrame#card[tone="warn"], QFrame#panel[tone="warn"] {{ border-color: {c['warn_border']}; }}
QFrame#cardDivider, QFrame#hLine {{
    background-color: {c['border_soft']};
    border: none;
    max-height: 1px;
}}
QFrame#cardHeader {{ background: transparent; }}
QFrame#subtlePanel {{
    background-color: {c['panel_alt']};
    border: 1px solid {c['border_soft']};
    border-radius: 12px;
}}

/* === Stat cards ========================================================= */
QLabel#statTitle {{ color: {c['muted']}; font-size: 12px; font-weight: 500; }}
QLabel#statValue {{ font-size: 24px; font-weight: 700; color: {c['text']}; }}
QLabel#statValue[tone="ok"] {{ color: {c['success']}; }}
QLabel#statValue[tone="warn"] {{ color: {c['warn']}; }}
QLabel#statValue[tone="bad"] {{ color: {c['danger']}; }}
QLabel#statValue[tone="idle"] {{ color: {c['text']}; }}
QLabel#statSubtext {{ font-size: 11px; color: {c['muted']}; }}
QLabel#statIcon {{ border-radius: 12px; font-size: 18px; }}
QLabel#statDelta {{ font-size: 11px; font-weight: 600; }}
QLabel#statDelta[tone="ok"] {{ color: {c['success']}; }}
QLabel#statDelta[tone="bad"] {{ color: {c['danger']}; }}
QLabel#statDelta[tone="idle"] {{ color: {c['muted']}; }}

/* === Buttons ============================================================ */
QPushButton {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 8px 16px;
    font-weight: 500;
    color: {c['text_secondary']};
    min-height: 20px;
}}
QPushButton:hover {{
    border-color: {c['primary']};
    color: {c['primary']};
    background-color: {c['card']};
}}
QPushButton:pressed {{ background-color: {c['primary_soft']}; }}
QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border_soft']}; background-color: {c['panel_alt']}; }}
QPushButton#primary {{
    background-color: {c['primary']};
    color: {c['primary_fg']};
    border: 1px solid {c['primary']};
    font-weight: 600;
    padding: 9px 20px;
}}
QPushButton#primary:hover {{
    background-color: {c['primary_hover']};
    border-color: {c['primary_hover']};
    color: {c['primary_fg']};
}}
QPushButton#success {{
    background-color: {c['success']};
    color: #FFFFFF;
    border: 1px solid {c['success']};
    font-weight: 600;
    padding: 9px 20px;
}}
QPushButton#success:hover {{ background-color: {c['success']}; color: #FFFFFF; border-color: {c['success']}; }}
QPushButton#danger {{
    background-color: {c['danger_bg']};
    border: 1px solid {c['danger_border']};
    color: {c['danger']};
    font-weight: 600;
    padding: 9px 18px;
}}
QPushButton#danger:hover {{ background-color: {c['danger']}; color: #FFFFFF; border-color: {c['danger']}; }}
QPushButton#softButton {{
    background-color: {c['primary_soft']};
    border: 1px solid transparent;
    color: {c['primary_soft_fg']};
    font-weight: 600;
}}
QPushButton#softButton:hover {{ background-color: {c['primary']}; color: {c['primary_fg']}; }}
QPushButton#controlButton {{
    padding: 7px 12px;
    font-size: 12px;
    font-weight: 600;
    min-width: 0px;
    min-height: 18px;
}}
QPushButton#controlButtonSuccess {{
    background-color: {c['success']};
    color: #FFFFFF;
    border: 1px solid {c['success']};
    font-weight: 600;
    padding: 7px 14px;
    font-size: 12px;
    min-width: 0px;
    min-height: 18px;
}}
QPushButton#controlButtonSuccess:hover {{
    background-color: {c['success']};
    border-color: {c['success']};
    color: #FFFFFF;
}}
QPushButton#controlButtonDanger {{
    background-color: {c['danger_bg']};
    border: 1px solid {c['danger_border']};
    color: {c['danger']};
    font-weight: 600;
    padding: 7px 14px;
    font-size: 12px;
    min-width: 0px;
    min-height: 18px;
}}
QPushButton#controlButtonDanger:hover {{
    background-color: {c['danger']};
    border-color: {c['danger']};
    color: #FFFFFF;
}}
QPushButton#ghostButton {{
    background: transparent;
    border: 1px solid transparent;
    color: {c['text_secondary']};
    padding: 6px 10px;
}}
QPushButton#ghostButton:hover {{ background-color: {c['hover']}; color: {c['primary']}; }}
QPushButton#linkButton {{
    background: transparent;
    border: none;
    color: {c['primary']};
    font-weight: 600;
    padding: 2px 4px;
}}
QPushButton#linkButton:hover {{ color: {c['primary_hover']}; }}
QPushButton#iconButton {{
    background: transparent;
    border: 1px solid {c['border']};
    border-radius: 9px;
    padding: 6px;
    min-width: 20px;
}}
QPushButton#iconButton:hover {{ background-color: {c['hover']}; border-color: {c['primary']}; }}
QPushButton#iconButtonFlat {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 6px;
    min-width: 20px;
}}
QPushButton#iconButtonFlat:hover {{ background-color: {c['hover']}; }}

/* === Inputs ============================================================= */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QPlainTextEdit {{
    background-color: {c['input_bg']};
    border: 1px solid {c['input_border']};
    border-radius: 10px;
    padding: 8px 12px;
    selection-background-color: {c['primary']};
    selection-color: {c['primary_fg']};
    color: {c['text']};
    min-height: 18px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QTimeEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {c['primary']};
    background-color: {c['card']};
}}
QLineEdit::placeholder {{ color: {c['muted']}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; width: 0px; }}
QComboBox QAbstractItemView {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 4px;
    outline: none;
    selection-background-color: {c['primary_soft']};
    selection-color: {c['primary_soft_fg']};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent; border: none; width: 16px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow,
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ width: 0px; height: 0px; }}

/* === Checkboxes ========================================================= */
QCheckBox, QRadioButton {{ background: transparent; spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator {{
    border: 1px solid {c['input_border']};
    border-radius: 5px;
    background-color: {c['card']};
}}
QCheckBox::indicator:hover {{ border-color: {c['primary']}; }}
QCheckBox::indicator:checked {{
    background-color: {c['primary']};
    border-color: {c['primary']};
}}
QRadioButton::indicator {{
    border: 1px solid {c['input_border']};
    border-radius: 8px;
    background-color: {c['card']};
}}
QRadioButton::indicator:checked {{ border: 5px solid {c['primary']}; }}

/* === Chips & pills ====================================================== */
QFrame#pill {{
    border-radius: 11px;
    border: 1px solid {c['border']};
    background-color: {c['chip_bg']};
    padding: 2px 10px;
}}
QLabel#pillText {{ font-size: 11px; font-weight: 600; color: {c['chip_fg']}; }}
QFrame#pill[tone="ok"] {{ border-color: {c['success_border']}; background-color: {c['success_bg']}; }}
QFrame#pill[tone="ok"] QLabel#pillText {{ color: {c['success']}; }}
QFrame#pill[tone="warn"] {{ border-color: {c['warn_border']}; background-color: {c['warn_bg']}; }}
QFrame#pill[tone="warn"] QLabel#pillText {{ color: {c['warn']}; }}
QFrame#pill[tone="bad"] {{ border-color: {c['danger_border']}; background-color: {c['danger_bg']}; }}
QFrame#pill[tone="bad"] QLabel#pillText {{ color: {c['danger']}; }}
QFrame#pill[tone="info"] {{ border-color: {c['info_border']}; background-color: {c['info_bg']}; }}
QFrame#pill[tone="info"] QLabel#pillText {{ color: {c['info']}; }}
QFrame#pill[tone="idle"] QLabel#pillText {{ color: {c['muted']}; }}
QLabel#chip {{
    background-color: {c['chip_bg']};
    color: {c['chip_fg']};
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#chip[tone="ok"] {{ background-color: {c['success_bg']}; color: {c['success']}; }}
QLabel#chip[tone="warn"] {{ background-color: {c['warn_bg']}; color: {c['warn']}; }}
QLabel#chip[tone="bad"] {{ background-color: {c['danger_bg']}; color: {c['danger']}; }}
QLabel#chip[tone="info"] {{ background-color: {c['info_bg']}; color: {c['info']}; }}
QFrame#verifiedBadge {{ background-color: {c['success_bg']}; border-radius: 8px; }}
QLabel#verifiedText {{ color: {c['success']}; font-size: 10px; font-weight: 700; }}
QFrame#unknownBadge {{ background-color: {c['warn_bg']}; border-radius: 8px; }}
QLabel#unknownText {{ color: {c['warn']}; font-size: 10px; font-weight: 700; }}
QLabel#badge {{
    background-color: {c['badge_bg']};
    color: {c['badge_fg']};
    border-radius: 8px;
    font-size: 10px;
    font-weight: 700;
    padding: 1px 5px;
}}

/* === Banners ============================================================ */
QFrame#banner {{
    background-color: {c['info_bg']};
    border: 1px solid {c['info_border']};
    border-radius: 12px;
}}
QFrame#banner[tone="ok"] {{ background-color: {c['success_bg']}; border-color: {c['success_border']}; }}
QFrame#banner[tone="warn"] {{ background-color: {c['warn_bg']}; border-color: {c['warn_border']}; }}
QFrame#banner[tone="bad"] {{ background-color: {c['danger_bg']}; border-color: {c['danger_border']}; }}
QFrame#banner[tone="idle"] {{ background-color: {c['panel_alt']}; border-color: {c['border']}; }}
QLabel#bannerTitle {{ font-size: 14px; font-weight: 700; color: {c['info']}; }}
QFrame#banner[tone="ok"] QLabel#bannerTitle {{ color: {c['success']}; }}
QFrame#banner[tone="warn"] QLabel#bannerTitle {{ color: {c['warn']}; }}
QFrame#banner[tone="bad"] QLabel#bannerTitle {{ color: {c['danger']}; }}
QFrame#banner[tone="idle"] QLabel#bannerTitle {{ color: {c['text']}; }}
QLabel#bannerBody {{ font-size: 12px; color: {c['text_secondary']}; }}

/* === Tables & lists ===================================================== */
QTableView, QTableWidget, QTreeWidget {{
    background-color: {c['card']};
    alternate-background-color: {c['card_alt']};
    border: none;
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: {c['primary_soft']};
    selection-color: {c['text']};
    outline: none;
}}
QListWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QTableView::item, QTableWidget::item {{ padding: 4px 8px; }}
QHeaderView::section {{
    background-color: {c['table_header_bg']};
    border: none;
    border-bottom: 1px solid {c['border']};
    padding: 9px 10px;
    font-weight: 600;
    font-size: 11px;
    color: {c['muted']};
    letter-spacing: .4px;
}}
QHeaderView::section:first {{ border-top-left-radius: 12px; }}
QHeaderView::section:last {{ border-top-right-radius: 12px; }}
QTableCornerButton::section {{ background-color: {c['table_header_bg']}; border: none; }}

/* === Activity feed ====================================================== */
QFrame#activityRow {{ background: transparent; border-radius: 10px; }}
QFrame#activityRow:hover {{ background-color: {c['hover']}; }}
QLabel#activityName {{ font-size: 13px; font-weight: 600; color: {c['text']}; }}
QLabel#activityDetail {{ font-size: 11px; color: {c['muted']}; }}
QLabel#activityTime {{ font-size: 11px; color: {c['muted']}; }}
QFrame#activityAvatar {{ background-color: {c['avatar_bg']}; border-radius: 18px; }}
QLabel#activityAvatarText {{ color: {c['avatar_fg']}; font-size: 12px; font-weight: 700; }}

/* === Status panel ======================================================= */
QFrame#statusPanel {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 14px;
}}
QFrame#statusRow {{ background: transparent; border-radius: 8px; }}
QFrame#statusRow:hover {{ background-color: {c['hover']}; }}
QLabel#statusRunning {{ font-size: 15px; font-weight: 700; color: {c['success']}; }}
QLabel#statusStopped {{ font-size: 15px; font-weight: 700; color: {c['danger']}; }}
QLabel#statusItem {{ font-size: 12px; color: {c['text_secondary']}; }}
QLabel#statusDot {{ font-size: 9px; }}
QLabel#statusValue {{ font-size: 12px; font-weight: 600; color: {c['text']}; }}

/* === System logs ======================================================== */
QFrame#logPanel {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 14px;
}}
QListWidget#logList {{
    background-color: {c['log_bg']};
    border: 1px solid {c['log_border']};
    border-radius: 10px;
    padding: 4px;
}}
QListWidget#logList::item {{ padding: 2px 4px; border-radius: 6px; }}
QListWidget#logList::item:selected {{ background-color: {c['primary_soft']}; }}
QLabel#logTag {{
    font-size: 10px; font-weight: 700; border-radius: 5px; padding: 1px 7px;
    background-color: {c['chip_bg']}; color: {c['chip_fg']};
}}
QLabel#logTag[level="INFO"] {{ background-color: {c['info_bg']}; color: {c['log_tag_info']}; }}
QLabel#logTag[level="DEBUG"] {{ background-color: {c['chip_bg']}; color: {c['log_tag_debug']}; }}
QLabel#logTag[level="WARN"] {{ background-color: {c['warn_bg']}; color: {c['log_tag_warn']}; }}
QLabel#logTag[level="ERROR"] {{ background-color: {c['danger_bg']}; color: {c['log_tag_error']}; }}
QLabel#logTimestamp {{
    font-size: 11px; color: {c['muted']};
    font-family: "SF Mono", "JetBrains Mono", "Consolas", monospace;
}}
QLabel#logMessage {{
    font-size: 12px; color: {c['text_secondary']};
    font-family: "SF Mono", "JetBrains Mono", "Consolas", monospace;
}}

/* === Camera preview ===================================================== */
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
    border-radius: 9px;
    padding: 2px 9px;
}}
QLabel#cameraInfo {{ font-size: 12px; color: {c['muted']}; }}
QLabel#cameraValue {{ font-size: 12px; font-weight: 600; color: {c['text_secondary']}; }}
QLabel#faceBadge {{
    background-color: rgba(2, 6, 23, 0.72);
    color: #FFFFFF;
    font-size: 11px;
    font-weight: 600;
    border-radius: 10px;
    padding: 4px 12px;
}}
QLabel#fpsOverlay {{
    font-size: 11px;
    color: rgba(255,255,255,0.85);
    background-color: rgba(2, 6, 23, 0.72);
    border-radius: 8px;
    padding: 3px 9px;
}}

/* === Avatars ============================================================ */
QLabel#avatar {{
    background-color: {c['avatar_bg']};
    color: {c['avatar_fg']};
    border-radius: 18px;
    font-weight: 700;
    font-size: 14px;
}}

/* === Tabs / groups / progress =========================================== */
QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: 12px;
    background-color: {c['card']};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 8px 16px;
    margin-right: 4px;
    border-radius: 9px;
    color: {c['muted']};
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background-color: {c['primary_soft']};
    color: {c['primary_soft_fg']};
}}
QTabBar::tab:hover:!selected {{ color: {c['text']}; background-color: {c['hover']}; }}
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 12px;
    margin-top: 14px;
    padding: 16px 14px 14px 14px;
    background-color: {c['card']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    color: {c['text']};
    font-weight: 600;
}}
QProgressBar {{
    border: none;
    border-radius: 5px;
    background-color: {c['chip_bg']};
    text-align: center;
    font-size: 10px;
    color: {c['text_secondary']};
    min-height: 8px;
}}
QProgressBar::chunk {{ background-color: {c['primary']}; border-radius: 5px; }}

/* === Navigation ========================================================= */
QFrame#sidebar {{
    background-color: {c['sidebar_bg']};
    border-right: 1px solid {c['border_soft']};
}}
QFrame#sidebarFooter {{
    background-color: {c['sidebar_bg']};
    border-top: 1px solid {c['border_soft']};
}}
QFrame#brandTile {{
    background-color: {c['primary']};
    border-radius: 12px;
}}
QListWidget#nav {{
    border: none;
    border-radius: 0px;
    background-color: {c['sidebar_bg']};
    padding: 4px 10px;
    outline: none;
}}
QListWidget#nav::item {{
    padding: 10px 12px;
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
QListWidget#nav::item:disabled {{
    color: {c['nav_section']};
    background: transparent;
    font-size: 10px;
    font-weight: 700;
    padding: 10px 12px 4px 12px;
}}

/* === Header ============================================================= */
QFrame#headerBar {{
    background-color: {c['header_bg']};
    border-bottom: 1px solid {c['header_border']};
}}
QFrame#headerChip {{
    background-color: {c['panel_alt']};
    border: 1px solid {c['border_soft']};
    border-radius: 12px;
}}
QFrame#headerDivider {{ background-color: {c['border_soft']}; border: none; }}
QLabel#headerChipPrimary {{ font-size: 13px; font-weight: 700; color: {c['text']}; }}
QLabel#headerChipSecondary {{ font-size: 11px; color: {c['muted']}; }}
QPushButton#headerIconButton {{
    background-color: {c['panel_alt']};
    border: 1px solid {c['border_soft']};
    border-radius: 12px;
    padding: 6px;
    min-width: 20px;
}}
QPushButton#headerIconButton:hover {{ background-color: {c['hover']}; border-color: {c['primary']}; }}

/* === Login ============================================================== */
QFrame#loginCard {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 16px;
}}
QLabel#loginTitle {{ font-size: 18px; font-weight: 700; color: {c['text']}; }}

/* === Scrollbars & status bar ============================================ */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {c['border']}; border-radius: 5px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {c['border']}; border-radius: 5px; min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
QStatusBar {{
    background-color: {c['header_bg']};
    border-top: 1px solid {c['header_border']};
    color: {c['muted']};
    font-size: 12px;
    padding: 2px 14px;
}}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background-color: {c['border_soft']}; }}

/* === Toast ============================================================== */
QFrame#toast {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-left: 4px solid {c['info']};
    border-radius: 10px;
}}
QFrame#toast[tone="ok"] {{ border-left-color: {c['success']}; }}
QFrame#toast[tone="bad"] {{ border-left-color: {c['danger']}; }}
QFrame#toast[tone="warn"] {{ border-left-color: {c['warn']}; }}
QLabel#toastText {{ font-size: 12px; color: {c['text_secondary']}; }}

/* === Frameless feed scrolling =========================================== */
QScrollArea#scrollFeed, QScrollArea#scrollFeed > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QScrollArea#scrollFeed {{ border-radius: 10px; }}
"""


def palette(theme="light"):
    return PALETTES.get(theme, PALETTES["light"])


def tone_color(theme, tone):
    """Resolve a tone name (ok/warn/bad/idle/info) to a hex colour."""
    return palette(theme)[TONE_COLORS.get(tone, "muted")]


def stat_tile_colors(theme, key):
    """Foreground/background pair for a stat card icon tile."""
    c = palette(theme)
    return c.get(f"stat_icon_{key}", c["primary"]), c.get(f"stat_icon_{key}_bg", c["primary_soft"])

