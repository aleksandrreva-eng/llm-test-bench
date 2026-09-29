"""Тема оформления: тёмная и светлая.

Токены тёмной темы сняты с утверждённого макета `docs/ui-prototype.html` —
приложение должно выглядеть как он, а не «похоже». Светлая собрана из тех же
ролей: макета для неё нет, поэтому она не «придумана заново», а повторяет
тёмную роль в роль.

Главное отличие от прежней темы: поверхности разделяются **яркостью**, а не
рамкой. Четыре ступени (`BG` → `SURFACE` → `SURFACE_2` → `SURFACE_3`) дают
иерархию, а рамка осталась только у вложенных объектов — поля ввода, таблицы,
разделители. Когда рамка была у каждого блока, она не выделяла ничего.

Направление ступени в темах разное, и это не ошибка. В тёмной окно самое
тёмное, панели светлее, наведение ещё светлее — «выше» значит «ярче». В
светлой окно серое, панели белые, а наведение и нажатие темнее панели:
на белом «выше» читается затемнением. Роль у токена одна и та же, меняется
только сторона контраста.

Имена старых констант сохранены (`PANEL`, `HEADER`, `TEXT_DIM`, `TEXT_MUTED`,
`ACCENT_HOVER`, `SELECTED`): их используют панели и проверки, и ломать их
ради переименования незачем.

Как это работает. Токены лежат в двух словарях (`DARK`, `LIGHT`), а модульные
имена (`theme.BG`, `theme.TEXT_3`, …) — это те же значения, разложенные по
именам при переключении темы. Панели читают `theme.ЦВЕТ` в момент отрисовки
(137 таких обращений), поэтому смена темы их не требует править: достаточно
переложить словарь и пересобрать стиль. Единственное, что общий стиль не
перепишет, — виджеты, которые ставят себе цвета сами через `setStyleSheet`;
у них есть метод `restyle_theme()`, и `apply_theme` зовёт его у всех.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# --- палитры -----------------------------------------------------------
#
# Ключи обоих словарей обязаны совпадать: `check_ui_server.py` это сверяет.
# Пропущенный в светлой теме ключ не упал бы, а тихо оставил бы в модуле
# цвет от прошлой темы — поэтому сверка, а не надежда.

DARK: dict[str, str] = {
    # поверхности: четыре ступени яркости
    "BG": "#131519",  # фон окна и полей ввода
    "SURFACE": "#191c22",  # панели, карточки, рельс
    "SURFACE_2": "#1f232b",  # шапка таблицы, полоса прогона
    "SURFACE_3": "#262b34",  # наведение, кнопки второго плана
    "SURFACE_4": "#2d333d",  # нажатие, дорожка прогресса
    "BORDER": "#2b313b",  # разделители и рамки вложенного
    "BORDER_STRONG": "#3a4250",  # рамки полей, hover-рамки
    # текст
    "TEXT": "#e8ecf2",  # заголовки, значения, имена
    "TEXT_2": "#a8b2c1",  # основной текст, строки таблиц
    "TEXT_3": "#6f7a8c",  # подсказки, подписи, неактивное
    "LABEL": "#8ab4f8",  # подписи параметров — «синий ключ» из макетов
    # акцент и семантика
    "ACCENT": "#3b82f6",
    "ACCENT_HI": "#5b9bff",  # наведение: в тёмной теме светлее
    "ACCENT_LO": "#2f6fd8",  # нажатие
    "ACCENT_DIM": "#1c3050",  # заливка выбранного. Сплошной цвет, а не rgba:
    # в Qt-стилях прозрачность ведёт себя по-разному
    # в зависимости от того, куда её подставили
    "OK": "#3fb950",
    "WARN": "#d29922",
    "FAIL": "#f85149",
    "INFO": "#58a6ff",
    # Приглушённые заливки под цветной текст: метка со счётом «13/15» читается
    # только тогда, когда фон темнее самого цвета, а не наоборот.
    "OK_DIM": "#1b3a22",
    "WARN_DIM": "#3a2f14",
    "FAIL_DIM": "#3a2124",
    "CONSOLE_BG": "#0e1013",  # журнал прогона: темнее фона, как в макете
    "DISABLED": "#4a5364",  # выключенная иконка
}

LIGHT: dict[str, str] = {
    "BG": "#f1f3f7",
    "SURFACE": "#ffffff",
    "SURFACE_2": "#f7f9fc",
    "SURFACE_3": "#eaeef4",
    "SURFACE_4": "#dfe4ec",
    "BORDER": "#dce0e7",
    "BORDER_STRONG": "#c2c9d4",
    "TEXT": "#0f141b",
    "TEXT_2": "#3a4250",
    # Подсказки темнее, чем «кажется нужным» для серого: на белом серый
    # читается хуже, чем на тёмном. Контраст подобран замером (см. ниже),
    # а не на глаз: #6b7484 давал 4.05 на метке «не гонялся» — ниже порога.
    "TEXT_3": "#5c6472",
    "LABEL": "#1a56c4",
    "ACCENT": "#2563eb",
    "ACCENT_HI": "#1d4ed8",  # наведение: в светлой теме темнее
    "ACCENT_LO": "#1e40af",
    "ACCENT_DIM": "#dbe6fd",
    # Семантика: цвета темнее «светлых» оттенков тёмной темы. На белом
    # светлый зелёный и янтарный дают контраст около 3 — текст не читается.
    # Замер по парам «цвет / приглушённая заливка»: OK 5.63, WARN 5.22,
    # FAIL 5.32 — у тёмной темы 4.94 / 5.21 / 4.40, то есть не хуже.
    "OK": "#17702f",
    "WARN": "#8a5c00",
    "FAIL": "#b81c27",
    "INFO": "#0969da",
    "OK_DIM": "#dcfce7",
    "WARN_DIM": "#fef3c7",
    "FAIL_DIM": "#fee2e2",
    "CONSOLE_BG": "#f4f6fa",
    "DISABLED": "#a8b0bd",
}

PALETTES: dict[str, dict[str, str]] = {"dark": DARK, "light": LIGHT}

#: Цвета линий на графиках. Набор на тему: на белом светлые тона тёмной
#: палитры не читаются, на тёмном тёмные тона светлой — тоже.
CHART_SERIES: dict[str, list[str]] = {
    "dark": ["#4ec9b0", "#569cd6", "#dcdcaa", "#c586c0", "#ce9178", "#b5cea8"],
    "light": ["#0f766e", "#1d4ed8", "#a16207", "#a21caf", "#c2410c", "#4d7c0f"],
}

#: Как называть темы в интерфейсе.
THEME_LABELS: dict[str, str] = {
    "dark": "Тёмная",
    "light": "Светлая",
    "auto": "Авто",
}

#: Порядок пунктов в меню «Вид» → «Тема». Светлая первая: её ищут, когда
#: тёмная не подходит, а «Авто» — режим, а не оформление, поэтому последний.
THEME_CHOICES: tuple[str, ...] = ("light", "dark", "auto")

#: Что делает каждый пункт — в подсказке, чтобы «Авто» не читалось как третья
#: палитра: это выбор между двумя, который делает система.
THEME_HINTS: dict[str, str] = {
    "light": "Светлое оформление",
    "dark": "Тёмное оформление — как на утверждённом макете",
    "auto": "Следовать теме системы: светлая или тёмная",
}

# --- сетка и шрифты: от темы не зависят ---
ROW_H = 28  # строка таблицы
CTL_H = 32  # поле ввода и обычная кнопка
PRIMARY_H = 36  # главная кнопка
RADIUS = 6

FONT_UI = '"Segoe UI Variable Text", "Segoe UI", Arial, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", monospace'

#: Подстановки в шаблон, которые цветом не являются.
_GEOMETRY = {"RADIUS": RADIUS, "FONT_UI": FONT_UI, "FONT_MONO": FONT_MONO}


_TEMPLATE = """
QWidget {{
    background-color: {BG};
    color: {TEXT_2};
    font-family: {FONT_UI};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

/* --- меню: это верхняя строка окна, поэтому она приподнята --- */
QMenuBar {{
    background: {SURFACE};
    color: {TEXT_2};
    border-bottom: 1px solid {BORDER};
    padding: 3px 6px;
}}
QMenuBar::item {{ padding: 5px 10px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {SURFACE_3}; color: {TEXT}; }}
QMenu {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; color: {TEXT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* --- карточка. Раньше рамка была у КАЖДОГО блока и потому не выделяла ничего --- */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    margin-top: 20px;
    padding: 10px;
    font-weight: 600;
    color: {TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 2px;
    padding: 0 0 6px 0;
    color: {TEXT};
    background: transparent;
}}

/* --- карточка Card: цвета сведены сюда, чтобы смена темы их переписывала --- */
#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
#cardHeader {{ background: transparent; border-bottom: 1px solid {BORDER}; }}
#cardBody {{ background: transparent; border: none; }}
#cardFooter {{ background: transparent; border-top: 1px solid {BORDER}; }}
#cardTitle {{ color: {TEXT}; font-weight: 600; background: transparent; border: none; }}
#cardBadge {{
    color: {TEXT_3}; background: {SURFACE_3}; border: none; border-radius: 9px;
    padding: 1px 8px; font-size: 11px; font-weight: 600;
}}

/* --- поля ввода --- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
    selection-background-color: {ACCENT_DIM};
    min-height: 22px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QPlainTextEdit:disabled {{
    color: {TEXT_3};
    background: {SURFACE_2};
    border-color: {BORDER};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_3};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    selection-background-color: {ACCENT_DIM};
    color: {TEXT};
    padding: 4px;
    outline: none;
}}

/* --- кнопки --- */
QPushButton {{
    background: {SURFACE_3};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 5px 14px;
    color: {TEXT_2};
    min-height: 22px;
}}
QPushButton:hover {{ background: {SURFACE_4}; color: {TEXT}; }}
QPushButton:pressed {{ background: {SURFACE_2}; }}
QPushButton:disabled {{ color: {TEXT_3}; background: {SURFACE_2}; border-color: {BORDER}; }}
QPushButton[accent="true"] {{
    background: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_LO}; }}
