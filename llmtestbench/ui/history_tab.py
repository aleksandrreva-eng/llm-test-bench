"""Раздел «История» (п. 5.2 ТЗ, макет `docs/ui-prototype.html`).

Читает JSON-прогоны из папки results/, собирает их в **прогоны целиком**,
даёт фильтры, сортировку по столбцу, выбор нескольких записей, пагинацию и
переход к сравнению.

Главное отличие от прежней вкладки — **строка таблицы это прогон, а не файл**.
Одно нажатие «Запустить» пишет столько файлов, сколько отмечено наборов, и
история показывала семь независимых строк там, где была одна партия. Теперь
это одна строка, которая разворачивается на кейсы прямо в таблице, а двойной
клик по кейсу открывает его карточку в окне. Разворот показывает первые
`CASE_PREVIEW_LIMIT` кейсов — в наборе `chat_single` их двадцать, а в прогоне
из всех наборов под сотню, и разворот целиком вытеснил бы с экрана всё
остальное; остаток сворачивается в строку «ещё N» со ссылкой на отчёт.

Что ещё изменилось против прежней вкладки:

* **фильтры применяются сразу.** Была кнопка «Применить»: выставил период,
  забыл нажать — и видишь не то, что настроил. Теперь каждое изменение поля
  перерисовывает список;
* **полоса массовых действий появляется только при выделении.** Кнопки
  «Сравнить» и «В корзину» висели всегда, хотя без выделения ничего не делают;
* **модель больше не дублируется.** Первая колонка показывала имя файла вида
  `{модель}_{набор}_{дата}.json`, а вторая — ту же модель. Теперь первая
  колонка — идентификатор прогона без модели, модель стоит один раз;
* **в колонке «Счёт» видно результат** — полоска и «13/15», а не только статус.

Источник — JSON, а не SQLite: у прогонов, перенесённых в базу, счёт пустой
(`total`/`passed` не разбираются при переносе), и история показывала нули.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QDate, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import report, results_index
from ..database import DatabaseManager
from ..results_index import RunGroup, RunRecord, seconds_text
from . import icons, theme
from .card import Card, EmptyState
from .case_card_dialog import CaseCardDialog

PAGE_SIZE = 10

#: Строка кейса ниже строки прогона: кейс — подпункт, а не равный.
CASE_ROW_H = 24

#: Отступ идентификатора кейса. Пробелами, а не отступом в пикселях: Qt не
#: умеет отступ текста в ячейке, а шрифт моноширинный, поэтому ширина пробела
#: постоянна и отступ предсказуем.
CASE_INDENT = "    "

#: Сколько кейсов показывает разворот. В наборе `chat_single` их двадцать, а в
#: прогоне из всех наборов — под сотню, и разворот целиком вытеснил бы с экрана
#: всё остальное. Остаток сворачивается в строку «ещё N» со ссылкой на отчёт.
CASE_PREVIEW_LIMIT = 15


#: Класс вердикта из `report.verdict` → цвет.
#: Функция, а не словарь на уровне модуля: словарь посчитался бы один раз при
#: импорте и остался в цветах той темы, что была при запуске.
def _verdict_color(verdict) -> str:
    return {
        "pass": theme.OK,
        "fail": theme.FAIL,
        "stand": theme.WARN,
        "skip": theme.TEXT_3,
    }.get(str(verdict), theme.TEXT_3)


# Столбец → как сортировать. Порядок совпадает с порядком колонок таблицы.
_SORT_KEYS = {
    1: lambda g: g.short_id.lower(),
    2: lambda g: g.model.lower(),
    3: lambda g: g.set_label.lower(),
    4: lambda g: g.score if g.score is not None else -1.0,
    5: lambda g: g.seconds,
    6: lambda g: g.started or g.finished or "",
    7: lambda g: g.status_ru,
}


def _clip(text: str, limit: int) -> str:
    """Сжать причину провала в одну короткую строку.

    В ячейке причина нужна одним взглядом, поэтому переводы строки и лишние
    пробелы схлопываются, а хвост обрезается. Полный текст остаётся в тултипе —
    он же и первоисточник, ячейка только показывает.
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _status_matches(group: RunGroup, status: str) -> bool:
    """Фильтр по статусу для партии: смотрим на файлы внутри неё.

    «Сбой» и «Остановлен» — если так закончился хотя бы один файл, «Готово» —
    если так закончились все. Это то же правило, по которому считается
    `RunGroup.status_ru`, поэтому фильтр и колонка не расходятся.
    """
    states = [(r.status or "").lower() for r in group.runs]
    if status == "finished":
        return bool(states) and all(state == "finished" for state in states)
    return status in states


def _matches_query(group: RunGroup, query: str) -> bool:
    """Поиск по идентификаторам: `batch_id`, любой `run_id`, любой набор."""
    if query in group.batch_id.lower():
        return True
    if any(query in rid.lower() for rid in group.run_ids):
        return True
    return any(query in sid.lower() for sid in group.set_ids)


def _group_status_color(group: RunGroup) -> str:
    """Цвет статуса прогона. Правило старшинства — как у `RunGroup.status_ru`."""
    states = [(r.status or "").lower() for r in group.runs]
    if "failed" in states:
        return theme.FAIL
    if "cancelled" in states:
        return theme.WARN
    if not group.scored:
        return theme.TEXT_3
    return theme.OK


