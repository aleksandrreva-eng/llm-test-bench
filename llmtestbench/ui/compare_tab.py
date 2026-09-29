"""Вкладка «Сравнение» (раздел 5.3 ТЗ, макет `Сравнение.svg`).

**Столбец таблицы — прогон, а не набор.** Раньше вкладка получала плоский
список JSON-файлов и делала колонку из каждого файла. Два прогона по семи
наборам давали четырнадцать колонок, подписанных одним и тем же именем модели,
и таблица отвечала на вопрос «чем chat_single отличается от speed», хотя
сравнивают модели. Теперь файлы собираются в прогоны той же группировкой, что
и в истории (`results_index.group_records`), и колонка — прогон целиком.

**Детализация — разворот строки.** Сводное «13/15» — это сумма по наборам, и по
нему не видно, какой набор провален. Строку можно развернуть: под ней
появляются подстроки по наборам, а двойной клик по подстроке открывает
покейсовое сравнение набора по всем прогонам (`SetCompareDialog`).

Графики нарисованы на QPainter, а не через matplotlib: две ломаные линии не
стоят лишней зависимости в сборке PyInstaller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..atomic_io import write_csv_atomic, write_text_atomic
from ..results_index import (
    RunGroup,
    RunStats,
    column_labels,
    group_records,
    parse_run_file,
    run_tooltip,
    seconds_text,
)
from . import icons, theme
from .theme import PALETTES, active_theme
from .card import Card, EmptyState
from .set_compare_dialog import SetCompareDialog

# Строки сводной таблицы: (ключ, подпись, лучшее значение, разворачивается).
# «лучше» = "max" | "min" | "" (не сравниваем). Разворот — подстроки по
# наборам: он есть только у строк, которые складываются из наборов.
ROWS: list[tuple[str, str, str, bool]] = [
    ("when", "Прогон", "", False),
    ("model", "Модель", "", False),
    ("size", "Размер модели", "", False),
    ("n_ctx", "Контекст", "max", False),
    ("sets", "Наборов", "", False),
    ("cases", "Кейсов", "", True),
    ("score", "Качество", "max", True),
    ("errors", "Сбои стенда", "min", True),
    ("empty", "Пустых ответов", "min", True),
    # Префилл идёт перед генерацией намеренно: на длинных контекстах модели
    # различаются именно им, а генерация у них держится примерно одинаково.
    ("prompt_tokens_per_sec", "Префилл (avg)", "max", True),
    ("tokens_per_sec", "Tokens/sec (avg)", "max", True),
    ("latency_ms", "Latency (avg)", "min", True),
    ("ttft_ms", "TTFT (avg)", "min", True),
    ("seconds", "Время прогона", "", True),
    ("versions", "Версии наборов", "", False),
    ("rank", "Итоговый рейтинг", "", False),
]

#: Строки, которые разворачиваются по наборам.
EXPANDABLE_ROWS = {key for key, _label, _better, expand in ROWS if expand}

#: Отступ подстроки набора. Пробелами, а не отступом в пикселях: Qt не умеет
#: отступ текста в ячейке, и подстрока иначе не отличалась бы от сводной.
SET_INDENT = "    "

#: Строка набора ниже строки метрики: набор — подпункт, а не равный.
SET_ROW_H = 24


#: Класс вердикта из `report.verdict` → цвет подписи (для диалога кейсов).
#: Цвета линий графика — в `theme.CHART_SERIES_ACTIVE`: на белом светлые тона
#: тёмной палитры не читаются, поэтому набор зависит от темы.


@dataclass
class Cell:
    """Значение ячейки: число для сравнения, текст для показа, подсказка.

    Число и текст разделены потому, что «лучшее значение» ищется по числу, а
    показывается «13/15 · 87%». Держать в ячейке строку и парсить её обратно
    при подсчёте рейтинга — верный способ однажды не распарсить.
    """

    value: float | None = None
    text: str = "—"
    tooltip: str = ""
    kind: str = ""


class LineChart(QWidget):
    """Простой линейный график с легендой."""

    def __init__(self, title: str, y_label: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.title = title
        self.y_label = y_label
        self.series: list[tuple[str, QColor, list[float]]] = []
        self._raw: list[tuple[str, list[float]]] = []
        self.x_labels: list[str] = []
        self.setMinimumHeight(200)

    def set_data(
        self,
        series: list[tuple[str, list[float]]],
        x_labels: list[str] | None = None,
    ) -> None:
        self._raw = [(name, list(values)) for name, values in series]
        self._paint_series()
        self.x_labels = list(x_labels or [])
        self.update()

    def _paint_series(self) -> None:
        """Разложить цвета по линиям — заново на каждую смену темы.

        `QColor` запоминает значение, поэтому держать его в `self.series` и
        перекрашивать при смене темы нельзя: цвета линий остались бы от
        прошлой. Храним сырые числа, а цвета берём здесь.
        """
        colors = theme.CHART_SERIES_ACTIVE
        self.series = [
            (name, QColor(colors[i % len(colors)]), list(values))
            for i, (name, values) in enumerate(self._raw)
        ]

    def restyle_theme(self) -> None:
        """Перекрасить линии под новую тему."""
        self._paint_series()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt-имя
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor(theme.BG))

        # заголовок
        p.setPen(QColor(theme.TEXT))
        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        p.drawText(12, 18, self.title)

        pad_l, pad_r, pad_t, pad_b = 56, 16, 34, 28
        plot = QRectF(pad_l, pad_t, max(1, w - pad_l - pad_r), max(1, h - pad_t - pad_b))

        if not self.series or not any(s[2] for s in self.series):
            p.setPen(QColor(theme.TEXT_MUTED))
            p.setFont(QFont())
            p.drawText(plot, Qt.AlignCenter, "нет данных для графика")
            p.end()
            return

        all_values = [v for _, _, vals in self.series for v in vals]
        vmin, vmax = min(all_values), max(all_values)
        if vmax == vmin:
            vmax = vmin + 1.0
        span = vmax - vmin
        vmin -= span * 0.1
        vmax += span * 0.1

        # сетка и подписи оси Y
        p.setFont(QFont("Segoe UI", 7))
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            p.setPen(QPen(QColor(theme.BORDER), 1, Qt.DotLine))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            value = vmax - (vmax - vmin) * i / 4
            p.setPen(QColor(theme.TEXT_3))
            p.drawText(
                QRectF(0, y - 8, pad_l - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                "%.0f" % value if abs(value) >= 10 else "%.2f" % value,
            )

        # оси
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        p.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        max_len = max(len(vals) for _, _, vals in self.series)

        def x_at(i: int) -> float:
            if max_len <= 1:
                return plot.center().x()
            return plot.left() + plot.width() * i / (max_len - 1)

        def y_at(v: float) -> float:
            return plot.bottom() - plot.height() * (v - vmin) / (vmax - vmin)

        # подписи оси X
        p.setPen(QColor(theme.TEXT_3))
        for i in range(max_len):
            label = self.x_labels[i] if i < len(self.x_labels) else str(i + 1)
            p.drawText(QRectF(x_at(i) - 30, plot.bottom() + 4, 60, 16), Qt.AlignCenter, label)

        # линии
        for _name, color, values in self.series:
            if not values:
                continue
            pen = QPen(color, 2)
            p.setPen(pen)
            pts = [QPointF(x_at(i), y_at(v)) for i, v in enumerate(values)]
            # strict=False: соседние точки — это сдвинутая на одну копия того
            # же списка, длины заведомо разные, и это здесь правильно.
            for a, b in zip(pts, pts[1:], strict=False):
                p.drawLine(a, b)
            p.setBrush(color)
            for pt in pts:
                p.drawEllipse(pt, 3, 3)

        # легенда
        lx = plot.left() + 6
        ly = plot.top() + 4
        p.setFont(QFont("Segoe UI", 7))
        for name, color, _ in self.series:
            p.setPen(QPen(color, 2))
            p.drawLine(QPointF(lx, ly), QPointF(lx + 12, ly))
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(lx + 16, ly + 3, name)
            lx += 16 + p.fontMetrics().horizontalAdvance(name) + 16
        p.end()


class CompareTab(QWidget):
    """Сводная таблица и графики по выбранным прогонам."""

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self._groups: list[RunGroup] = []
        self._expanded_rows: set[str] = set()
        self._only_diff = False
        self._charts_visible = True
        #: Есть ли расхождение версий наборов. Флаг, а не `isVisible()`: у
        #: скрытого окна видимость ложна у всего, и HTML-выгрузка потеряла бы
        #: предупреждение (та же ловушка, что со свёрнутым журналом прогона).
        self._version_conflict = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = Card("Сравнение прогонов")
        self.card = card
        card.add_action(self._build_toolbar())

        # Предупреждение о разных версиях наборов (п. 11.7 ТЗ). Скрыто, пока
        # расхождений нет: постоянная строка на экране — это шум.
        self.version_warning = QLabel("")
        self.version_warning.setObjectName("versionWarning")
        self.version_warning.setWordWrap(True)
        self.version_warning.setVisible(False)
        card.add_body(self.version_warning)

        self.table = self._build_table()
        card.add_body(self.table, 3)

        charts = QHBoxLayout()
        charts.setSpacing(10)
        self.tps_chart = LineChart("Tokens/sec по кейсам прогонов")
        self.lat_chart = LineChart("Latency по кейсам прогонов")
        charts.addWidget(self.tps_chart)
        charts.addWidget(self.lat_chart)
        self.charts_box = QWidget()
        self.charts_box.setLayout(charts)
        card.add_body(self.charts_box, 2)

        # Заглушка та же, что в «Истории»: иконка, заголовок и подсказка.
        # Голый серый абзац посреди пустой карточки читался как сломанный
        # экран, а не как «здесь появится сравнение».
        self.placeholder = EmptyState(
            "chart",
            "Сравнивать нечего",
            "Отметьте два прогона на экране «История» и нажмите «Сравнить "
            "выбранные». Готовый набор файлов открывается кнопкой «Открыть "
            "JSON…».",
        )
        card.add_body(self.placeholder, 1)

        root.addWidget(card, 1)
        self.set_empty(True)

    # ------------------------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        """Кнопки в шапке карточки: фильтр различий, графики, экспорт."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.diff_btn = QPushButton("Только различия")
        self.diff_btn.setProperty("chip", True)
        self.diff_btn.setCheckable(True)
        self.diff_btn.setToolTip("Скрыть строки, где все прогоны совпали")
        self.diff_btn.toggled.connect(self._toggle_diff)
        row.addWidget(self.diff_btn)

        self.charts_btn = QPushButton("Графики")
        self.charts_btn.setProperty("chip", True)
        self.charts_btn.setCheckable(True)
        self.charts_btn.setChecked(True)
        self.charts_btn.toggled.connect(self._toggle_charts)
        row.addWidget(self.charts_btn)

        self.open_btn = QPushButton("Открыть JSON…")
        self.open_btn.setProperty("size", "sm")
        self.open_btn.setIcon(icons.icon("folder", "TEXT_2", 13))
        self.open_btn.setToolTip(
            "Выбрать файлы прогонов вручную. Файлы одного запуска соберутся в один столбец"
        )
        self.open_btn.clicked.connect(self._open_files)
        row.addWidget(self.open_btn)

        self.export_html_btn = QPushButton("Экспорт в HTML")
        self.export_html_btn.setProperty("accent", True)
        self.export_html_btn.setProperty("size", "sm")
        self.export_html_btn.setIcon(icons.icon("external", "#ffffff", 13))
        self.export_html_btn.clicked.connect(self.export_html)
        row.addWidget(self.export_html_btn)

        self.export_csv_btn = QPushButton("Экспорт в CSV")
        self.export_csv_btn.setProperty("size", "sm")
        self.export_csv_btn.clicked.connect(self.export_csv)
        row.addWidget(self.export_csv_btn)

        self.print_btn = QPushButton()
        self.print_btn.setProperty("size", "icon")
        self.print_btn.setIcon(icons.icon("print", "TEXT_3", 14))
        self.print_btn.setToolTip("Отправить сравнение на печать")
        self.print_btn.clicked.connect(self._print)
        row.addWidget(self.print_btn)

        self.close_btn = QPushButton()
        self.close_btn.setProperty("size", "icon")
        self.close_btn.setIcon(icons.icon("close", "TEXT_3", 14))
        self.close_btn.setToolTip("Закрыть сравнение")
        self.close_btn.clicked.connect(lambda: self.set_empty(True))
        row.addWidget(self.close_btn)
        return host

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 1)
        table.setHorizontalHeaderLabels(["Параметр"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 210)
        # Клик по подписи разворачивает строку, двойной клик по значению
        # открывает кейсы набора. Двойной клик шлёт и одиночный, поэтому
        # разворот сработает по столбцу 0, а кейсы — по остальным.
        table.cellClicked.connect(self._on_cell_clicked)
        table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        return table

    # ------------------------------------------------------------------

    def set_empty(self, empty: bool) -> None:
        self.placeholder.setVisible(empty)
        self.table.setVisible(not empty)
        self.charts_box.setVisible(not empty and self._charts_visible)
        self.version_warning.setVisible(False)
        for btn in (
            self.diff_btn,
            self.charts_btn,
            self.export_html_btn,
            self.export_csv_btn,
            self.print_btn,
            self.close_btn,
        ):
            btn.setEnabled(not empty)
        if empty:
            self.card.set_badge("")
            self._groups = []
            self._expanded_rows = set()
            self._version_conflict = False
            self.table.clearSpans()
            self.table.setRowCount(0)
            self.table.setColumnCount(1)
            self.table.setHorizontalHeaderItem(0, QTableWidgetItem("Параметр"))

    def load(self, paths: list[str] | list[Path]) -> None:
        """Загрузить и показать сравнение по файлам прогонов.

        На входе — файлы, на выходе — прогоны: одно нажатие «Запустить» пишет
        столько файлов, сколько отмечено наборов, и колонка из каждого файла
        показывала бы наборы, а не модели. Группировка та же, что в истории,
        поэтому столбцы здесь ровно те прогоны, что отмечены там.
        """
        records = [parse_run_file(Path(p)) for p in paths]
        records = [r for r in records if not r.error]
        if not records:
            QMessageBox.warning(self, "Сравнение", "Не удалось прочитать выбранные файлы.")
            return
        groups = group_records(records)
        if not groups:
            QMessageBox.warning(self, "Сравнение", "Не удалось собрать прогоны из файлов.")
            return

        # Хронологический порядок: слева ранний прогон, справа поздний. Так
        # сравнение читается как «было — стало», а не как порядок выделения.
        groups.sort(key=lambda g: (g.started or g.finished or "", g.key))
        self._groups = groups
        self._expanded_rows = set()

        self.set_empty(False)
        self.card.set_badge(
            "%d %s" % (len(groups), theme.plural(len(groups), "прогон", "прогона", "прогонов"))
        )
        self._render_warning()
        self._render_table()
        self._render_charts()

    # ------------------------------------------------------------------
    # значения ячеек

    def restyle_theme(self) -> None:
        """Пересобрать таблицу под новую тему.

        Цвета ячеек стоят не в стиле, а в самих `QTableWidgetItem`:
        `setForeground` и `setBackground` запоминают значение, и общий стиль
        их не перепишет. Пересборка безопасна: таблица строится из
        `self._groups`, а развёрнутые строки берутся из `self._expanded`,
        поэтому состояние разворота сохраняется.
        """
        if self._groups:
            self._render_table()
        self._render_warning()

    def _run_cell(self, group: RunGroup, key: str) -> Cell:
        """Ячейка сводной строки — по прогону целиком."""
        if key == "when":
            # Дата и время прогона — первой строкой, а не в шапке: два прогона
            # одной модели иначе не различить, а в шапке столько текста не
            # помещается.
            return Cell(None, group.date_text, run_tooltip(group))
        if key == "model":
            return Cell(None, group.model or "—", group.model or "—")
        if key == "size":
            size = _model_size_bytes(group)
            return Cell(
                None,
                _fmt_size(size),
                "Размер файла модели на диске"
                if size
                else "Файл модели не найден по пути из прогона",
            )
        if key == "n_ctx":
            ctx = group.context_size
            return Cell(float(ctx) if ctx else None, theme.grouped(ctx) if ctx else "—")
        if key == "sets":
            return Cell(None, str(len(group.set_ids)), ", ".join(group.set_ids) or "—")
        if key == "versions":
            versions = sorted({v for v in group.set_versions.values() if v})
            text = ", ".join(versions) if versions else "—"
            return Cell(None, text, _versions_tooltip(group))
        if key == "rank":
            return Cell(None, "—")
        return self._stats_cell(group.stats, key)

    def _set_cell(self, stats: RunStats | None, key: str) -> Cell:
        """Ячейка подстроки набора. Набора в прогоне не было — прочерк."""
        if stats is None:
            return Cell(None, "—", "Этот набор в прогоне не гонялся")
        return self._stats_cell(stats, key)

    @staticmethod
    def _stats_cell(stats: RunStats, key: str) -> Cell:
        """Ячейка по счёту и средним: одна и та же для прогона и для набора."""
        if key == "cases":
            return Cell(float(stats.cases), str(stats.cases))
        if key == "score":
            if not stats.scored:
                return Cell(
                    None,
                    "без проверок",
                    "%d %s без проверок"
                    % (stats.cases, theme.plural(stats.cases, "кейс", "кейса", "кейсов")),
                )
            share = stats.score or 0.0
            return Cell(
                share,
                "%d/%d · %d%%" % (stats.passed, stats.counted, round(share * 100)),
                "%d из %d кейсов с проверкой" % (stats.passed, stats.counted),
            )
        if key == "errors":
            return Cell(
                float(stats.stand_errors),
                str(stats.stand_errors),
                "Кейсов, не доехавших до сервера: %d" % stats.stand_errors,
            )
        if key == "empty":
            return Cell(
                float(stats.empty_answers),
                str(stats.empty_answers),
                "Пустых ответов: %d" % stats.empty_answers,
            )
        if key in ("tokens_per_sec", "prompt_tokens_per_sec"):
            value = getattr(stats, key)
            if not value:
                return Cell(None, "—")
            label = (
                "Скорость обработки промпта"
                if key == "prompt_tokens_per_sec"
                else "Средняя скорость генерации"
            )
            tooltip = "%s: среднее по %d %s" % (
                label,
                stats.cases,
                theme.plural(stats.cases, "кейсу", "кейсам", "кейсам"),
            )
            if stats.speeds_estimated:
                tooltip += "\n≈ посчитано по своим замерам: сервер не прислал timings"
            return Cell(value, "%.1f" % value, tooltip)
        if key in ("latency_ms", "ttft_ms"):
            value = getattr(stats, key)
            if not value:
                return Cell(None, "—")
            label = "TTFT" if key == "ttft_ms" else "Полное время ответа"
            return Cell(value, "%.0f мс" % value, "%s: среднее по кейсам" % label)
        if key == "seconds":
            if not stats.seconds:
                return Cell(None, "—")
            return Cell(stats.seconds, seconds_text(stats.seconds), "Суммарное время прогона")
        return Cell(None, "—")

    # ------------------------------------------------------------------
    # отрисовка

    def _render_warning(self) -> None:
        """Предупреждение по п. 11.7 ТЗ: у прогонов разные версии наборов.

        Наличие расхождения запоминается флагом, а не спрашивается у
        `isVisible()`: `save_config` и выгрузка зовутся в том числе тогда,
        когда окно скрыто, и у скрытого окна `isVisible()` ложно у любого
        виджета — предупреждение молча пропало бы из HTML.
        """
        conflicts = self._version_conflicts()
        self._version_conflict = bool(conflicts)
        if not conflicts:
            self.version_warning.setVisible(False)
            return
        self.version_warning.setText(
            "⚠ Наборы различаются версиями — сравнивать по ним некорректно:\n%s"
            % "\n".join(conflicts)
        )
        self.version_warning.setVisible(True)

    def _version_conflicts(self) -> list[str]:
        """«набор: версия / версия» по тем наборам, где версии разошлись."""
        per_set: dict[str, set[str]] = {}
        for group in self._groups:
            for set_id, version in group.set_versions.items():
                per_set.setdefault(set_id, set()).add(version or "—")
        return [
            "%s: %s" % (set_id, " / ".join(sorted(versions)))
            for set_id, versions in sorted(per_set.items())
            if len(versions) > 1
        ]

    def _render_table(self) -> None:
        groups = self._groups
        # Всё, что нужно на каждую ячейку, считается один раз: разворот строки
        # перерисовывает таблицу целиком, и пересборка разбивки по наборам на
        # каждой ячейке сделала бы её квадратичной.
        self._set_index = [self._stats_by_set(g) for g in groups]
        self._visible_keys = self._diff_keys(groups)
        self._sets_order = self._set_order()

        best = self._best_values(groups)
        place = self._places(groups, best)

        self.table.clearSpans()
        self.table.setColumnCount(1 + len(groups))
        self._set_headers(groups)

        plan = self._row_plan()
        self.table.setRowCount(len(plan))
        for row, spec in enumerate(plan):
            if spec[0] == "main":
                self._fill_main_row(row, spec[1], spec[2], spec[3], best, place)
            else:
                self._fill_set_row(row, spec[1], spec[2], spec[3])

    def _diff_keys(self, groups: list[RunGroup]) -> list[str]:
        """Ключи строк к показу. «Только различия» прячет совпавшие.

        Сравниваются показанные тексты, а не числа: строка «Версии наборов» и
        «Модель» числом не выражаются вовсе, а различие по ним интересно ровно
        так же.
        """
        out = []
        for key, _label, _better, _expand in ROWS:
            if key == "rank":
                out.append(key)
                continue
            if self._only_diff:
                texts = {self._run_cell(g, key).text for g in groups}
                if len(texts) <= 1:
                    continue
            out.append(key)
        return out

    def _set_headers(self, groups: list[RunGroup]) -> None:
        """Шапка: «Параметр» и по колонке на прогон.

        Имя модели обрезается по длине: колонок бывает семь, каждой достаётся
        пара сотен пикселей, а `QHeaderView` длинный заголовок не обрезает
        многоточием, а рисует поверх соседней колонки — на снимке экрана это
        выглядело как сдвинутая шапка. Полное имя — в подсказке.
        """
        self.table.setHorizontalHeaderItem(0, QTableWidgetItem("Параметр"))
        header = self.table.horizontalHeader()
        labels = column_labels(groups)
        for col, (group, label) in enumerate(zip(groups, labels, strict=True), start=1):
            item = QTableWidgetItem(theme.elide(label))
            item.setToolTip("Прогон: %s\n%s" % (label, run_tooltip(group)))
            self.table.setHorizontalHeaderItem(col, item)
            header.setSectionResizeMode(col, QHeaderView.Stretch)

    def _best_values(self, groups: list[RunGroup]) -> dict[str, float]:
        """Лучшее значение по каждой строке с критерием.

        Если значения совпали у всех, «лучшего» нет: семь одинаковых ★ в строке
        «Контекст» не сообщают ничего, а шума дают на пол-таблицы. Ноль сбоев
        стенда у всех — это тоже не чья-то победа, а отсутствие различий.
        """
        best: dict[str, float] = {}
        for key, _label, better, _expand in ROWS:
            if not better:
                continue
            values = [self._run_cell(g, key).value for g in groups]
            nums = [v for v in values if isinstance(v, (int, float))]
            if not nums or min(nums) == max(nums):
                continue
            best[key] = max(nums) if better == "max" else min(nums)
        return best

    def _places(self, groups: list[RunGroup], best: dict[str, float]) -> dict[int, int]:
        """Места по числу «лучших» попаданий — как в макете."""
        rank_score = {i: 0 for i in range(len(groups))}
        for key, _label, better, _expand in ROWS:
            if not better or key not in best:
                continue
            for i, group in enumerate(groups):
                value = self._run_cell(group, key).value
                if isinstance(value, (int, float)) and float(value) == best[key]:
                    rank_score[i] += 1
        order = sorted(range(len(groups)), key=lambda i: -rank_score[i])
        return {idx: pos + 1 for pos, idx in enumerate(order)}

    def _row_plan(self) -> list[tuple]:
        """Плоский список строк: сводные и, под развёрнутыми, подстроки наборов.

        Номера строк нигде не запоминаются — разворот сдвигает всё, что ниже,
        поэтому ключ строки лежит в самой ячейке (`Qt.UserRole`).
        """
        plan: list[tuple] = []
        for key, label, better, _expand in ROWS:
            if key not in self._visible_keys:
                continue
            plan.append(("main", key, label, better))
            if key not in EXPANDABLE_ROWS or key not in self._expanded_rows:
                continue
            for set_id, set_name in self._sets_order:
                plan.append(("set", key, set_name or set_id, set_id))
        return plan

    def _set_order(self) -> list[tuple[str, str]]:
        """Наборы всех прогонов по порядку первого появления.

        Объединение, а не набор первого прогона: у двух моделей наборы могли
        гоняться разные, и пропущенный набор обязан быть виден прочерком, а не
        отсутствовать в таблице.
        """
        order: list[tuple[str, str]] = []
        seen: set[str] = set()
        for group in self._groups:
            for stats in group.set_stats:
                key = stats.set_id or stats.set_name
                if key in seen:
                    continue
                seen.add(key)
                order.append((stats.set_id, stats.set_name))
        return order

    def _stats_by_set(self, group: RunGroup) -> dict[str, RunStats]:
        return {st.set_id: st for st in group.set_stats}

    def _fill_main_row(
        self,
        row: int,
        key: str,
        label: str,
        better: str,
        best: dict[str, float],
        place: dict[int, int],
    ) -> None:
        head = QTableWidgetItem(label)
        head.setForeground(QColor(theme.LABEL))
        head.setData(Qt.UserRole, key)
        if key in EXPANDABLE_ROWS:
            head.setIcon(
                icons.icon(
                    "chevron_down" if key in self._expanded_rows else "chevron_right", "TEXT_3", 12
                )
            )
            head.setToolTip(
                "Клик — развернуть по наборам; двойной клик по значению — кейсы набора"
            )
        self.table.setItem(row, 0, head)

        for col, group in enumerate(self._groups, start=1):
            cell = self._run_cell(group, key)
            if key == "rank":
                item = QTableWidgetItem(
                    "%d место%s" % (place[col - 1], " 🏆" if place[col - 1] == 1 else "")
                )
                item.setForeground(QColor(theme.WARN))
                item.setFont(_bold())
            else:
                item = QTableWidgetItem(cell.text)
                item.setToolTip(cell.tooltip)
                # Plain data («Прогон», «Модель», «Наборов», «Время прогона»…)
                # never gets painted in _paint(). Without this they keep Qt's
                # default black — dark text on a dark cell in the HTML export,
                # and whatever Qt felt like in the app itself.
                item.setForeground(QColor(theme.TEXT))
                self._paint(item, cell, key, better, best)
            self.table.setItem(row, col, item)
        self.table.setRowHeight(row, theme.ROW_H)

    def _fill_set_row(self, row: int, key: str, label: str, set_id: str) -> None:
        head = QTableWidgetItem(SET_INDENT + label)
        head.setForeground(QColor(theme.TEXT_3))
        head.setData(Qt.UserRole, key)
        head.setData(Qt.UserRole + 1, set_id)
        head.setToolTip(
            "Набор %s. Двойной клик по значению — кейсы набора по "
            "всем прогонам" % (set_id or label)
        )
        self.table.setItem(row, 0, head)

        for col, stats_by_set in enumerate(self._set_index, start=1):
            stats = stats_by_set.get(set_id)
            cell = self._set_cell(stats, key)
            item = QTableWidgetItem(cell.text)
            item.setToolTip(cell.tooltip)
            if stats is not None and key == "score" and stats.scored:
                item.setForeground(QColor(_share_color(stats.score or 0.0)))
            elif stats is not None and key in ("errors", "empty"):
                item.setForeground(QColor(theme.FAIL if cell.value else theme.TEXT_3))
            else:
                item.setForeground(QColor(theme.TEXT_2))
            self.table.setItem(row, col, item)
        self.table.setRowHeight(row, SET_ROW_H)

    @staticmethod
    def _paint(
        item: QTableWidgetItem, cell: Cell, key: str, better: str, best: dict[str, float]
    ) -> None:
        """Раскрасить значение и пометить лучшее в строке."""
        value = cell.value
        if key == "score" and isinstance(value, (int, float)):
            item.setForeground(QColor(_share_color(float(value))))
        elif key in ("errors", "empty"):
            item.setForeground(QColor(theme.OK if not value else theme.FAIL))
        elif key in ("tokens_per_sec", "prompt_tokens_per_sec", "latency_ms", "ttft_ms"):
            item.setForeground(QColor(theme.TEXT))

        is_best = (
            bool(better)
            and key in best
            and isinstance(value, (int, float))
            and float(value) == best[key]
        )
        if is_best:
            item.setText(cell.text + " ★")
            item.setFont(_bold())
            # Заливка из палитры, а не готовый зелёный с прозрачностью.
            # Было `QColor(26, 77, 26, 128)`: на тёмном фоне половинная
            # прозрачность давала тёмно-зелёную ячейку, а на белом тот же
            # зелёный разбавлялся до мутного `#8ca68c` — цвет зависел от
            # темы, но знала об этом только константа в коде.
            item.setBackground(QColor(theme.OK_DIM))

    def _render_charts(self) -> None:
        """Две ломаные: по точке на кейс, по линии на прогон.

        Ось X — порядковый номер кейса в прогоне, а не название набора: у
        разных прогонов наборы идут разным порядком, и общая ось по наборам
        показывала бы линию там, где точек нет.
        """
        longest = max((len(_case_series(g, "tokens_per_sec")) for g in self._groups), default=0)
        labels = ["кейс %d" % (i + 1) for i in range(longest)]
        names = column_labels(self._groups)
        self.tps_chart.set_data(
            [
                (name, _case_series(g, "tokens_per_sec"))
                for name, g in zip(names, self._groups, strict=True)
            ],
            labels,
        )
        self.lat_chart.set_data(
            [
                (name, _case_series(g, "latency_ms"))
                for name, g in zip(names, self._groups, strict=True)
            ],
            labels,
        )

    # ------------------------------------------------------------------
    # поведение таблицы

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Клик по подписи сводной строки разворачивает её по наборам.

        Клик по подписи **подстроки** ничего не делает: разворачивать в ней
        нечего, а сворачивать родителя по клику в чужой строке — не то, чего
        ждёшь.
        """
        if column != 0 or self._row_set(row) is not None:
            return
        key = self._row_key(row)
        if key is None or key not in EXPANDABLE_ROWS:
            return
        if key in self._expanded_rows:
            self._expanded_rows.discard(key)
        else:
            self._expanded_rows.add(key)
        self._render_table()

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        """Двойной клик по значению подстроки — кейсы набора по прогонам.

        Не по значению сводной строки: «Качество 13/15» — это сумма по наборам,
        и открывать по ней кейсы нечего. Набор берётся из подписи строки, а не
        из номера: разворот сдвигает строки, и запомненный номер указал бы не
        туда.
        """
        if column == 0:
            return
        set_id = self._row_set(row)
        if set_id is None:
            return
        name = ""
        for group in self._groups:
            for stats in group.set_stats:
                if stats.set_id == set_id:
                    name = stats.set_name
                    break
            if name:
                break
        dialog = SetCompareDialog(set_id, name, self._groups, self.cfg, self)
        dialog.exec()

    def _row_key(self, row: int) -> str | None:
        """Ключ строки по её подписи. Номера строк не запоминаются.

        Разворот сдвигает всё, что ниже, поэтому хранить «развёрнута строка 5»
        нельзя — после разворота пятая строка другая. Читаем ключ из самой
        ячейки: он там и лежит.
        """
        item = self.table.item(row, 0)
        if item is None:
            return None
        key = item.data(Qt.UserRole)
        return str(key) if key else None

    def _row_set(self, row: int) -> str | None:
        """Идентификатор набора, если строка — подстрока набора."""
        item = self.table.item(row, 0)
        if item is None:
            return None
        meta = item.data(Qt.UserRole + 1)
        return str(meta) if meta else None

    # ------------------------------------------------------------------

    def _toggle_diff(self, checked: bool) -> None:
        self._only_diff = checked
        if self._groups:
            self._render_table()

    def _toggle_charts(self, checked: bool) -> None:
        self._charts_visible = checked
        self.charts_box.setVisible(checked and bool(self._groups))

    def _open_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите JSON-файлы прогонов",
            str(self.cfg.results_path),
            "JSON (*.json)",
        )
        if files:
            self.load(files)

    def _print(self) -> None:
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter

        printer = QPrinter(QPrinter.HighResolution)
        if QPrintDialog(printer, self).exec():
            QMessageBox.information(
                self,
                "Печать",
                "Печать сводной таблицы будет доступна после этапа отчётов.",
            )

    # ------------------------------------------------------------------

    def export_csv(self, path: str | None = None) -> None:
        """Выгрузка сводной таблицы в CSV.

        `path` можно задать снаружи — так выгрузку проверяет `check_compare.py`
        без диалога выбора файла: диалог в offscreen-режиме не открыть, а
        проверять нужно именно запись, а не факт вызова `QFileDialog`.
        """
        if not self._groups:
            return
        # Кнопка посылает `clicked(False)` — Qt подставляет флаг в `path`.
        # Превращаем любой не-строковый аргумент обратно в «диалог выбора»:
        # иначе кнопка тихо выходит и ничего не происходит.
        if path is not None and not isinstance(path, str):
            path = None
        interactive = path is None
        if interactive:
            default = str(self.cfg.reports_path / "comparison.csv")
            path, _ = QFileDialog.getSaveFileName(self, "Экспорт в CSV", default, "CSV (*.csv)")
        if not path:
            return
        try:
            rows: list[list[str]] = [self._header_texts()]
            for row in range(self.table.rowCount()):
                cells = []
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    cells.append(item.text() if item else "")
                rows.append(cells)
            # Разделитель «;» — так таблица открывается в русском Excel без
            # танцев с настройкой списков разделителей.
            write_csv_atomic(path, rows, delimiter=";")
        except OSError as exc:
            QMessageBox.warning(self, "Экспорт CSV", "Не удалось записать файл:\n%s" % exc)
            return
        if interactive:
            QMessageBox.information(self, "Экспорт CSV", "Сохранено:\n%s" % path)

    def export_html(self, path: str | None = None) -> None:
        """HTML-отчёт по сводной таблице (п. 3.7 ТЗ)."""
        if not self._groups:
            return
        # Кнопка посылает `clicked(False)` — Qt подставляет флаг в `path`.
        # Превращаем любой не-строковый аргумент обратно в «диалог выбора».
        if path is not None and not isinstance(path, str):
            path = None
        interactive = path is None
        if interactive:
            default = str(self.cfg.reports_path / "comparison.html")
            path, _ = QFileDialog.getSaveFileName(self, "Экспорт в HTML", default, "HTML (*.html)")
        if not path:
            return
        try:
            write_text_atomic(path, self._build_html())
        except OSError as exc:
            QMessageBox.warning(self, "Экспорт HTML", "Не удалось записать файл:\n%s" % exc)
            return
        if interactive:
            QMessageBox.information(self, "Экспорт HTML", "Сохранено:\n%s" % path)

    def _header_texts(self) -> list[str]:
        """Подписи столбцов из шапки таблицы — одна правда на все выгрузки."""
        out = []
        for col in range(self.table.columnCount()):
            item = self.table.horizontalHeaderItem(col)
            out.append(item.text() if item else "")
        return out

    def _build_html(self) -> str:
        """Сводная таблица в HTML.

        Шапка выгружается вместе с телом: без неё в файле остаются столбцы без
        подписей, и «какой это прогон» по таблице не понять.
        """
        heads = self._header_texts()
        head_html = "".join('<th style="text-align:left;">%s</th>' % text for text in heads)

        rows = []
        for row in range(self.table.rowCount()):
            cells = []
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                text = item.text() if item else ""
                color = item.foreground().color().name() if item else theme.TEXT_DIM
                tag = "th" if col == 0 else "td"
                style = "color:%s;" % color
                if item and item.font().bold():
                    style += "font-weight:600;"
                cells.append('<%s style="%s">%s</%s>' % (tag, style, text, tag))
            rows.append("<tr>%s</tr>" % "".join(cells))

        warning = ""
        if self._version_conflict:
            warning = '<p class="warn">%s</p>' % self.version_warning.text().replace("\n", "<br>")

        return (
            """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>LLM Test Bench — сравнение прогонов</title>
<style>
body { background:%(BG)s; color:%(TEXT)s; font-family:"Segoe UI",Arial,sans-serif; margin:24px; }
h1 { font-size:18px; color:%(TEXT)s; }
table { border-collapse:collapse; width:100%%; }
th, td { border-bottom:1px solid %(BORDER)s; padding:6px 10px; font-size:13px; text-align:left; }
th { background:%(SURFACE_2)s; color:%(TEXT_2)s; font-weight:600; border-bottom:2px solid %(BORDER)s; }
tr:last-child td { border-bottom:none; }
tr:nth-child(even) td { background:%(SURFACE)s; }
.warn { color:%(WARN)s; }
</style></head><body>
"""
            % {
                "BG": PALETTES[active_theme()]["BG"],
                "TEXT": PALETTES[active_theme()]["TEXT"],
                "BORDER": PALETTES[active_theme()]["BORDER"],
                "SURFACE": PALETTES[active_theme()]["SURFACE"],
                "SURFACE_2": PALETTES[active_theme()]["SURFACE_2"],
                "TEXT_2": PALETTES[active_theme()]["TEXT_2"],
                "WARN": PALETTES[active_theme()]["WARN"],
            }
        ) + (
            """<h1>LLM Test Bench — сравнение прогонов</h1>
<p>Столбец — прогон целиком, строка — метрика. Подстроки с отступом — наборы
внутри прогона.</p>
%s
<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>
<p>Сформировано: %s</p>
</body></html>"""
            % (warning, head_html, "".join(rows), _now())
        )


# ----------------------------------------------------------------------
# вспомогательное


def _bold() -> QFont:
    font = QFont()
    font.setBold(True)
    return font


def _share_color(share: float) -> str:
    """Цвет доли пройденных кейсов — тот же порог, что у метки в истории."""
    if share >= 0.9:
        return theme.OK
    if share >= 0.7:
        return theme.WARN
    return theme.FAIL


def _versions_tooltip(group: RunGroup) -> str:
    versions = group.set_versions
    if not versions:
        return "Версии наборов в прогоне не записаны"
    return "\n".join(
        "%s — %s" % (set_id, version or "—") for set_id, version in sorted(versions.items())
    )


def _case_series(group: RunGroup, key: str) -> list[float]:
    """Метрика каждого кейса прогона по порядку — точки для графика."""
    if key == "tokens_per_sec":
        field = "tokens_per_sec"
    else:
        field = "total_ms"
    values: list[float] = []
    for _rec, case in group.cases:
        value = case.get(field)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%d.%m.%Y %H:%M")


def _fmt_size(value) -> str:
    if not isinstance(value, (int, float)) or not value:
        return "—"
    return "%.2f GB" % (float(value) / (1024**3))


def _model_size_bytes(group: RunGroup) -> int | None:
    """Размер gguf-модели на диске (в байтах).

    «Размер модели» в сравнении — это размер модели, которую гоняли, а не
    небольшой JSON прогона. Путь к модели — model_path из файла прогона.
    """
    try:
        for rec in group.runs:
            raw = rec.raw if isinstance(rec.raw, dict) else {}
            model_path = raw.get("model_path")
            if not model_path:
                continue
            path = Path(model_path)
            if path.is_file():
                return path.stat().st_size
        return None
    except OSError:
        return None
