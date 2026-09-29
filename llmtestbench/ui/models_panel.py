"""Панель моделей (разделы 3.1 и 5.1 ТЗ, макет `docs/ui-prototype.html`).

Строка модели несёт то, по чему модель и выбирают: параметры, контекст,
квант и размер на диске. Раньше параметры и контекст лежали одной мелкой
серой строкой вперемешку с размером, и сравнивать модели между собой было
нельзя — а сравнивают именно по этим полям.

Сканирование идёт в отдельном потоке: чтение метаданных двадцатигигабайтной
модели не должно морозить интерфейс.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QWidget,
)

from ..config import AppConfig
from ..gguf_scanner import ModelInfo, scan_models
from . import icons, theme
from .card import Card

ROW_HEIGHT = 46
CHECK_SIZE = 15


class _ScanWorker(QObject):
    """Сканирование директории в фоне."""

    progress = Signal(int, int, str)
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, directory: str, recursive: bool, include_sidecars: bool):
        super().__init__()
        self._dir = directory
        self._recursive = recursive
        self._include_sidecars = include_sidecars

    def run(self) -> None:
        try:
            models = scan_models(
                self._dir,
                recursive=self._recursive,
                include_sidecars=self._include_sidecars,
                progress=lambda i, n, p: self.progress.emit(i, n, p.name),
            )
        except Exception as exc:  # noqa: BLE001 — поток не должен падать молча
            self.failed.emit(str(exc))
            return
        self.finished.emit(models)


class _ModelDelegate(QStyledItemDelegate):
    """Двухстрочная строка: имя, под ним параметры · контекст · квант, справа размер.

    Флажок рисуем сами, а не через `QStyle.PE_IndicatorCheckBox`: стиль Fusion
    нарисовал бы светлый квадрат, который на тёмной карточке выглядит чужеродно.
    """

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 — Qt-имя
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        model: ModelInfo = index.data(Qt.UserRole)
        if model is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.fillRect(rect, QColor(theme.ACCENT_DIM))
        elif hovered:
            painter.fillRect(rect, QColor(theme.SURFACE_2))

        # --- флажок ---
        check = index.data(Qt.CheckStateRole)
        box = QRect(
            rect.left() + 8,
            rect.top() + (rect.height() - CHECK_SIZE) // 2,
            CHECK_SIZE,
            CHECK_SIZE,
        )
        if model.sidecar:
            # Сайдкар нельзя выбрать моделью — флажка у него нет.
            painter.setPen(QColor(theme.BORDER_STRONG))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(box.left() + 3, box.center().y(), box.right() - 3, box.center().y())
        else:
            checked = check == Qt.Checked
            painter.setPen(QColor(theme.ACCENT if checked else theme.BORDER_STRONG))
            painter.setBrush(QColor(theme.ACCENT if checked else theme.BG))
            painter.drawRoundedRect(box, 3, 3)
            if checked:
                painter.drawPixmap(
                    box.left() + 2, box.top() + 2, icons.pixmap("check", "#ffffff", 11)
                )

        # --- размер справа: считаем заранее, чтобы знать, где резать имя ---
        size_text = model.size_human if model.size_bytes else ""
        mono = theme.mono_font(10)
        painter.setFont(mono)
        size_w = painter.fontMetrics().horizontalAdvance(size_text)

        text_left = box.right() + 11
        text_right = rect.right() - 10 - (size_w + 12 if size_text else 0)

        # --- имя ---
        name_font = QFont()
        name_font.setPointSizeF(9.5)
        name_font.setWeight(QFont.DemiBold)
        painter.setFont(name_font)
        painter.setPen(QColor(theme.FAIL if model.error else theme.TEXT))
        metrics = painter.fontMetrics()
        name = metrics.elidedText(model.file_name, Qt.ElideMiddle, max(20, text_right - text_left))
        painter.drawText(text_left, rect.top() + 19, name)

        # --- мета: параметры · контекст · квант ---
        meta_font = QFont()
        meta_font.setPointSizeF(8.5)
        painter.setFont(meta_font)
        painter.setPen(QColor(theme.TEXT_3))
        painter.drawText(
            text_left,
            rect.top() + 34,
            painter.fontMetrics().elidedText(
                self._meta(model), Qt.ElideRight, max(20, text_right - text_left)
            ),
        )

        # --- размер ---
        if size_text:
            painter.setFont(mono)
            painter.setPen(QColor(theme.TEXT_3))
            painter.drawText(
                QRect(rect.right() - 10 - size_w, rect.top(), size_w, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                size_text,
            )
        painter.restore()

    @staticmethod
    def _meta(model: ModelInfo) -> str:
        parts = []
        if model.params_human:
            parts.append(model.params_human)
        if model.n_ctx_train:
            parts.append("ctx %s" % theme.grouped(model.n_ctx_train))
        if model.quant:
            parts.append(model.quant)
        if model.sidecar:
            parts.append("сайдкар: %s" % model.sidecar)
        if model.error:
            parts.append("ошибка чтения")
        return "  ·  ".join(parts) or "—"


class ModelsPanel(Card):
    """Список моделей с поиском, фильтром и отметками."""

    selection_changed = Signal(list)  # список выбранных ModelInfo
    scan_finished = Signal(list)  # все найденные ModelInfo

    def __init__(self, cfg: AppConfig, parent: QWidget | None = None):
        super().__init__("Модели", parent)
        self.cfg = cfg
        self._models: list[ModelInfo] = []
        self._thread: QThread | None = None
        self._worker: _ScanWorker | None = None
        self._show_sidecars = False

        # --- шапка: счётчик и пересканирование ---
        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.setProperty("flat", True)
        self.scan_btn.setProperty("size", "sm")
        self.scan_btn.setIcon(icons.icon("refresh", "TEXT_3", 14))
        self.scan_btn.setToolTip("Перечитать папку и метаданные моделей (F5)")
        self.scan_btn.clicked.connect(self.rescan)
        self.add_action(self.scan_btn)

        # --- поиск ---
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Фильтр по имени…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.addAction(icons.icon("search", "TEXT_3", 14), QLineEdit.LeadingPosition)
        self.filter_edit.textChanged.connect(lambda _: self._refill())
        self.add_body(self.filter_edit)

        # --- фильтр: модели или служебные файлы ---
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.models_chip = QPushButton("Модели")
        self.sidecars_chip = QPushButton("Служебные")
        for chip, sidecars in ((self.models_chip, False), (self.sidecars_chip, True)):
            chip.setProperty("chip", True)
            chip.setCheckable(True)
            chip.clicked.connect(lambda _=False, s=sidecars: self._set_sidecars(s))
            chips.addWidget(chip)
        chips.addStretch(1)
        self.add_body_layout(chips)

        # --- папка ---
        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.folder_icon = QLabel()
        self._draw_folder_icon()
        path_row.addWidget(self.folder_icon)
        self.path_label = QLabel(cfg.models_dir or "папка не выбрана")
        self.path_label.setObjectName("modelsPath")
        path_row.addWidget(self.path_label, 1)
        change_btn = QPushButton("Сменить")
        change_btn.setProperty("flat", True)
        change_btn.clicked.connect(self._browse)
        path_row.addWidget(change_btn)
        self.add_body_layout(path_row)

        # --- список ---
        self.list = QListWidget()
        self.list.setObjectName("optList")
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_ModelDelegate(self.list))
        self.list.itemChanged.connect(self._on_item_changed)
        self.add_body(self.list, 1)

        # --- подвал ---
        self.count_label = QLabel("моделей не найдено")
        self.count_label.setProperty("role", "hint")
        self.add_footer(self.count_label, 1)

        self.all_btn = QPushButton("Все")
        self.all_btn.setProperty("flat", True)
        self.all_btn.setProperty("size", "sm")
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.add_footer(self.all_btn)

        self.none_btn = QPushButton("Снять")
        self.none_btn.setProperty("flat", True)
        self.none_btn.setProperty("size", "sm")
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        self.add_footer(self.none_btn)

        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        self.status.setWordWrap(True)
        self.add_body(self.status)

        self._set_sidecars(False, rescan=False)

    # ------------------------------------------------------------------
    # сканирование

    def _browse(self) -> None:
        start = self.cfg.models_dir or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Выберите папку с моделями", start)
        if not chosen:
            return
        self.cfg.models_dir = chosen
        self._update_path_label()
        self.rescan()

    def _draw_folder_icon(self) -> None:
        self.folder_icon.setPixmap(icons.pixmap("folder", "TEXT_3", 13))

    def restyle_theme(self) -> None:
        """Перерисовать значок папки: `QPixmap` помнит цвет при рисовании."""
        self._draw_folder_icon()

    def _update_path_label(self) -> None:
        self.path_label.setText(self.cfg.models_dir or "папка не выбрана")
        self.path_label.setToolTip(self.cfg.models_dir)

    def rescan(self) -> None:
        """Запустить сканирование в фоне."""
        directory = self.cfg.models_dir or ""
        if not directory or not Path(directory).is_dir():
            self.status.setText("Папка не выбрана или не существует.")
            return
        if self._thread is not None and self._thread.isRunning():
            return

        self.cfg.models_dir = directory
        self._update_path_label()
        self.scan_btn.setEnabled(False)
        self.status.setText("Сканирую…")

        self._thread = QThread(self)
        self._worker = _ScanWorker(directory, bool(self.cfg.recursive_scan), True)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_scanned)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.status.setText("Читаю метаданные: %d/%d — %s" % (done, total, name))

    def _on_scanned(self, models: list) -> None:
        self._models = list(models)
        self._stop_thread()
        self._refill()
        self.scan_finished.emit(self._models)

    def _on_failed(self, message: str) -> None:
        self._stop_thread()
        self.status.setText("Ошибка сканирования: %s" % message)
        QMessageBox.warning(self, "Сканирование", "Не удалось прочитать папку:\n%s" % message)

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None
        self.scan_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # список

    def _set_sidecars(self, sidecars: bool, rescan: bool = True) -> None:
        self._show_sidecars = bool(sidecars)
        for chip, active in ((self.models_chip, not sidecars), (self.sidecars_chip, sidecars)):
            chip.setChecked(active)
            chip.setProperty("on", active)
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        self._refill()

    def _visible_models(self) -> list[ModelInfo]:
        query = self.filter_edit.text().strip().lower()
        out = []
        for m in self._models:
            # Сравниваем именно булево: у обычной модели `sidecar` — пустая
            # строка, а `"" != False` в Python истинно, и проверка «в лоб»
            # выбрасывала из списка все модели подряд.
            if bool(m.sidecar) != self._show_sidecars:
                continue
            if (
                query
                and query not in m.file_name.lower()
                and query not in (m.display_name or "").lower()
            ):
                continue
            out.append(m)
        return out

    def _refill(self) -> None:
        """Перерисовать список, сохранив отметки выбранных моделей."""
        checked = {m.path for m in self.selected_models()}
        self.list.blockSignals(True)
        self.list.clear()
        for m in self._visible_models():
            item = QListWidgetItem()
            item.setData(Qt.UserRole, m)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if m.path in checked else Qt.Unchecked)
            item.setToolTip(self._item_tooltip(m))
            self.list.addItem(item)
        self.list.blockSignals(False)

        total_models = sum(1 for m in self._models if m.is_model)
        total_sidecars = sum(1 for m in self._models if m.sidecar)
        shown = len(self._visible_models())
        selected = len(self.selected_models())

        self.models_chip.setText("Модели %d" % total_models)
        self.sidecars_chip.setText("Служебные %d" % total_sidecars)
        self.set_badge("%d" % (total_sidecars if self._show_sidecars else total_models))

        if not self._models:
            self.count_label.setText("моделей не найдено")
        else:
            self.count_label.setText("показано %d · выбрано %d" % (shown, selected))
        if self._models:
            self.status.setText("")

    @staticmethod
    def _item_tooltip(m: ModelInfo) -> str:
        lines = [
            "Файл: %s" % m.file_name,
            "Путь: %s" % m.path,
            "Размер: %s" % (m.size_human if m.size_bytes else "—"),
        ]
        if m.name:
            lines.append("Имя в GGUF: %s" % m.name)
        if m.arch:
            lines.append("Архитектура: %s" % m.arch)
        if m.params_human:
            lines.append("Параметров: %s" % m.params_human)
        if m.n_ctx_train:
            lines.append("Контекст (обучение): %s" % theme.grouped(m.n_ctx_train))
        if m.n_layers:
            lines.append("Слоёв: %d" % m.n_layers)
        if m.quant:
            lines.append("Квантование: %s" % m.quant)
        if m.sidecar:
            lines.append("Это сайдкар (%s), не самостоятельная модель" % m.sidecar)
        if m.error:
            lines.append("Ошибка: %s" % m.error)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # выбор

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        model: ModelInfo = item.data(Qt.UserRole)
        if model is not None and model.sidecar:
            # Сайдкары выбирать нельзя — запустить их отдельно не получится.
            self.list.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.list.blockSignals(False)
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(
            "показано %d · выбрано %d" % (len(self._visible_models()), len(self.selected_models()))
        )
        self.selection_changed.emit(self.selected_models())

    def _set_all(self, checked: bool) -> None:
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            model: ModelInfo = item.data(Qt.UserRole)
            if model is not None and model.sidecar:
                item.setCheckState(Qt.Unchecked)
                continue
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.list.blockSignals(False)
        self._update_count()

    def selected_models(self) -> list[ModelInfo]:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() != Qt.Checked:
                continue
            model: ModelInfo = item.data(Qt.UserRole)
            if model is not None and model.is_model:
                out.append(model)
        return out

    def all_models(self) -> list[ModelInfo]:
        return list(self._models)

    def models_dir(self) -> str:
        return self.cfg.models_dir


__all__ = ["ModelsPanel"]