QPushButton[accent="true"]:disabled {{
    background: {SURFACE_2}; color: {TEXT_3}; border-color: {BORDER};
}}
QPushButton[danger="true"] {{ color: {FAIL}; }}
QPushButton[danger="true"]:hover {{ background: {FAIL_DIM}; color: {FAIL}; border-color: {FAIL}; }}
QPushButton[flat="true"] {{
    background: transparent; border-color: transparent; color: {TEXT_3}; padding: 4px 8px;
}}
QPushButton[flat="true"]:hover {{ background: {SURFACE_3}; color: {TEXT}; }}

/* Кнопка-метка (тег): состояние через свойство `on` */
QPushButton[chip="true"] {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 13px;
    padding: 3px 12px;
    color: {TEXT_3};
    font-size: 12px;
}}
QPushButton[chip="true"]:hover {{ color: {TEXT_2}; border-color: {BORDER_STRONG}; }}
QPushButton[chip="true"][on="true"] {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: {LABEL};
    font-weight: 600;
}}

QCheckBox, QRadioButton {{ spacing: 7px; color: {TEXT_2}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1.5px solid {BORDER_STRONG};
    border-radius: 3px;
    background: {BG};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {BORDER}; background: {SURFACE_2};
}}

/* --- таблицы и списки --- */
QListWidget, QTreeWidget, QTableWidget, QTableView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {TEXT_2};
    gridline-color: {BORDER};
    alternate-background-color: {SURFACE};
    selection-background-color: {ACCENT_DIM};
    selection-color: {TEXT};
    outline: none;
}}
QListWidget::item {{ padding: 5px 7px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {ACCENT_DIM}; color: {TEXT}; }}
QListWidget::item:hover {{ background: {SURFACE_2}; }}
QTableWidget::item {{ padding: 0 8px; }}
QTableWidget::item:selected {{ background: {ACCENT_DIM}; color: {TEXT}; }}
QHeaderView {{ background: {SURFACE_2}; }}
QHeaderView::section {{
    background: {SURFACE_2};
    color: {TEXT_3};
    padding: 7px 8px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
    font-size: 11px;
}}
QHeaderView::section:hover {{ color: {TEXT_2}; }}
QTableCornerButton::section {{ background: {SURFACE_2}; border: none; }}

