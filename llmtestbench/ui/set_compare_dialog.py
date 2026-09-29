"""Покейсовое сравнение одного набора по нескольким прогонам.

Открывается двойным кликом по подстроке набора в сводном сравнении. Сводка
отвечает «у кого счёт выше», а этот экран — «на каких именно кейсах разошлось»:
разница в один кейс из пятнадцати в сводке выглядит одинаково у всех, и по
«13/15 против 12/15» не понять, что именно у второй модели не получилось.

Строки — кейсы набора, столбцы — прогоны. Кейс, которого в прогоне не было,
стоит прочерком, а не пропущенной строкой: иначе «модель не гоняли на этом
кейсе» выглядело бы как «модель его прошла».

Формулировки вердикта и метрик берутся из `report` — те же, что в HTML-отчёте
и в карточке кейса: три экрана об одном кейсе не должны говорить разными
словами.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
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

from .. import report
from ..results_index import RunGroup, RunRecord, column_labels, run_tooltip
from . import theme
from .card import Badge
from .case_card_dialog import CaseCardDialog

#: Класс вердикта из `report.verdict` → знак и цвет ячейки.
_VERDICT_MARK = {"pass": "✔", "fail": "✘", "stand": "⚠", "skip": "–"}
_VERDICT_COLOR = {
    "pass": theme.OK,
    "fail": theme.FAIL,
    "stand": theme.WARN,
    "skip": theme.TEXT_3,
}

#: Подстрока набора в сводке — отступ пробелами: Qt не умеет отступ в ячейке.
CASE_ROW_H = 24


class SetCompareDialog(QDialog):
    """Кейсы одного набора в нескольких прогонах."""

    def __init__(
        self,
        set_id: str,
        set_name: str,
        groups: list[RunGroup],
        cfg=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.set_id = set_id
        self.set_name = set_name
        # Прогоны без этого набора отбрасываются: пять пустых столбцов «—»
        # не сообщают ничего, а место занимают. Кто именно не гонял набор,
        # сказано в подписи — это единственное, что тут стоит знать.
        self.groups = [g for g in groups if self._has_set(g)]
        self.skipped = [g for g in groups if not self._has_set(g)]
        self.cfg = cfg
        self._per_group = self._collect()

        self.setWindowTitle("Набор %s — кейсы по прогонам" % (set_name or set_id))
        self.setMinimumSize(680, 460)
        self.resize(940, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(self._build_head())
        root.addWidget(self._build_table(), 1)
        root.addWidget(self._build_footer())

    # ------------------------------------------------------------------

    def _has_set(self, group: RunGroup) -> bool:
        return any(stats.set_id == self.set_id for stats in group.set_stats)

    def _collect(self) -> list[dict[str, tuple[RunRecord, dict]]]:
        """Кейсы набора по прогонам: `case_id → (запись, кейс)`.

        Порядок кейсов задаёт первый прогон, у которого они есть: в остальных
        кейсы идут так же (набор один и тот же), а если порядок и разошёлся,
        строки всё равно сопоставляются по `case_id`, а не по позиции.
        """
        out: list[dict[str, tuple[RunRecord, dict]]] = []
        self._case_order: list[str] = []
        for group in self.groups:
            mapping: dict[str, tuple[RunRecord, dict]] = {}
            for rec, case in group.cases:
                if (rec.set_id or "") != self.set_id:
                    continue
                case_id = str(case.get("case_id") or "")
                if not case_id:
                    continue
                mapping.setdefault(case_id, (rec, case))
                if case_id not in self._case_order:
                    self._case_order.append(case_id)
            out.append(mapping)
        return out

    def _build_head(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        ident = QLabel(self.set_id or "набор")
        ident.setObjectName("setIdent")
        ident.setFont(theme.mono_font(11))
        top.addWidget(ident)

        title = QLabel(self.set_name or self.set_id or "набор")
        title.setProperty("role", "pageTitle")
        top.addWidget(title)
        top.addStretch(1)

        total = len(self._case_order)
        top.addWidget(
            Badge("%d %s" % (total, theme.plural(total, "кейс", "кейса", "кейсов")), "mute")
        )
        lay.addLayout(top)

        sub = QLabel(
            "Столбец — прогон, строка — кейс. Двойной клик по ячейке "
            "открывает карточку кейса: задание, ответ и разбор проверки."
        )
        sub.setProperty("role", "pageSub")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        if self.skipped:
            note = QLabel("Этот набор не гонялся: %s" % ", ".join(column_labels(self.skipped)))
            note.setProperty("role", "hint")
            note.setWordWrap(True)
            lay.addWidget(note)
        return host

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 1 + len(self.groups))
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(True)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 150)
        table.setHorizontalHeaderItem(0, QTableWidgetItem("Кейс"))

        labels = column_labels(self.groups)
        for col, (group, label) in enumerate(zip(self.groups, labels, strict=True), start=1):
            item = QTableWidgetItem(theme.elide(label))
            item.setToolTip("Прогон: %s\n%s" % (label, group.date_text))
            table.setHorizontalHeaderItem(col, item)
            header.setSectionResizeMode(col, QHeaderView.Stretch)

        table.cellDoubleClicked.connect(self._on_double_click)
        self.table = table
        self._fill(table)
        return table

    def _fill(self, table: QTableWidget) -> None:
        """Две итоговые строки, затем строка на каждый кейс набора.

        Первой идёт дата прогона: имя модели в шапке у двух прогонов одной
        модели совпадает, и без этой строки столбцы неотличимы.
        """
        table.setRowCount(2 + len(self._case_order))

        when = QTableWidgetItem("Прогон")
        when.setForeground(_color(theme.LABEL))
        when.setToolTip("Дата и время старта прогона")
        table.setItem(0, 0, when)
        for col, group in enumerate(self.groups, start=1):
            item = QTableWidgetItem(group.date_text)
            item.setFont(theme.mono_font(9))
            item.setToolTip(run_tooltip(group))
            table.setItem(0, col, item)
        table.setRowHeight(0, theme.ROW_H)

        head = QTableWidgetItem("Итого")
        head.setForeground(_color(theme.LABEL))
        head.setToolTip("Кейсы с проверкой: пройдено из всех")
        table.setItem(1, 0, head)

        totals = [self._set_stats(g) for g in self.groups]
        for col, stats in enumerate(totals, start=1):
            if stats is None or not stats.scored:
                text, color = "без проверок", theme.TEXT_3
            else:
                text = "%d/%d · %d%%" % (
                    stats.passed,
                    stats.counted,
                    round((stats.score or 0) * 100),
                )
                color = _share_color(stats.score or 0.0)
            item = QTableWidgetItem(text)
            item.setForeground(_color(color))
            item.setFont(_bold())
            table.setItem(1, col, item)
        table.setRowHeight(1, theme.ROW_H)

        for row, case_id in enumerate(self._case_order, start=2):
            name_item = QTableWidgetItem(case_id)
            name_item.setFont(theme.mono_font(9))
            name_item.setForeground(_color(theme.TEXT_2))
            table.setItem(row, 0, name_item)
            for col, mapping in enumerate(self._per_group, start=1):
                table.setItem(row, col, self._case_item(mapping.get(case_id)))
            table.setRowHeight(row, CASE_ROW_H)

    def _set_stats(self, group: RunGroup):
        for stats in group.set_stats:
            if stats.set_id == self.set_id:
                return stats
        return None

    @staticmethod
    def _case_item(entry: tuple[RunRecord, dict] | None) -> QTableWidgetItem:
        """Ячейка «кейс × прогон»: знак вердикта и полный разбор в подсказке."""
        if entry is None:
            item = QTableWidgetItem("—")
            item.setForeground(_color(theme.TEXT_3))
            item.setToolTip("Этого кейса в прогоне не было")
            return item

        rec, case = entry
        cls, label = report.verdict(case)
        item = QTableWidgetItem(_VERDICT_MARK.get(cls, "?"))
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(_color(_VERDICT_COLOR.get(cls, theme.TEXT_3)))
        item.setData(Qt.UserRole, entry)

        lines = [
            "%s · %s" % (case.get("case_id") or "—", case.get("name") or "—"),
            "вердикт: %s" % label,
            "проверка: %s" % (case.get("check_type") or "—"),
            "прогон: %s" % rec.run_id,
            report.case_metrics_text(case),
        ]
        reason = str(case.get("reason") or "").strip()
        if reason:
            lines += ["", reason]
        item.setToolTip("\n".join(lines))
        return item

    def _build_footer(self) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        legend = QLabel("✔ зачтён · ✘ провал · ⚠ сбой стенда · – пропуск · — кейса не было")
        legend.setProperty("role", "hint")
        row.addWidget(legend)
        row.addStretch(1)

        close_btn = QPushButton("Закрыть")
        close_btn.setProperty("size", "sm")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        return host

    # ------------------------------------------------------------------

    def _on_double_click(self, row: int, column: int) -> None:
        """Карточка кейса — как в истории, а не уход в браузер за одним кейсом."""
        if column == 0 or row == 0:
            return
        item = self.table.item(row, column)
        entry = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(entry, tuple) or len(entry) != 2:
            return
        rec, case = entry
        dialog = CaseCardDialog(rec, case, self)
        dialog.report_requested.connect(self._open_report)
        dialog.exec()

    def _open_report(self, rec: RunRecord) -> None:
        """HTML-отчёт прогона: собрать, если его ещё нет, и открыть в браузере.

        Путь и правило те же, что в истории (`reports/<идентификатор>.html`),
        поэтому уже собранный отчёт не пересобирается и не дублируется.
        """
        from .history_tab import _open_in_browser

        if not rec.path.is_file():
            QMessageBox.information(
                self, "Открыть отчёт", "Файла прогона больше нет:\n%s" % rec.path
            )
            return
        if self.cfg is None:
            return
        html_path = self.cfg.reports_path / (rec.path.stem + ".html")
        try:
            if not html_path.is_file():
                report.write_report(
                    [rec.raw],
                    html_path,
                    "Тестирование %s — %s"
                    % (rec.model or rec.path.stem, rec.set_name or rec.set_id or "набор"),
                )
        except (OSError, ValueError):
            QMessageBox.warning(
                self, "Открыть отчёт", "Не удалось сформировать HTML-отчёт для %s" % rec.path.name
            )
            return
        _open_in_browser(str(html_path))


def _bold() -> QFont:
    font = QFont()
    font.setBold(True)
    return font


def _share_color(share: float) -> str:
    """Цвет доли пройденных кейсов — тот же порог, что в сводном сравнении."""
    if share >= 0.9:
        return theme.OK
    if share >= 0.7:
        return theme.WARN
    return theme.FAIL


def _color(value: str) -> QColor:
    """`QColor` из строки темы — короче, чем `QColor(theme.X)` в каждой строке."""
    return QColor(value)


__all__ = ["SetCompareDialog"]