class _ScoreDelegate(QStyledItemDelegate):
    """Колонка «Счёт». Два разных содержимого в одной колонке.

    Строка прогона — полоска доли пройденных кейсов плюс «13/15»: одного числа
    мало, чтобы сравнить два прогона глазами, нужна длина. Строка кейса —
    точка цвета вердикта и подпись («зачтён», «провал», «сбой стенда»).

    Делегат заменяет базовую отрисовку целиком, поэтому текст и заливку
    выделенной строки рисует сам — иначе ячейка окажется пустой.
    """

    BAR_W = 52
    BAR_H = 5

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 — Qt-имя
        return QSize(120, theme.ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(Qt.UserRole)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.ACCENT_DIM))

        if isinstance(data, dict) and "verdict" in data:
            self._paint_verdict(painter, option, data)
        elif data:
            self._paint_score(painter, option, data)
        else:
            self._paint_dash(painter, option)
        painter.restore()

    # ------------------------------------------------------------------

    @staticmethod
    def _paint_dash(painter: QPainter, option) -> None:
        painter.setPen(QColor(theme.TEXT_3))
        painter.drawText(option.rect.adjusted(10, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, "—")

    def _paint_score(self, painter: QPainter, option, data) -> None:
        passed, counted, kind = data
        share = (passed / counted) if counted else 0.0
        color = {"pass": theme.OK, "warn": theme.WARN, "fail": theme.FAIL}.get(kind, theme.TEXT_3)

        rect = option.rect
        bar_x = rect.left() + 10
        bar_y = rect.top() + (rect.height() - self.BAR_H) // 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.SURFACE_4))
        painter.drawRoundedRect(QRect(bar_x, bar_y, self.BAR_W, self.BAR_H), 3, 3)
        if share > 0:
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(
                QRect(bar_x, bar_y, max(3, int(self.BAR_W * share)), self.BAR_H), 3, 3
            )

        font = QFont()
        font.setPointSizeF(9.5)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(
            rect.adjusted(bar_x - rect.left() + self.BAR_W + 8, 0, -6, 0),
            Qt.AlignLeft | Qt.AlignVCenter,
            "%d/%d" % (passed, counted),
        )

    @staticmethod
    def _paint_verdict(painter: QPainter, option, data) -> None:
        """Строка кейса: точка вердикта и подпись под ней."""
        rect = option.rect
        color = _verdict_color(data.get("verdict"))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QPoint(rect.left() + 14, rect.top() + rect.height() // 2), 4, 4)

        font = QFont()
        font.setPointSizeF(9.5)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(
            rect.adjusted(24, 0, -6, 0),
            Qt.AlignLeft | Qt.AlignVCenter,
            str(data.get("label") or ""),
        )


class HistoryTab(QWidget):
    """Список прогонов с фильтрами, сортировкой и пагинацией."""

    compare_requested = Signal(list)  # список путей

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        # `_records` нужен только спискам моделей и наборов в фильтрах —
        # таблица работает с группами.
        self._records: list[RunRecord] = []
        self._groups: list[RunGroup] = []
        self._filtered: list[RunGroup] = []
        self._page = 1
        #: Выделение — по `run_id`, а не по ключам групп: сравнение и корзина
        #: работают с файлами, и группа здесь только способ их отметить.
        self._checked: set[str] = set()
        #: Раскрытые группы — по ключу, не по номеру строки: разворот сдвигает
        #: строки, и запомненные индексы после него указывали бы не туда.
        self._expanded: set[str] = set()
        #: `run_id` → путь к файлу. Заполняется при чтении папки.
        self._path_by_run: dict[str, Path] = {}
        #: Кнопки столбца действий. Список нужен потому, что Qt не удаляет
        #: виджеты ячеек вместе со строками — подробности в `_release_row_widgets`.
        self._row_widgets: list[QWidget] = []
        self._sort_col = 6  # по дате
        self._sort_desc = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        root.addWidget(self._build_bulk_bar())
        root.addWidget(self._build_card(), 1)

        self.refresh()

    # ------------------------------------------------------------------
    # построение

    def _build_bulk_bar(self) -> QFrame:
        """Полоса массовых действий. Скрыта, пока ничего не выделено."""
        bar = QFrame()
        bar.setObjectName("bulkBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(8)

        self.bulk_label = QLabel("Ничего не выделено")
        lay.addWidget(self.bulk_label)
        lay.addStretch(1)

        self.compare_btn = QPushButton("Сравнить выбранные")
        self.compare_btn.setProperty("size", "sm")
        self.compare_btn.setIcon(icons.icon("chart", "TEXT_2", 13))
        self.compare_btn.setToolTip(
            "Сравнить отмеченные прогоны целиком: столбец — прогон, строки — "
            "метрики, разворот строки — по наборам"
        )
        self.compare_btn.clicked.connect(self._compare)
        lay.addWidget(self.compare_btn)

        self.trash_btn = QPushButton("В корзину")
        self.trash_btn.setProperty("danger", True)
        self.trash_btn.setProperty("size", "sm")
        self.trash_btn.setIcon(icons.icon("trash", "FAIL", 13))
        self.trash_btn.clicked.connect(self._delete_selected)
        lay.addWidget(self.trash_btn)

        self.clear_btn = QPushButton("Снять выделение")
        self.clear_btn.setProperty("flat", True)
        self.clear_btn.setProperty("size", "sm")
        self.clear_btn.clicked.connect(lambda: self._check_all(False))
        lay.addWidget(self.clear_btn)

        bar.setVisible(False)
        self.bulk_bar = bar
        return bar

    def _build_card(self) -> Card:
        card = Card(show_header=False)
        self.card = card

        card.add_body(self._build_filters())

        self.table = self._build_table()
        self.empty = EmptyState(
            "clock",
            "Прогонов нет",
            "Запустите прогон на экране «Тестирование» — результат появится здесь.",
        )

        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        card.add_body(self.stack, 1)
        card.set_body_margins(0, 0, 0, 0)

        self.page_label = QLabel("Показано 0 из 0")
        self.page_label.setProperty("role", "hint")
        card.add_footer(self.page_label)

        self.select_all_btn = QPushButton("Выделить все")
        self.select_all_btn.setProperty("flat", True)
        self.select_all_btn.setProperty("size", "sm")
        self.select_all_btn.setToolTip("Отметить все прогоны, попавшие под фильтры")
        self.select_all_btn.clicked.connect(lambda: self._check_all(True))
        card.add_footer(self.select_all_btn)
        card.add_footer_stretch()

        self.prev_btn = QPushButton("‹ Назад")
        self.prev_btn.setProperty("flat", True)
        self.prev_btn.setProperty("size", "sm")
        self.prev_btn.clicked.connect(lambda: self._goto(self._page - 1))
        card.add_footer(self.prev_btn)

        self._pager_host = QWidget()
        self._pager_lay = QHBoxLayout(self._pager_host)
        self._pager_lay.setContentsMargins(0, 0, 0, 0)
        self._pager_lay.setSpacing(4)
        card.add_footer(self._pager_host)

        self.next_btn = QPushButton("Вперёд ›")
        self.next_btn.setProperty("flat", True)
        self.next_btn.setProperty("size", "sm")
        self.next_btn.clicked.connect(lambda: self._goto(self._page + 1))
        card.add_footer(self.next_btn)

        self.page_buttons: list[QPushButton] = []
        return card

    def _build_filters(self) -> QWidget:
        """Строка фильтров. Кнопки «Применить» нет — всё применяется сразу."""
        host = QWidget()
        host.setObjectName("filters")
        row = QHBoxLayout(host)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)

        row.addLayout(self._filter_field("Модель", self._model_combo()))
        row.addLayout(self._filter_field("Набор", self._set_combo()))
        row.addLayout(self._filter_field("Период", self._period_row()))
        row.addLayout(self._filter_field("Статус", self._status_combo()))

        self.search_edit = QLineEdit()
        # Подсказка короткая и в формате значения, а не целый id: длинный
        # образец обрезался по краю поля и читался как уже введённый фильтр.
        self.search_edit.setPlaceholderText("напр. chat_single")
        self.search_edit.setToolTip(
            "Фильтр по идентификатору прогона, имени модели или набора — "
            "сравнение по подстроке, регистр не важен"
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setStyleSheet("QLineEdit { font-family: %s; }" % theme.FONT_MONO)
        self.search_edit.textChanged.connect(lambda _: self._apply_filters())
        box = self._filter_field("Поиск по идентификатору", self.search_edit)
        row.addLayout(box, 1)

        reset_btn = QPushButton("Сбросить")
        reset_btn.setProperty("flat", True)
        reset_btn.setProperty("size", "sm")
        reset_btn.clicked.connect(self._reset_filters)
        row.addWidget(reset_btn, 0, Qt.AlignBottom)

        refresh_btn = QPushButton("Обновить")
        refresh_btn.setProperty("flat", True)
        refresh_btn.setProperty("size", "sm")
        refresh_btn.setIcon(icons.icon("refresh", "TEXT_3", 13))
        refresh_btn.setToolTip("Перечитать папку results/")
        refresh_btn.clicked.connect(self.refresh)
        row.addWidget(refresh_btn, 0, Qt.AlignBottom)
        return host

    @staticmethod
    def _filter_field(label: str, widget: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        cap = QLabel(label)
        cap.setProperty("role", "fieldLabel")
        box.addWidget(cap)
        box.addWidget(widget)
        return box

    def _model_combo(self) -> QComboBox:
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(170)
        self.model_combo.currentIndexChanged.connect(lambda _: self._apply_filters())
        return self.model_combo

    def _set_combo(self) -> QComboBox:
        self.type_combo = QComboBox()
        self.type_combo.setMinimumWidth(140)
        self.type_combo.currentIndexChanged.connect(lambda _: self._apply_filters())
        return self.type_combo

    def _status_combo(self) -> QComboBox:
        self.status_combo = QComboBox()
        self.status_combo.addItem("Все", "")
        self.status_combo.addItem("Готово", "finished")
        self.status_combo.addItem("Остановлен", "cancelled")
        self.status_combo.addItem("Сбой", "failed")
        self.status_combo.addItem("Без проверок", "no_score")
        self.status_combo.currentIndexChanged.connect(lambda _: self._apply_filters())
        return self.status_combo

    def _period_row(self) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("dd.MM.yyyy")
        self.date_from.setDate(QDate.currentDate().addMonths(-1))
        self.date_from.dateChanged.connect(lambda _: self._apply_filters())
        row.addWidget(self.date_from)

        dash = QLabel("—")
        dash.setProperty("role", "hint")
        row.addWidget(dash)

        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("dd.MM.yyyy")
        self.date_to.setDate(QDate.currentDate().addDays(1))
        self.date_to.dateChanged.connect(lambda _: self._apply_filters())
        row.addWidget(self.date_to)
        return host

    def _build_table(self) -> QTableWidget:
        cols = ["", "Прогон", "Модель", "Набор", "Счёт", "Время", "Дата", "Статус", ""]
        table = QTableWidget(0, len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(False)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(theme.ROW_H)
        table.setItemDelegateForColumn(4, _ScoreDelegate(table))
        table.setStyleSheet("QTableWidget { background: transparent; border: none; }")

        header = table.horizontalHeader()
        # Ширины заданы явно, а не «по содержимому». `ResizeToContents` растит
        # колонку под самое длинное значение, а растягиваемая колонка получает
        # остаток — и в истории этот остаток выходил отрицательным: колонка
        # «Прогон» сжималась до 108 px при нужных 139, и идентификатор прогона
        # показывался как «202609…». Замерено на настоящем окне.
        #
        # Растягиваем колонку «Набор», а не «Прогон»: идентификатор прогона
        # обрезать нельзя, а имя набора переносится в подсказку.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 34)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        table.setColumnWidth(1, 200)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        table.setColumnWidth(2, 180)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        table.setColumnWidth(4, 118)
        for column, width in ((5, 84), (6, 99), (7, 165)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            table.setColumnWidth(column, width)
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        table.setColumnWidth(8, 40)

        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)

        table.itemChanged.connect(self._on_item_changed)
        table.cellClicked.connect(self._on_cell_clicked)
        table.cellDoubleClicked.connect(self._on_double_click)
        return table

    # ------------------------------------------------------------------
    # данные

    def refresh(self) -> None:
        """Перечитать прогоны из папки results/ и собрать их в партии."""
        try:
            self._records = results_index.load_runs(self.cfg)
        except OSError:
            self._records = []
        self._groups = results_index.group_records(self._records)
        self._path_by_run = {rec.run_id: rec.path for rec in self._records}
        # Ключи исчезнувших групп чистим, иначе `_expanded` копил бы мусор.
        self._expanded &= {g.key for g in self._groups}
        self._fill_combos()
        self._apply_filters()

    def restyle_theme(self) -> None:
        """Перерисовать таблицу под новую тему.

        Цвет вердикта ставится на `QTableWidgetItem` через `setForeground` —
        это значение, а не правило стиля, и само оно не обновится. Проще
        пересобрать страницу из уже прочитанных прогонов, чем обходить
        строки и править цвета: страница строится из `self._filtered` и
        ничего не перечитывает с диска.
        """
        self._render_page()

    def total_runs(self) -> int:
        """Сколько файлов найдено."""
        return len(self._records)

    def total_groups(self) -> int:
        """Сколько прогонов найдено — для метки в рельсе.

        Именно прогонов, а не файлов: в рельсе число должно совпадать с числом
        строк, которые пользователь видит на экране.
        """
        return len(self._groups)

    def _fill_combos(self) -> None:
        """Пересобрать списки моделей и наборов, сохранив выбор."""
        cur_model = self.model_combo.currentData()
        cur_set = self.type_combo.currentData()
        for combo, values, current in (
            (self.model_combo, results_index.available_models(self._records), cur_model),
            (self.type_combo, results_index.available_sets(self._records), cur_set),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Все", "")
            for value in values:
                combo.addItem(value, value)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _apply_filters(self) -> None:
        """Применить фильтры и перерисовать. Вызывается на каждое изменение."""
        model = self.model_combo.currentData() or ""
        set_id = self.type_combo.currentData() or ""
        status = self.status_combo.currentData() or ""
        query = self.search_edit.text().strip().lower()
        d_from = self.date_from.date()
        d_to = self.date_to.date()

        out = []
        for group in self._groups:
            if model and group.model != model:
                continue
            # Набор матчится, если он есть хотя бы у одного файла партии:
            # иначе фильтр по набору прятал бы партию, в которой он прогнан.
            if set_id and set_id not in group.set_ids:
                continue
            if status == "no_score":
                if group.scored:
                    continue
            elif status and not _status_matches(group, status):
                continue
            if query and not _matches_query(group, query):
                continue
            d = group.date
            if d.isValid() and (d < d_from or d > d_to):
                continue
            out.append(group)

        key = _SORT_KEYS.get(self._sort_col)
        if key is not None:
            out.sort(key=key, reverse=self._sort_desc)
        self._filtered = out
        self._page = 1
        self._render_page()

    def _reset_filters(self) -> None:
        for combo in (self.model_combo, self.type_combo, self.status_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.date_from.setDate(QDate.currentDate().addMonths(-1))
        self.date_to.setDate(QDate.currentDate().addDays(1))
        self._apply_filters()

    def _on_header_clicked(self, column: int) -> None:
        """Сортировка по столбцу: повторный клик меняет направление."""
        if column not in _SORT_KEYS:
            return
        if column == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = column
            self._sort_desc = True
        self._apply_filters()

    # ------------------------------------------------------------------
    # отрисовка

    def _page_count(self) -> int:
        """Страниц по числу прогонов: разворот на пагинацию не влияет."""
        return max(1, (len(self._filtered) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _render_page(self) -> None:
        pages = self._page_count()
        self._page = min(max(1, self._page), pages)
        start = (self._page - 1) * PAGE_SIZE
        chunk = self._filtered[start : start + PAGE_SIZE]

        if self._filtered:
            self.stack.setCurrentWidget(self.table)
        else:
            if self._groups:
                self.empty.set_text(
                    "Под фильтры ничего не попало",
                    "Ослабьте период или сбросьте фильтры — прогоны в папке есть.",
                )
            else:
                self.empty.set_text(
                    "Прогонов нет",
                    "Запустите прогон на экране «Тестирование» — результат появится здесь.",
                )
            self.stack.setCurrentWidget(self.empty)

        # Список строк строится заранее: у прогона за развёрнутыми кейсами
        # строки идут подряд, и считать их по ходу заполнения — верный способ
        # сбиться на первой же раскрытой группе.
        rows: list[tuple[str, RunGroup, tuple]] = []
        for group in chunk:
            rows.append(("group", group, ()))
            if group.key not in self._expanded:
                continue
            cases = group.cases
            for rec, case in cases[:CASE_PREVIEW_LIMIT]:
                rows.append(("case", group, (rec, case)))
            hidden = len(cases) - CASE_PREVIEW_LIMIT
            if hidden > 0:
                rows.append(("more", group, (hidden,)))

        self._release_row_widgets()
        self.table.blockSignals(True)
        # Объединения ячеек снимаем перед пересборкой: строка «ещё N» тянется
        # на несколько колонок, и без сброса объединение осталось бы на строке,
        # которая теперь стала обычной.
        self.table.clearSpans()
        self.table.setRowCount(len(rows))
        for row, (kind, group, payload) in enumerate(rows):
            if kind == "group":
                self._fill_group_row(row, group)
            elif kind == "case":
                rec, case = payload
                self._fill_case_row(row, group, rec, case)
            else:
                self._fill_more_row(row, group, payload[0])
        self.table.blockSignals(False)

        header = self.table.horizontalHeader()
        header.setSortIndicator(
            self._sort_col, Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        )

        total = len(self._filtered)
        shown_from = start + 1 if chunk else 0
        shown_to = start + len(chunk)
        # После «из» нужен родительный падеж: «из 1 прогона», «из 5 прогонов».
        # Поэтому формы здесь не такие, как в именительном.
        self.page_label.setText(
            "Показано %d–%d из %d %s"
            % (shown_from, shown_to, total, theme.plural(total, "прогона", "прогонов", "прогонов"))
            if total
            else "Ничего не найдено"
        )

        self._rebuild_pager(pages)
        self._sync_bulk_bar()

    def _release_row_widgets(self) -> None:
        """Снять кнопки прошлой отрисовки.

        `setRowCount()` уничтожает элементы таблицы, но **не** виджеты,
        поставленные через `setCellWidget`: виджет с родителем остаётся жив и
        продолжает рисоваться там, где стоял. После свёртывания и разворота
        кнопки прошлой отрисовки оставались поверх колонки «Статус» — на снимке
        это выглядело как вторая колонка кнопок, сдвинутая на строку вверх.

        `setParent(None)` отвязывает виджет от окна сразу, поэтому он перестаёт
        рисоваться уже в этом кадре, а `deleteLater()` освобождает память в
        следующем цикле событий.
        """
        for widget in self._row_widgets:
            widget.setParent(None)
            widget.deleteLater()
        self._row_widgets.clear()

    # --- строка прогона -------------------------------------------------

    def _fill_group_row(self, row: int, group: RunGroup) -> None:
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(self._group_check_state(group))
        chk.setData(Qt.UserRole, ("group", group, None))
        self.table.setItem(row, 0, chk)

        opened = group.key in self._expanded
        name = QTableWidgetItem(group.short_id)
        name.setFont(theme.mono_font(9))
        # Треугольник разворота — в столбце 1, а не в нулевом: в нулевом
        # чекбокс, и две кликабельные зоны в одной ячейке мешают друг другу.
        name.setIcon(icons.icon("chevron_down" if opened else "chevron_right", "TEXT_3", 12))
        name.setToolTip(self._group_tooltip(group))
        self.table.setItem(row, 1, name)

        # Подсказки на «широких» колонках обязательны: ширины фиксированы, и
        # длинное имя модели или набора Qt обрежет многоточием.
        model_item = QTableWidgetItem(group.model or "—")
        model_item.setToolTip(group.model or "—")
        self.table.setItem(row, 2, model_item)

        set_item = QTableWidgetItem(group.set_label or "—")
        set_item.setToolTip(", ".join(group.set_ids) or "—")
        self.table.setItem(row, 3, set_item)

        score = QTableWidgetItem()
        if group.scored:
            share = group.passed / group.counted
            kind = "pass" if share >= 0.9 else ("warn" if share >= 0.7 else "fail")
            score.setData(Qt.UserRole, (group.passed, group.counted, kind))
            score.setToolTip(
                "%d из %d пройдено · %s" % (group.passed, group.counted, group.set_label)
            )
        elif group.total:
            score.setToolTip("Наборы без проверок — смотри метрики в отчёте")
        self.table.setItem(row, 4, score)

        seconds = QTableWidgetItem(group.seconds_text)
        seconds.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 5, seconds)

        date = QTableWidgetItem(group.date_text)
        date.setFont(theme.mono_font(9))
        self.table.setItem(row, 6, date)

        status = QTableWidgetItem(group.status_ru)
        status.setForeground(QColor(_group_status_color(group)))
        status.setToolTip(group.status_hint)
        self.table.setItem(row, 7, status)

        action = self._group_action(group)
        self._row_widgets.append(action)
        self.table.setCellWidget(row, 8, action)
        self.table.setRowHeight(row, theme.ROW_H)

    @staticmethod
    def _group_tooltip(group: RunGroup) -> str:
        """Тултип строки прогона: из чего он собран и почему помечен «≈»."""
        lines = []
        if group.batch_id:
            lines.append("Прогон: %s" % group.batch_id)
        else:
            lines.append("Точного идентификатора прогона нет")
        lines.append(
            "%d %s: %s"
            % (
                len(group.runs),
                theme.plural(len(group.runs), "набор", "набора", "наборов"),
                ", ".join(group.set_ids) or "—",
            )
        )
        lines.append("Кейсов: %d · файлов: %d" % (group.cases_count, len(group.runs)))
        if group.cases_count > CASE_PREVIEW_LIMIT:
            lines.append(
                "В таблице показаны первые %d кейсов, остальные — в отчёте." % CASE_PREVIEW_LIMIT
            )
        if group.inferred:
            lines.append(
                "Группа собрана по времени старта: файлы записаны до появления "
                "идентификатора прогона, и точную принадлежность по ним не "
                "восстановить."
            )
        lines.append(
            "Двойной клик по прогону — HTML-отчёт, по кейсу — карточка "
            "кейса; треугольник слева — разворот на кейсы."
        )
        return "\n".join(lines)

    def _group_check_state(self, group: RunGroup):
        ids = set(group.run_ids)
        if ids and ids <= self._checked:
            return Qt.Checked
        if ids & self._checked:
            return Qt.PartiallyChecked
        return Qt.Unchecked

    def _group_action(self, group: RunGroup) -> QWidget:
        """Кнопка «открыть отчёт» — одна на строку, вместо колонки со значком."""
        btn = QPushButton()
        btn.setProperty("size", "icon")
        btn.setIcon(icons.icon("external", "ACCENT_HI", 14))
        btn.setToolTip("Открыть HTML-отчёт прогона")
        btn.setEnabled(any(p.is_file() for p in group.paths))
        btn.clicked.connect(lambda _=False, g=group: self._open_group(g))
        return btn

    # --- строка кейса ---------------------------------------------------

    def _fill_case_row(self, row: int, group: RunGroup, rec: RunRecord, case: dict) -> None:
        meta = QTableWidgetItem()
        meta.setFlags(Qt.ItemIsEnabled)
        meta.setData(Qt.UserRole, ("case", rec, case))
        self.table.setItem(row, 0, meta)

        case_id = QTableWidgetItem(CASE_INDENT + str(case.get("case_id") or "—"))
        case_id.setFont(theme.mono_font(9))
        case_id.setForeground(QColor(theme.TEXT_3))
        case_id.setToolTip("Прогон: %s\nНабор: %s" % (rec.run_id, rec.set_id or "—"))
        self.table.setItem(row, 1, case_id)

        name_item = QTableWidgetItem(str(case.get("name") or "—"))
        name_item.setToolTip(str(case.get("name") or "—"))
        self.table.setItem(row, 2, name_item)

        # Набор в строке кейса нужен только тогда, когда в партии их несколько:
        # при одном наборе колонка повторяла бы строку прогона.
        multi = len(group.set_ids) > 1
        set_item = QTableWidgetItem(rec.set_id if multi else "")
        if multi:
            set_item.setToolTip(rec.set_name or rec.set_id)
        self.table.setItem(row, 3, set_item)

        cls, label = report.verdict(case)
        verdict = QTableWidgetItem()
        verdict.setData(Qt.UserRole, {"verdict": cls, "label": label})
        verdict.setToolTip(str(case.get("reason") or label))
        self.table.setItem(row, 4, verdict)

        total_ms = float(case.get("total_ms") or 0.0)
        seconds = QTableWidgetItem(seconds_text(total_ms / 1000.0) if total_ms > 0 else "—")
        seconds.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 5, seconds)

        self.table.setItem(row, 6, QTableWidgetItem(""))

        reason = str(case.get("reason") or "")
        reason_item = QTableWidgetItem(_clip(reason, 90) or label)
        reason_item.setForeground(QColor(_verdict_color(cls)))
        reason_item.setToolTip(reason or label)
        self.table.setItem(row, 7, reason_item)

        action = self._case_action(rec, case)
        self._row_widgets.append(action)
        self.table.setCellWidget(row, 8, action)
        self.table.setRowHeight(row, CASE_ROW_H)

    def _case_action(self, rec: RunRecord, case: dict) -> QWidget:
        btn = QPushButton()
        btn.setProperty("size", "icon")
        btn.setIcon(icons.icon("grid", "ACCENT_HI", 14))
        btn.setToolTip("Открыть карточку кейса: задание, ответ, рассуждение, разбор проверки")
        btn.clicked.connect(lambda _=False, r=rec, c=case: self._open_case(r, c))
        return btn

    def _fill_more_row(self, row: int, group: RunGroup, hidden: int) -> None:
        """Строка-подсказка под предпросмотром кейсов.

        Кейсов в прогоне бывает под сотню, и показывать их все сразу незачем:
        нужен ответ «сколько их всего и где остальные». Двойной клик открывает
        отчёт прогона со всеми кейсами — тот же, что и по строке прогона.
        """
        meta = QTableWidgetItem()
        meta.setFlags(Qt.ItemIsEnabled)
        meta.setData(Qt.UserRole, ("more", group, None))
        self.table.setItem(row, 0, meta)

        item = QTableWidgetItem(
            "%sещё %d %s — двойной клик откроет отчёт"
            % (CASE_INDENT, hidden, theme.plural(hidden, "кейс", "кейса", "кейсов"))
        )
        item.setFont(theme.mono_font(9))
        item.setForeground(QColor(theme.TEXT_3))
        item.setToolTip(
            "В прогоне %d %s, в таблице показаны первые %d.\n"
            "Полный список — в HTML-отчёте прогона."
            % (
                group.cases_count,
                theme.plural(group.cases_count, "кейс", "кейса", "кейсов"),
                CASE_PREVIEW_LIMIT,
            )
        )
        self.table.setItem(row, 1, item)

        # Подсказка тянется на три колонки. В одной «Прогон» (200 px) она
        # обрезалась на «двойн…» — то есть ровно там, где сказано, что делать.
        self.table.setSpan(row, 1, 1, 3)
        self.table.setRowHeight(row, CASE_ROW_H)

    def _rebuild_pager(self, pages: int) -> None:
        while self._pager_lay.count():
            item = self._pager_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.page_buttons = []

        # Окно из семи страниц вокруг текущей: список страниц не растёт вместе
        # с числом прогонов и не ломает подвал.
        first = max(1, min(self._page - 3, pages - 6))
        last = min(pages, first + 6)
        for n in range(first, last + 1):
            btn = QPushButton(str(n))
            btn.setProperty("size", "sm")
            btn.setFixedWidth(32)
            if n == self._page:
                btn.setProperty("accent", True)
            else:
                btn.setProperty("flat", True)
            btn.clicked.connect(lambda _=False, p=n: self._goto(p))
            self._pager_lay.addWidget(btn)
            self.page_buttons.append(btn)

        self.prev_btn.setEnabled(self._page > 1)
        self.next_btn.setEnabled(self._page < pages)

    # ------------------------------------------------------------------
    # выделение и действия

    def _row_meta(self, row: int):
        """Разбор строки: `("group", группа, None)` или `("case", запись, кейс)`.

        Метаданные лежат в нулевом столбце у обоих видов строк. Номера строк
        нигде не запоминаются: разворот группы сдвигает всё, что ниже, и
        сохранённый индекс после него указывал бы на чужую строку.
        """
        item = self.table.item(row, 0)
        if item is None:
            return None
        meta = item.data(Qt.UserRole)
        return meta if isinstance(meta, tuple) and len(meta) == 3 else None

    def _sync_bulk_bar(self) -> None:
        """Полоса действий видна только при непустом выделении."""
        count = len(self._checked)
        self.bulk_bar.setVisible(count > 0)
        if count:
            groups = self._selected_groups()
            self.bulk_label.setText(
                "Выбрано: %d %s · %d %s"
                % (
                    len(groups),
                    theme.plural(len(groups), "прогон", "прогона", "прогонов"),
                    count,
                    theme.plural(count, "набор", "набора", "наборов"),
                )
            )
        # Сравнивать нужно **прогоны**, а не файлы: один прогон из семи наборов
        # даёт семь файлов, и по счёту файлов кнопка предлагала бы сравнить
        # прогон сам с собой.
        self.compare_btn.setEnabled(len(self._selected_groups()) >= 2)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Чекбокс строки прогона отмечает все её файлы.

        Выделение хранится по `run_id`, а не по ключам групп: сравнивать и
        удалять приходится файлы, и групповой ключ для этого бесполезен.
        """
        if item.column() != 0:
            return
        meta = item.data(Qt.UserRole)
        if not isinstance(meta, tuple) or meta[0] != "group":
            return
        group = meta[1]
        if item.checkState() == Qt.Unchecked:
            for rid in group.run_ids:
                self._checked.discard(rid)
        else:
            self._checked.update(group.run_ids)
        self._sync_bulk_bar()

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Разворот прогона на кейсы — клик по треугольнику (столбец 1).

        Двойной клик по треугольнику развернёт и тут же свернёт обратно: Qt на
        двойной клик присылает `clicked` дважды. Это безвредно — состояние
        возвращается к исходному, а отчёт на треугольнике не открывается.
        """
        if column != 1:
            return
        meta = self._row_meta(row)
        if meta is None or meta[0] != "group":
            return
        self._toggle_group(meta[1].key)

    def _toggle_group(self, key: str) -> None:
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self._render_page()

    def _on_double_click(self, row: int, column: int) -> None:
        """Двойной клик: по прогону — HTML-отчёт, по кейсу — карточка кейса.

        Диспетчер обязателен: раньше обработчик брал запись из нулевого
        столбца и открывал отчёт, а на строках кейсов там лежит не запись —
        отчёт открылся бы по чужому файлу.

        Строка «ещё N» ведёт в отчёт прогона: она и стоит там, чтобы за
        остальными кейсами было куда пойти.
        """
        meta = self._row_meta(row)
        if meta is None:
            return
        if meta[0] == "group":
            # На треугольнике (столбец 1) двойной клик ничего не открывает:
            # там уже сработал одиночный, и отчёт поверх разворота — не то,
            # чего ждёшь от клика по стрелке.
            if column != 1:
                self._open_group(meta[1])
        elif meta[0] == "case":
            self._open_case(meta[1], meta[2])
        elif meta[0] == "more":
            self._open_group(meta[1])

    def _check_all(self, checked: bool) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.table.blockSignals(False)
        if checked:
            for group in self._filtered:
                self._checked.update(group.run_ids)
        else:
            self._checked.clear()
        self._sync_bulk_bar()

    def _goto(self, page: int) -> None:
        self._page = page
        self._render_page()

    def _selected_groups(self) -> list[RunGroup]:
        """Прогоны, у которых отмечен хотя бы один файл.

        Считаются по группам, а не по `run_id`: выделение хранится файлами,
        но и сравнение, и подпись полосы говорят о прогонах.
        """
        return [g for g in self._groups if self._checked.intersection(g.run_ids)]

    def _selected_paths(self) -> list[str]:
        """Пути к JSON выбранных прогонов (для сравнения).

        Берутся из карты, собранной при чтении папки, а не склеиваются из
        `run_id`: у файла, переименованного руками, имя и `run_id` расходятся,
        и путь получился бы несуществующий.
        """
        return [
            str(self._path_by_run[rid])
            for rid in sorted(self._checked)
            if rid in self._path_by_run
        ]

    def _compare(self) -> None:
        """Отдать в сравнение прогоны целиком.

        Вкладка сравнения собирает файлы обратно в прогоны той же группировкой,
        поэтому важно отдать **все** файлы отмеченных прогонов: по одному
        набору из партии она не поймёт, что это была партия.
        """
        if len(self._selected_groups()) < 2:
            QMessageBox.information(
                self,
                "Сравнение",
                "Отметьте минимум два прогона. Сравниваются прогоны целиком, "
                "а не отдельные наборы: один прогон из семи наборов — это одна "
                "строка и один столбец в сравнении.",
            )
            return
        paths = self._selected_paths()
        if len(paths) < 2:
            QMessageBox.information(
                self, "Сравнение", "Не удалось найти файлы выбранных прогонов."
            )
            return
        self.compare_requested.emit(paths)

    def _delete_selected(self) -> None:
        run_ids = sorted(self._checked)
        if not run_ids:
            return
        names = "\n".join(run_ids[:10])
        more = "\n… и ещё %d" % (len(run_ids) - 10) if len(run_ids) > 10 else ""
        answer = QMessageBox.question(
            self,
            "Удаление результатов",
            "Удалить %d %s из истории?\n\n%s%s\n\n"
            "JSON-отчёты уйдут в корзину, записи — из базы."
            % (
                len(run_ids),
                theme.plural(len(run_ids), "набор", "набора", "наборов"),
                names,
                more,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        paths = [str(self._path_by_run[rid]) for rid in run_ids if rid in self._path_by_run]
        # В корзину, а не безвозвратно: это результаты многоминутных прогонов.
        deleted, failed = _send_to_trash(paths)
        # И из базы — чтобы не висели мёртвыми строками.
        try:
            db = DatabaseManager(self.cfg.db_path)
            for rid in run_ids:
                db.delete_run(rid)
        except Exception:  # noqa: BLE001
            pass
        self._checked.clear()
        self.refresh()
        if failed:
            QMessageBox.warning(
                self,
                "Удаление",
                "Удалено %d, не удалось %d:\n%s" % (deleted, len(failed), "\n".join(failed[:5])),
            )

    # ------------------------------------------------------------------
    # открытие отчётов и карточек

    def _open_case(self, rec: RunRecord, case: dict) -> None:
        """Карточка кейса в окне — вместо ухода в браузер за одним кейсом."""
        dialog = CaseCardDialog(rec, case, self)
        dialog.report_requested.connect(self._open_record)
        dialog.exec()

    def _open_record(self, rec: RunRecord) -> None:
        if not rec.path.is_file():
            QMessageBox.information(
                self, "Открыть отчёт", "Файла прогона больше нет:\n%s" % rec.path
            )
            return
        html_path = self._report_html_path([rec.path])
        if html_path is None:
            QMessageBox.warning(
                self,
                "Открыть отчёт",
                "Не удалось сформировать HTML-отчёт для %s" % rec.path.name,
            )
            return
        _open_in_browser(str(html_path))

    def _open_group(self, group: RunGroup) -> None:
        """Отчёт по всему прогону: все наборы партии в одном файле."""
        paths = [p for p in group.paths if p.is_file()]
        if not paths:
            QMessageBox.information(self, "Открыть отчёт", "Файлов этого прогона больше нет.")
            return
        html_path = self._report_html_path(paths, group)
        if html_path is None:
            QMessageBox.warning(
                self,
                "Открыть отчёт",
                "Не удалось сформировать HTML-отчёт по прогону %s"
                % (group.batch_id or group.short_id),
            )
            return
        _open_in_browser(str(html_path))

    def _report_html_path(self, json_paths, group: RunGroup | None = None) -> Path | None:
        """HTML в reports/<идентификатор>.html; если его нет — собрать из JSON.

        Имя файла для партии — её `batch_id`, поэтому у прогона из одного
        набора оно совпадает с прежним (по `run_id`), и уже собранные отчёты
        не переименовываются.

        Старые прогоны, сделанные до автоматической генерации, HTML-файла
        не имеют — тогда он создаётся по запросу, а не остаётся «мёртвой»
        строкой в истории.
        """
        paths = [Path(p) for p in json_paths]
        if not paths:
            return None
        stem = (group.batch_id or paths[0].stem) if group else paths[0].stem
        html_path = self.cfg.reports_path / (stem + ".html")
        if html_path.is_file():
            return html_path
        try:
            from ..report import write_report

            runs = []
            for path in paths:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    runs.append(data)
            if not runs:
                return None
            title = "Тестирование %s — %s" % (
                runs[0].get("model") or stem,
                (group.set_label if group else "")
                or runs[0].get("set_name")
                or runs[0].get("set_id")
                or "набор",
            )
            if group is not None and len(runs) > 1:
                title += " (%d %s, %d кейсов)" % (
                    len(runs),
                    theme.plural(len(runs), "набор", "набора", "наборов"),
                    group.cases_count,
                )
            write_report(runs, html_path, title)
            return html_path
        except (OSError, ValueError):
            return None


def _open_in_browser(path: str) -> None:
    """Открыть файл в системном браузере.

    Windows — через webbrowser (передаёт путь браузеру), на остальных
    платформах — xdg-open/open. Браузер открывается с file://-указателем.
    """
    import sys

    import webbrowser

    try:
        if sys.platform == "win32":
            webbrowser.open(Path(path).as_uri())
        else:
            webbrowser.open(path)
    except Exception:  # noqa: BLE001
        pass


def _send_to_trash(paths: list[str]) -> tuple[int, list[str]]:
    """Отправить файлы в корзину.

    Удалять результаты прогонов безвозвратно нельзя: это часы работы GPU,
    которые не восстановить. Windows — через оболочку (SHFileOperation с
    FOF_ALLOWUNDO), Linux — через gio/trash-put. Если ни одного способа нет,
    файл остаётся на месте, а не пропадает молча.
    """
    import sys

    if sys.platform == "win32":
        return _trash_windows(paths)

    import shutil
    import subprocess

    if shutil.which("gio"):
        cmd_prefix = ["gio", "trash"]
    elif shutil.which("trash-put"):
        cmd_prefix = ["trash-put"]
    else:
        cmd_prefix = []

    deleted = 0
    failed: list[str] = []
    for p in paths:
        try:
            if cmd_prefix:
                subprocess.run(cmd_prefix + [p], check=True, capture_output=True)
            else:
                Path(p).unlink()
            deleted += 1
        except Exception:  # noqa: BLE001
            failed.append(Path(p).name)
    return deleted, failed


def _trash_windows(paths: list[str]) -> tuple[int, list[str]]:
    """Windows-часть: корзина через shell32.SHFileOperationW."""
    import ctypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004

    deleted = 0
    failed: list[str] = []
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
    except (AttributeError, OSError):
        return 0, [Path(p).name for p in paths]

    for p in paths:
        op = _SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        # Двойной нуль — обязательный признак конца списка путей.
        op.pFrom = str(Path(p).resolve()) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        try:
            res = shell32.SHFileOperationW(ctypes.byref(op))
        except OSError:
            failed.append(Path(p).name)
            continue
        if res == 0 and not op.fAnyOperationsAborted:
            deleted += 1
        else:
            failed.append(Path(p).name)
    return deleted, failed


__all__ = ["HistoryTab"]