/* --- прогресс --- */
QProgressBar {{
    background: {SURFACE_4};
    border: none;
    border-radius: 3px;
    text-align: center;
    color: {TEXT};
    height: 6px;
    font-size: 11px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* --- прокрутка --- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {SURFACE_4}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QStatusBar {{ background: {SURFACE_2}; color: {TEXT_3}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
QSplitter::handle:hover {{ background: {BORDER}; }}

QToolTip {{
    background: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 5px 8px;
}}

/* --- роли подписей (используются панелями) --- */
QLabel[role="hint"] {{ color: {TEXT_3}; }}
QLabel[role="label"] {{ color: {LABEL}; }}
QLabel[role="cardTitle"] {{ color: {TEXT}; font-weight: 600; }}
QLabel[role="metric"] {{ color: {TEXT}; font-size: 22px; font-weight: 600; }}
QLabel[role="mono"] {{ font-family: {FONT_MONO}; font-size: 12px; }}
QLabel[role="pageTitle"] {{
    color: {TEXT}; font-size: 20px; font-weight: 600;
    background: transparent; letter-spacing: -0.2px;
}}
QLabel[role="pageSub"] {{ color: {TEXT_3}; font-size: 12px; background: transparent; }}
QLabel[role="fieldLabel"] {{
    color: {LABEL}; font-size: 12px; background: transparent;
}}
QLabel[role="metricKey"] {{
    color: {TEXT_3}; font-size: 11px; letter-spacing: 0.4px;
    text-transform: uppercase; background: transparent;
}}
QLabel[role="metricValue"] {{
    color: {TEXT}; font-size: 24px; font-weight: 600; background: transparent;
}}
QLabel[role="name"] {{ color: {TEXT}; background: transparent; }}
QLabel[status="OK"] {{ color: {OK}; }}
QLabel[status="WARN"] {{ color: {WARN}; }}
QLabel[status="FAIL"] {{ color: {FAIL}; }}
QLabel[status="INFO"] {{ color: {TEXT_3}; }}

/* --- кнопки помельче: в шапках карточек и подвалах --- */
QPushButton[size="sm"] {{
    min-height: 18px; padding: 2px 10px; font-size: 12px;
}}
QPushButton[size="icon"] {{
    min-height: 18px; padding: 3px 5px; background: transparent;
    border-color: transparent;
}}
QPushButton[size="icon"]:hover {{ background: {SURFACE_3}; border-color: {BORDER}; }}

/* --- полоса прогона: приподнята над рабочей областью --- */
QFrame#runBar {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QPlainTextEdit#console {{
    background: {CONSOLE_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {TEXT_2};
    font-family: {FONT_MONO};
    font-size: 11px;
    padding: 6px 8px;
    selection-background-color: {ACCENT_DIM};
}}

/* --- список с отметками (модели, наборы) --- */
QListWidget#optList {{ background: transparent; border: none; padding: 0; }}
QListWidget#optList::item {{ border: none; }}

/* --- подписи, которые ставились себе самим стилем --- */
QLabel#runStat {{ color: {TEXT_3}; font-size: 12px; background: transparent; }}
QLabel#modelsPath {{
    color: {TEXT_3}; font-family: {FONT_MONO}; font-size: 11px;
    background: transparent;
}}

/* Идентификаторы в шапках карточек кейса и набора: моноширинный шрифт
   ставится кодом (от него считается высота блока), цвет — здесь. */
QLabel#caseId, QLabel#setIdent {{ color: {LABEL}; background: transparent; }}

/* Строка набора в панели тестирования. */
QLabel[role="setName"] {{
    color: {TEXT}; font-family: {FONT_MONO}; font-size: 12px;
    background: transparent;
}}
QLabel[role="setMeta"] {{ color: {TEXT_3}; font-size: 11px; background: transparent; }}
QLabel#setCasesLabel {{ color: {TEXT_3}; font-size: 12px; background: transparent; }}
QFrame#hrLine {{ background: {BORDER}; border: none; }}

/* Предупреждение о разных версиях наборов (п. 11.7 ТЗ). */
QLabel#versionWarning {{
    color: {WARN}; background: {WARN_DIM}; border: 1px solid {WARN};
    border-radius: 4px; padding: 6px 10px;
}}

