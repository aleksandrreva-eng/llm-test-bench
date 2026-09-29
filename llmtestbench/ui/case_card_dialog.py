"""Карточка кейса: двойной клик по строке кейса в истории.

Показывает то же, что и HTML-отчёт, но не уводит в браузер: задание, ответ,
рассуждение модели, разбор проверки, ошибку, оценку судьи и метрики. Открывать
браузер ради одного кейса — это потеря контекста: фильтры истории, выделение и
позиция в таблице остаются в другом окне, и вернуться к ним можно только
закрыв вкладку.

Данные берутся **только** из JSON прогона (`RunRecord.raw`) — карточка обязана
показывать ровно то, что лежит в файле и в отчёте. Формулировки общие с отчётом
(`report.verdict`, `report.case_metrics_text`), чтобы отчёт и карточка не
разошлись словами об одном и том же кейсе.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import report
from ..results_index import RunRecord
from . import icons, theme
from .card import Badge, Card, Metric

#: Класс вердикта из `report.verdict` → вид метки.
_BADGE_KIND = {"pass": "pass", "fail": "fail", "stand": "warn", "skip": "mute"}

#: Класс вердикта → значение свойства `status`. Цвет по нему берёт общий стиль
#: (`QLabel[status=...]`), поэтому после смены темы перекрашивать нечего.
#: Словарь на уровне модуля тут был бы ошибкой: цвета он не хранит, но
#: `_VERDICT_COLOR` до него хранил и оставался в цветах темы на момент импорта.
_VERDICT_STATUS = {
    "pass": "OK",
    "fail": "FAIL",
    "stand": "WARN",
    "skip": "INFO",
}

#: Пределы высоты текстового блока. Ниже 64 px текст не читается, выше 300 px
#: один блок занимает весь экран и до остальных не добраться.
_MIN_BLOCK_H = 64
_MAX_BLOCK_H = 300


class CaseCardDialog(QDialog):
    """Карточка одного кейса одного прогона."""

    #: Просьба открыть HTML-отчёт прогона целиком (несёт `RunRecord`).
    report_requested = Signal(object)

    def __init__(self, record: RunRecord, case: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.record = record
        self.case = case
        self._editors: list[QPlainTextEdit] = []

        case_id = str(case.get("case_id") or "—")
        self.setWindowTitle("Кейс %s — %s" % (case_id, record.model or "прогон"))
        self.setMinimumSize(720, 520)
        self.resize(900, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(self._build_head())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_footer())

    # ------------------------------------------------------------------
    # построение

    def _build_head(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.id_label = QLabel(str(self.case.get("case_id") or "—"))
        self.id_label.setObjectName("caseId")
        self.id_label.setFont(theme.mono_font(11))
        top.addWidget(self.id_label)

        name = QLabel(str(self.case.get("name") or "—"))
        name.setProperty("role", "pageTitle")
        top.addWidget(name)
        top.addStretch(1)

        cls, label = report.verdict(self.case)
        top.addWidget(Badge(label, _BADGE_KIND.get(cls, "mute")))
        lay.addLayout(top)

        set_label = self.record.set_name or self.record.set_id or "набор"
        sub = QLabel(
            "%s · %s · %s · %s"
            % (
                self.record.model or "модель неизвестна",
                set_label,
                self.case.get("check_type") or "без проверки",
                self.record.date_text,
            )
        )
        sub.setProperty("role", "pageSub")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        return host

    def _build_body(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(10)

        self._text_section(
            lay,
            "Задание",
            self._text("prompt"),
            badge="%s токенов" % self.case.get("prompt_tokens", 0),
        )
        self._text_section(
            lay,
            "Ответ",
            self._text("answer"),
            badge="%s токенов" % self.case.get("answer_tokens", 0),
        )
        if self.case.get("reasoning"):
            self._text_section(
                lay,
                "Рассуждение модели",
                self._text("reasoning"),
                badge="%s токенов" % self.case.get("reasoning_tokens", 0),
                tone="muted",
            )
        self._verdict_section(lay)
        if self.case.get("error") or self.case.get("stand_error"):
            self._text_section(
                lay,
                "Ошибка",
                str(self.case.get("stand_error") or self.case.get("error")),
                tone="fail",
            )
        if self.case.get("judge_score") is not None or self.case.get("judge_error"):
            self._judge_section(lay)
        self._metrics_section(lay)

        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _build_footer(self) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        report_btn = QPushButton("Открыть отчёт прогона")
        report_btn.setProperty("flat", True)
        report_btn.setProperty("size", "sm")
        report_btn.setIcon(icons.icon("external", "ACCENT_HI", 13))
        report_btn.setToolTip("HTML-отчёт целиком: все наборы и все кейсы")
        report_btn.clicked.connect(self._ask_report)
        row.addWidget(report_btn)

        copy_btn = QPushButton("Копировать ответ")
        copy_btn.setProperty("flat", True)
        copy_btn.setProperty("size", "sm")
        copy_btn.setEnabled(bool(self.case.get("answer")))
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self._text("answer")))
        row.addWidget(copy_btn)

        row.addStretch(1)

        close_btn = QPushButton("Закрыть")
        close_btn.setProperty("size", "sm")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        return host

    # ------------------------------------------------------------------
    # секции

    def _make_editor(self, text: str, tone: str = "") -> QPlainTextEdit:
        """Текстовый блок карточки: только чтение, моноширинный.

        Моноширинный — потому что в ответах бывает JSON и код, а в отчёте те же
        блоки набраны `<pre>`. Шрифт ставится кодом, а не стилем: от него
        считается высота блока (`_fit_blocks`), и шрифт из QSS с `fontMetrics()`
        не совпал бы.

        `tone` — роль блока (`muted`, `fail`), а не цвет: цвет по свойству
        берёт общий стиль. Готовый цвет здесь означал бы, что после смены темы
        в открытой карточке остался бы цвет прежней.
        """
        edit = QPlainTextEdit()
        edit.setObjectName("caseText")
        edit.setReadOnly(True)
        edit.setFont(theme.mono_font(9))
        if tone:
            edit.setProperty("tone", tone)
        edit.setPlainText(text or "—")
        self._editors.append(edit)
        return edit

    def _text_section(
        self, layout: QVBoxLayout, title: str, text: str, badge: str = "", tone: str = ""
    ) -> None:
        """Секция с длинным текстом."""
        card = Card(title)
        if badge:
            card.set_badge(badge)
        card.add_body(self._make_editor(text, tone))
        layout.addWidget(card)

    def _verdict_section(self, layout: QVBoxLayout) -> None:
        """Разбор проверки: чем именно кейс признан пройденным или нет."""
        cls, label = report.verdict(self.case)
        lines = [
            "вердикт: %s" % label,
            "проверка: %s" % (self.case.get("check_type") or "—"),
            "причина остановки: %s" % (self.case.get("stop_reason") or "—"),
            "бюджет ответа: %s, с запасом на рассуждение: %s"
            % (self.case.get("max_tokens_answer", 0), self.case.get("max_tokens_effective", 0)),
        ]
        if self.case.get("empty"):
            lines.append("ответ пуст — см. причину ниже")
        reason = str(self.case.get("reason") or "").strip()
        if reason:
            lines += ["", reason]
        card = Card("Разбор проверки")
        card.set_badge(label)

        body = QLabel("\n".join(lines))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setProperty("role", "mono")
        body.setProperty("status", _VERDICT_STATUS.get(cls, "INFO"))
        card.add_body(body)
        layout.addWidget(card)

    def _judge_section(self, layout: QVBoxLayout) -> None:
        score = self.case.get("judge_score")
        badge = "%.1f/10" % score if isinstance(score, (int, float)) else "—"
        card = Card("Оценка судьи")
        card.set_badge(badge)

        bits = []
        if self.case.get("judge_error"):
            bits.append("сбой судьи: %s" % self.case["judge_error"])
        if self.case.get("judge_response"):
            bits.append("сырой ответ судьи:\n%s" % self.case["judge_response"])
        if not bits:
            bits.append("судья отработал без замечаний")

        edit = self._make_editor("\n\n".join(bits), tone="muted")
        card.add_body(edit)
        layout.addWidget(card)

    def _metrics_section(self, layout: QVBoxLayout) -> None:
        """Метрики: плитки «на глаз» и та же строка, что в HTML-отчёте.

        Строка внизу не дублирование ради дублирования: она собрана той же
        функцией, что и в отчёте, и если формулировка метрик когда-нибудь
        изменится, расхождение будет видно сразу.
        """
        card = Card("Метрики")
        tiles = QWidget()
        row = QHBoxLayout(tiles)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)

        approx = "≈" if self.case.get("speeds_estimated") else ""
        for key, value in (
            ("ВХОД, ТОК.", str(self.case.get("prompt_tokens", 0))),
            ("ПРЕФИЛЛ, МС", str(self.case.get("prompt_ms", 0))),
            ("ПРЕФИЛЛ, T/S", approx + str(self.case.get("prompt_tokens_per_sec", 0))),
            ("TTFT, МС", str(round(self.case.get("ttft_ms") or 0))),
            ("ВЫХОД, ТОК.", str(self.case.get("completion_tokens", 0))),
            ("РАССУЖД., ТОК.", str(self.case.get("reasoning_tokens", 0))),
            ("ГЕНЕРАЦИЯ, T/S", approx + str(self.case.get("tokens_per_sec", 0))),
            ("ВРЕМЯ, МС", str(round(self.case.get("total_ms") or 0))),
        ):
            row.addWidget(Metric(key, value))
        row.addStretch(1)
        card.add_body(tiles)

        line = QLabel(report.case_metrics_text(self.case))
        line.setWordWrap(True)
        line.setTextInteractionFlags(Qt.TextSelectableByMouse)
        line.setProperty("role", "hint")
        line.setFont(theme.mono_font(8))
        card.add_body(line)
        layout.addWidget(card)

    # ------------------------------------------------------------------
    # поведение

    def _text(self, key: str) -> str:
        """Значение поля кейса строкой. Пустое поле — пустая строка, не «None»."""
        value = self.case.get(key)
        return "" if value is None else str(value)

    def _ask_report(self) -> None:
        """Попросить родителя открыть отчёт прогона и закрыться.

        Сама карточка браузер не открывает: отчёт — дело вкладки истории, у неё
        есть путь к папке reports/ и знание, что делать, если HTML ещё не собран.
        """
        self.report_requested.emit(self.record)
        self.accept()

    def showEvent(self, event) -> None:  # noqa: N802 — Qt-имя
        """Подогнать высоту текстовых блоков под содержимое.

        `QPlainTextEdit` не умеет расти по тексту, а фиксированная высота
        врёт в обе стороны: короткий ответ оставляет пустое поле, длинный
        рассуждающий ответ занимает весь экран. Мерить приходится после
        показа — до него ширина поля ещё не известна.
        """
        super().showEvent(event)
        self._fit_blocks()

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt-имя
        super().resizeEvent(event)
        self._fit_blocks()

    def _fit_blocks(self) -> None:
        """Подогнать высоту текстовых блоков под содержимое.

        Считаем сами, а не спрашиваем документ: разметка `QPlainTextEdit`
        (`QPlainTextDocumentLayout`) не учитывает перенос по ширине, и
        `document().size().height()` возвращает высоту только по жёстким
        переводам строки — на длинном ответе без переносов это 64 px вместо
        трёхсот, то есть ответ просто не видно. Шрифт моноширинный, поэтому
        ширина символа постоянна и перенос считается честно.
        """
        for edit in self._editors:
            metrics = edit.fontMetrics()
            char_w = max(1, metrics.horizontalAdvance("0"))
            # −14 на рамку и внутренние отступы поля.
            width = max(240, edit.viewport().width() - 14)
            per_line = max(8, width // char_w)
            lines = 0
            for paragraph in edit.toPlainText().split("\n"):
                lines += max(1, -(-len(paragraph) // per_line))
            height = lines * metrics.lineSpacing() + 14
            edit.setFixedHeight(int(min(_MAX_BLOCK_H, max(_MIN_BLOCK_H, height))))
