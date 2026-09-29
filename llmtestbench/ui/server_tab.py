"""Раздел «Сервер» — лог процесса (раздел 3.4.5 ТЗ).

Отдельный экран с цветовой маркировкой, кнопками «Очистить»,
«Сохранить в файл» и «Автопрокрутка». Ротация лога — 10 МБ.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from ..atomic_io import write_text_atomic
from ..logging_setup import get_logger
from . import icons, theme
from .card import Card

log = get_logger(__name__)

MAX_LOG_BYTES = 10 * 1024 * 1024  # п. 3.4.5: ротация на 10 МБ


# уровни лога → цвет. Функция, а не словарь на уровне модуля: словарь
# посчитался бы при импорте и остался в цветах той темы, что была при запуске.
def _level_color(level: str) -> str:
    return {
        "INFO": theme.TEXT_2,
        "OK": theme.OK,
        "PASS": theme.OK,
        "WARN": theme.WARN,
        "FAIL": theme.FAIL,
        "ERROR": theme.FAIL,
        "DEBUG": theme.TEXT_3,
    }.get(str(level).upper(), theme.TEXT_2)


#: Столько же строк, сколько держит документ (`setMaximumBlockCount`): списки
#: расходятся, если хранить больше, — перерисовка показала бы старое.
MAX_LINES = 20000


class ServerLogTab(Card):
    """Лог сервера и выполнения."""

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__("Журнал сервера", parent)
        self.cfg = cfg
        self._buffer_bytes = 0
        # Строки держим отдельно от документа: цвет ставится форматом текста,
        # то есть значением, и при смене темы его надо проставить заново.
        self._lines: list[tuple[str, str]] = []

        self.autoscroll_cb = QCheckBox("Автопрокрутка")
        self.autoscroll_cb.setChecked(True)
        self.autoscroll_cb.setToolTip("Прокручивать журнал к последней строке")
        self.add_action(self.autoscroll_cb)

        self.clear_btn = QPushButton("Очистить")
        self.clear_btn.setProperty("flat", True)
        self.clear_btn.setProperty("size", "sm")
        self.clear_btn.setIcon(icons.icon("trash", "TEXT_3", 13))
        self.clear_btn.clicked.connect(self.clear)
        self.add_action(self.clear_btn)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setProperty("flat", True)
        self.save_btn.setProperty("size", "sm")
        self.save_btn.setIcon(icons.icon("copy", "TEXT_3", 13))
        self.save_btn.setToolTip("Сохранить журнал в файл logs/")
        self.save_btn.clicked.connect(self.save_to_file)
        self.add_action(self.save_btn)

        self.view = QPlainTextEdit()
        self.view.setObjectName("console")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setPlaceholderText(
            "Здесь появятся строки llama-server: загрузка модели, слоты, ошибки."
        )
        self.set_body_margins(10, 10, 10, 10)
        self.add_body(self.view, 1)

        self.status = QLabel("журнал пуст")
        self.status.setProperty("role", "hint")
        self.add_footer(self.status, 1)

    # ------------------------------------------------------------------

    def append(self, message: str, level: str = "INFO") -> None:
        """Добавить строку лога с меткой времени и цветом по уровню."""
        stamp = datetime.now().strftime("%H:%M:%S")
        line = "[%s] %-5s %s" % (stamp, level.upper(), message)
        self._lines.append((line, level.upper()))
        if len(self._lines) > MAX_LINES:
            del self._lines[: len(self._lines) - MAX_LINES]
        self._insert(line, level.upper())

        self._buffer_bytes += len(line.encode("utf-8"))
        if self._buffer_bytes > MAX_LOG_BYTES:
            self._rotate()

        if self.autoscroll_cb.isChecked():
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        self._update_status()

    def _insert(self, line: str, level: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_level_color(level)))
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(line + "\n", fmt)

    def _update_status(self) -> None:
        self.status.setText(
            "%d строк · %.1f КБ" % (self.view.blockCount(), self._buffer_bytes / 1024)
        )

    def restyle_theme(self) -> None:
        """Перерисовать журнал под новую тему.

        Цвет уровня — формат текста, а не правило стиля: вставленные строки
        помнят цвет той темы, при которой их добавили. На белом фоне серый
        INFO из тёмной палитры читался бы как погасший, поэтому журнал
        пересобирается из сохранённых строк.
        """
        scroll = self.view.verticalScrollBar().value()
        self.view.clear()
        for line, level in self._lines:
            self._insert(line, level)
        self.view.verticalScrollBar().setValue(scroll)
        self._update_status()

    def _rotate(self) -> None:
        """Старый лог — в файл, окно начинаем заново (п. 3.4.5)."""
        logs_dir = self.cfg.logs_path
        try:
            name = "server_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")
            write_text_atomic(logs_dir / name, self.view.toPlainText())
        except OSError as exc:
            log.warning("журнал сервера не выгружен в logs/: %s", exc)
        self.view.clear()
        self._lines.clear()
        self._buffer_bytes = 0
        self.append("журнал превысил 10 МБ и был выгружен в logs/", "WARN")

    def clear(self) -> None:
        self.view.clear()
        self._lines.clear()
        self._buffer_bytes = 0
        self.status.setText("журнал пуст")

    def save_to_file(self) -> None:
        logs_dir = self.cfg.logs_path
        default = str(logs_dir / ("server_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")))
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить журнал", default, "Журнал (*.log);;Все файлы (*)"
        )
        if not path:
            return
        try:
            write_text_atomic(path, self.view.toPlainText())
        except OSError as exc:
            self.append("не удалось сохранить журнал: %s" % exc, "ERROR")
            return
        self.append("журнал сохранён: %s" % path, "OK")


__all__ = ["ServerLogTab"]