/* Текстовые блоки карточки кейса: цвет по роли блока, не по виджету. */
QPlainTextEdit#caseText[tone="muted"] {{ color: {TEXT_3}; }}
QPlainTextEdit#caseText[tone="fail"] {{ color: {FAIL}; }}

/* --- рельс разделов --- */
#navRail {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}

/* --- полоса массовых действий в истории --- */
#filters {{ background: transparent; border-bottom: 1px solid {BORDER}; }}
QFrame#bulkBar {{
    background: {ACCENT_DIM};
    border: 1px solid {ACCENT};
    border-radius: 4px;
}}
QFrame#bulkBar QLabel {{ background: transparent; color: {TEXT}; font-weight: 600; }}

/* --- пустое состояние --- */
QLabel[role="emptyTitle"] {{ color: {TEXT_2}; font-size: 14px; font-weight: 600; }}
QLabel[role="emptyText"] {{ color: {TEXT_3}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""


#: Готовая таблица стилей активной темы. Объявлена пустой только чтобы имя
#: было видно статическому анализатору: собирается она в `_install()` вместе с
#: остальными токенами, которые тоже появляются в модуле динамически.
STYLESHEET: str = ""


def build_stylesheet(tokens: dict[str, str] | None = None) -> str:
    """Собрать таблицу стилей из набора токенов.

    Шаблон один на обе темы: различаются только подставляемые значения.
    Раньше это была f-строка, собранная на импорте, — из-за этого тему нельзя
    было сменить без перезапуска.
    """
    values = dict(tokens if tokens is not None else PALETTES[_active])
    values.update(_GEOMETRY)
    return _TEMPLATE.format(**values)


# --- активная тема -----------------------------------------------------

_active: str = "dark"


def active_theme() -> str:
    """Какая тема действует сейчас: `dark` или `light`."""
    return _active


def resolve(name: str) -> str:
    """Имя темы из настроек → `dark` или `light`.

    «Авто» спрашивает систему. Если система не сказала ничего внятного —
    возвращаем тёмную: она утверждена макетом, и ошибиться в её сторону
    безопаснее, чем показать светлую тому, кто её не просил.
    """
    if name in PALETTES:
        return name
    app = QApplication.instance()
    if app is not None:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return "light"
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
    return "dark"


def _install(name: str) -> None:
    """Разложить токены темы по модульным именам.

    Имена не объявлены в коде явно, а создаются здесь: панели читают
    `theme.BG` и остальные 137 раз, и держать рядом второй список тех же имён
    — значит рано или поздно их разойтись. Сверку ключей обеих палитр делает
    `check_ui_server.py`, так что забытый токен ловится проверкой, а не глазом.
    """
    tokens = PALETTES[name]
    g = globals()
    g.update(tokens)

    # Производные имена: старые названия сохранены ради панелей и проверок.
    g["PANEL"] = tokens["SURFACE"]
    g["HEADER"] = tokens["SURFACE_2"]
    g["TEXT_DIM"] = tokens["TEXT_2"]
    g["TEXT_MUTED"] = tokens["TEXT_3"]
    g["ACCENT_HOVER"] = tokens["ACCENT_HI"]
    g["SELECTED"] = tokens["ACCENT_DIM"]
    g["STATUSBAR"] = tokens["SURFACE_2"]

    # Цвета статусов для ячеек таблиц и логов
    g["STATUS_COLORS"] = {
        "OK": tokens["OK"],
        "PASS": tokens["OK"],
        "WARN": tokens["WARN"],
        "FAIL": tokens["FAIL"],
        "ERROR": tokens["FAIL"],
    }
    g["CHART_SERIES_ACTIVE"] = list(CHART_SERIES[name])
    g["STYLESHEET"] = build_stylesheet(tokens)


def _restyle_widgets(app: QApplication) -> None:
    """Позвать `restyle_theme()` у всех, кто её умеет.

    Часть виджетов ставит цвета не общим стилем, а сама через `setStyleSheet`
    при создании: у карточки это фон шапки, у метки — цвет по смыслу. Общий
    стиль их не перепишет, поэтому контракт такой: есть метод
    `restyle_theme` — виджет перечитает цвета сам.
    """
    for widget in app.allWidgets():
        try:
            hook = getattr(widget, "restyle_theme", None)
        except RuntimeError:
            continue  # C++ объект уже удалён, виджету уже всё равно
        if callable(hook):
            hook()


def apply_theme(app: QApplication, name: str = "dark") -> None:
    """Применить тему: палитра, стиль и перекраска тех, кто красится сам.

    `name` — `dark`, `light` или `auto`. Значение по умолчанию — тёмная,
    чтобы старые вызовы `apply_theme(app)` (проверки, снимки) продолжали
    работать без правок.
    """
    global _active
    resolved = resolve(name)
    _install(resolved)
    _active = resolved
    tokens = PALETTES[resolved]

    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(tokens["BG"]))
    pal.setColor(QPalette.WindowText, QColor(tokens["TEXT_2"]))
    pal.setColor(QPalette.Base, QColor(tokens["BG"]))
    pal.setColor(QPalette.AlternateBase, QColor(tokens["SURFACE"]))
    pal.setColor(QPalette.Text, QColor(tokens["TEXT"]))
    pal.setColor(QPalette.Button, QColor(tokens["SURFACE_3"]))
    pal.setColor(QPalette.ButtonText, QColor(tokens["TEXT_2"]))
    pal.setColor(QPalette.Highlight, QColor(tokens["ACCENT_DIM"]))
    pal.setColor(QPalette.HighlightedText, QColor(tokens["TEXT"]))
    pal.setColor(QPalette.ToolTipBase, QColor(tokens["SURFACE_2"]))
    pal.setColor(QPalette.ToolTipText, QColor(tokens["TEXT"]))
    pal.setColor(QPalette.PlaceholderText, QColor(tokens["TEXT_3"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(tokens["TEXT_3"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(tokens["TEXT_3"]))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
    _restyle_widgets(app)


def color(token: str) -> str:
    """Цвет по имени токена — для мест, где цвет передают строкой.

    Иконки и делегаты получают не значение, а имя токена (`"TEXT_3"`), и
    берут цвет в момент отрисовки: иначе после смены темы у них остался бы
    цвет прошлой. Готовый «#rrggbb» возвращается как есть — так вызывающий
    может передать и постоянный цвет вроде белого на акцентной кнопке.
    """
    if token.startswith("#"):
        return token
    return globals().get(token, token)


_install("dark")


def mono_font(size: int = 11) -> QFont:
    """Моноширинный шрифт для логов, чисел и путей.

    Числа набраны табличными цифрами — колонки не «пляшут» при обновлении.
    """
    f = QFont("Cascadia Mono")
    if not f.exactMatch():
        f = QFont("Consolas")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма множественного числа: 1 набор / 2 набора / 5 наборов.

    Без этого счётчик пишет «1 наборов» — мелочь, по которой интерфейс сразу
    читается как неродной.
    """
    a, b = abs(n) % 10, abs(n) % 100
    if a == 1 and b != 11:
        return one
    if 2 <= a <= 4 and (b < 10 or b >= 20):
        return few
    return many


def grouped(value: int) -> str:
    """262144 → «262 144»: разряды читаются, а слипшееся число — нет."""
    return "{:,}".format(int(value)).replace(",", " ")


#: Сколько знаков имени модели помещается в шапку колонки сравнения. Больше —
#: заголовок наезжает на соседний столбец: `QHeaderView` длинный текст не
#: укорачивает, а рисует поверх соседней колонки.
HEADER_CHARS = 24


def elide(text: str, limit: int = HEADER_CHARS) -> str:
    """Обрезать подпись по числу знаков, сохранив начало.

    По знакам, а не по пикселям: ширины колонок зависят от числа прогонов и
    меняются на лету, а число знаков известно заранее и одинаково у всех
    колонок — иначе подписи в шапке выглядели бы разной длины без причины.
    """
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def restyle(*widgets) -> None:
    """Перечитать стиль после смены динамического свойства.

    Qt не пересчитывает QSS при `setProperty`: без unpolish/polish кнопка
    останется серой, хотя свойство `accent` уже выставлено. Вызов разбросан
    по всему коду, поэтому собран в одну функцию.
    """
    for w in widgets:
        if w is None:
            continue
        w.style().unpolish(w)
        w.style().polish(w)
