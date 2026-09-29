"""Рельс навигации по разделам (утверждённый макет, раздел 3 отчёта).

Раньше разделы переключались вкладками сверху. Вкладки забирали ширину у
рабочей области и конкурировали за внимание с её содержимым; рельс слева
отдаёт всю ширину таблицам и сворачивается до 60 px, когда она особенно нужна.

Состояние свёрнутости хранится в конфиге, поэтому окно открывается таким,
каким его оставили.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme

RAIL_WIDTH = 208
RAIL_WIDTH_COLLAPSED = 60


class _RailItem(QWidget):
    """Один пункт рельса: иконка, подпись, необязательная метка-счётчик.

    Собран из QLabel, а не из QPushButton, ради метки справа: у кнопки текст
    и счётчик слились бы в одну строку и разъехались по ширине.
    """

    activated = Signal(str)

    def __init__(self, key: str, label: str, icon_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        self._icon_name = icon_name
        self._on = False
        self._collapsed = False

        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(10)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setPixmap(icons.pixmap(icon_name, theme.TEXT_3, 16))
        row.addWidget(self.icon_label)

        self.text_label = QLabel(label)
        row.addWidget(self.text_label, 1)

        self.badge = QLabel("")
        self.badge.setObjectName("railBadge")
        self.badge.setVisible(False)
        row.addWidget(self.badge)

        self.setToolTip(label)
        self._apply_style()

    # ------------------------------------------------------------------

    def _apply_style(self) -> None:
        bg = theme.ACCENT_DIM if self._on else "transparent"
        fg = theme.TEXT if self._on else theme.TEXT_2
        left = theme.ACCENT if self._on else "transparent"
        weight = "600" if self._on else "400"
        self.setStyleSheet(
            "QWidget {{ background: %s; border-left: 3px solid %s;"
            " border-radius: 6px; }}" % (bg, left)
        )
        self.text_label.setStyleSheet(
            "QLabel { color: %s; font-weight: %s; background: transparent;"
            " border: none; }" % (fg, weight)
        )
        self.badge.setStyleSheet(
            "QLabel { color: %s; background: %s; border: none;"
            " border-radius: 9px; padding: 1px 7px; font-size: 11px; }"
            % (theme.TEXT_3, theme.SURFACE_3)
        )
        self.icon_label.setPixmap(
            icons.pixmap(self._icon_name, theme.ACCENT if self._on else theme.TEXT_3, 16)
        )

    def set_on(self, on: bool) -> None:
        self._on = bool(on)
        self._apply_style()

    def restyle_theme(self) -> None:
        """Перекраситься под новую тему.

        Иконка рельса — растровая (`setPixmap`), цвет в неё вписан при
        рисовании; `_apply_style` перерисовывает её вместе с остальным.
        """
        self._apply_style()

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.text_label.setVisible(not collapsed)
        self.badge.setVisible(not collapsed and bool(self.badge.text()))

    def set_badge(self, text: str) -> None:
        self.badge.setText(text or "")
        self.badge.setVisible(not self._collapsed and bool(text))

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt-имя
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.key)
        super().mousePressEvent(event)


class NavRail(QWidget):
    """Вертикальный рельс разделов."""

    current_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: dict[str, _RailItem] = {}
        self._order: list[str] = []
        self._current = ""
        self._collapsed = False

        self.setFixedWidth(RAIL_WIDTH)
        # Селектор по objectName, а не по имени Python-класса: Qt сопоставляет
        # селекторы с именами C++-классов, и «NavRail» в них не сработал бы.
        # Само правило — в общей таблице стилей, иначе смена темы его не тронет.
        self.setObjectName("navRail")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 12, 8, 8)
        root.setSpacing(2)

        self._list = QVBoxLayout()
        self._list.setSpacing(2)
        root.addLayout(self._list)
        root.addStretch(1)

        # переключатель сворачивания — внизу, как «служебный» пункт
        self.toggle_btn = QPushButton("Свернуть панель")
        self.toggle_btn.setProperty("flat", True)
        self.toggle_btn.setIcon(icons.icon("chevron_right", "TEXT_3", 14))
        self.toggle_btn.setToolTip("Свернуть панель до иконок (Ctrl+B)")
        self.toggle_btn.clicked.connect(self.toggle_collapsed)
        root.addWidget(self.toggle_btn)

    # ------------------------------------------------------------------

    def add_section(self, key: str, label: str, icon_name: str) -> _RailItem:
        item = _RailItem(key, label, icon_name, self)
        item.activated.connect(self.set_current)
        self._items[key] = item
        self._order.append(key)
        self._list.addWidget(item)
        return item

    def sections(self) -> list[str]:
        return list(self._order)

    def current(self) -> str:
        return self._current

    def set_current(self, key: str) -> None:
        if key not in self._items:
            return
        self._current = key
        for k, item in self._items.items():
            item.set_on(k == key)
        self.current_changed.emit(key)

    def set_badge(self, key: str, text: str) -> None:
        item = self._items.get(key)
        if item is not None:
            item.set_badge(text)

    # ------------------------------------------------------------------

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.setFixedWidth(RAIL_WIDTH_COLLAPSED if self._collapsed else RAIL_WIDTH)
        for item in self._items.values():
            item.set_collapsed(self._collapsed)
        # Иконку разворота отражаем зеркально: та же стрелка, но в другую сторону.
        pm = icons.pixmap("chevron_right", theme.TEXT_3, 14)
        if not self._collapsed:
            pm = pm.transformed(QTransform().scale(-1, 1))
        self.toggle_btn.setIcon(pm)
        self.toggle_btn.setText("" if self._collapsed else "Свернуть панель")
        self.toggle_btn.setToolTip(
            "Развернуть панель (Ctrl+B)"
            if self._collapsed
            else "Свернуть панель до иконок (Ctrl+B)"
        )

    def restyle_theme(self) -> None:
        """Перерисовать иконку разворота под новую тему.

        Иконка растровая и ещё отражена зеркально — живому движку иконок
        такое не поручить, поэтому просто повторяем сборку: цвет берётся
        заново, отражение сохраняется.
        """
        self.set_collapsed(self._collapsed)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)
