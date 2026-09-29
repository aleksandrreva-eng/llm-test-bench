"""Карточка с шапкой — базовый блок всех экранов макета.

Заменяет `QGroupBox`: у него заголовок живёт в рамке и не может нести ни
счётчик справа, ни кнопки действий, а в утверждённом макете шапка есть у
каждого блока.

Карточка разделяет содержимое на три части: шапка (заголовок, метка, действия),
тело (прокручиваемое содержимое) и подвал (итоги и кнопки). Разделители —
тонкие линии, а не рамки: сама карточка отделена от фона яркостью.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import theme

HEADER_H = 38


class Card(QFrame):
    """Панель с шапкой, телом и подвалом.

    Цвета карточки живут в общей таблице стилей (`theme.py`, селекторы
    `#card`, `#cardHeader`, `#cardBody`, `#cardFooter`, `#cardTitle`,
    `#cardBadge`), а не в `setStyleSheet` при создании. Так их переписывает
    смена темы: виджетный стиль сильнее общего, и оставленный здесь цвет
    пережил бы переключение — проверено, карточка оставалась тёмной в светлой
    теме.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None, show_header: bool = True):
        super().__init__(parent)
        self.setObjectName("card")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- шапка ---
        self.header = QWidget()
        self.header.setObjectName("cardHeader")
        self.header.setFixedHeight(HEADER_H)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(12, 0, 8, 0)
        head.setSpacing(8)

        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        head.addWidget(self.title)

        self.badge = QLabel("")
        self.badge.setObjectName("cardBadge")
        self.badge.setVisible(False)
        head.addWidget(self.badge)

        head.addStretch(1)
        self._head_actions = head
        self.header.setVisible(show_header)
        root.addWidget(self.header)

        # --- тело ---
        self.body = QWidget()
        self.body.setObjectName("cardBody")
        # Селектор — по имени, а не по классу `QWidget`. Правило на классе
        # достаётся и всем потомкам тела: поля ввода и консоли внутри
        # карточки теряли свою рамку и фон и рисовались фоном карточки
        # (проверено пиксельно — так выглядели поиск по моделям, поля Host и
        # порта, «Команда запуска» и лог сервера).
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(10, 10, 10, 10)
        self._body_layout.setSpacing(8)
        root.addWidget(self.body, 1)

        # --- подвал ---
        self.footer = QWidget()
        self.footer.setObjectName("cardFooter")
        self._footer_layout = QHBoxLayout(self.footer)
        self._footer_layout.setContentsMargins(12, 7, 8, 7)
        self._footer_layout.setSpacing(6)
        self.footer.setVisible(False)
        root.addWidget(self.footer)

    # ------------------------------------------------------------------

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def set_badge(self, text: str) -> None:
        self.badge.setText(text or "")
        self.badge.setVisible(bool(text))

    def add_action(self, widget: QWidget) -> None:
        """Кнопка или поле в шапке, справа от заголовка."""
        self._head_actions.addWidget(widget)

    def add_body(self, widget: QWidget, stretch: int = 0) -> None:
        self._body_layout.addWidget(widget, stretch)

    def add_body_layout(self, layout, stretch: int = 0) -> None:
        self._body_layout.addLayout(layout, stretch)

    def set_body_margins(self, left: int, top: int, right: int, bottom: int) -> None:
        """Когда внутри таблица или список — отступы не нужны."""
        self._body_layout.setContentsMargins(left, top, right, bottom)

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    def add_footer(self, widget: QWidget, stretch: int = 0) -> None:
        self.footer.setVisible(True)
        self._footer_layout.addWidget(widget, stretch)

    def add_footer_stretch(self) -> None:
        self._footer_layout.addStretch(1)

    def footer_layout(self) -> QHBoxLayout:
        self.footer.setVisible(True)
        return self._footer_layout


def field(label: str, widget: QWidget, hint: str = "") -> QVBoxLayout:
    """Поле формы: подпись сверху, поле, необязательная подсказка снизу.

    Подпись синяя (`LABEL`) — как в макете: она не спорит с содержимым поля,
    но остаётся отличимой от подсказки, которая серая и мельче.
    """
    box = QVBoxLayout()
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)

    cap = QLabel(label)
    cap.setProperty("role", "fieldLabel")
    box.addWidget(cap)
    box.addWidget(widget)
    if hint:
        note = QLabel(hint)
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        box.addWidget(note)
    return box


class StatusDot(QLabel):
    """Цветной кружок состояния (готов / внимание / ошибка)."""

    def __init__(self, state: str = "idle", parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(9, 9)
        self._state = state
        self.set_state(state)

    def set_state(self, state: str) -> None:
        self._state = state
        colors = {
            "ok": theme.OK,
            "ready": theme.OK,
            "warn": theme.WARN,
            "starting": theme.WARN,
            "fail": theme.FAIL,
            "error": theme.FAIL,
            "idle": theme.TEXT_3,
            "stopped": theme.TEXT_3,
        }
        color = colors.get(state, theme.TEXT_3)
        self.setStyleSheet("QLabel { background: %s; border: none; border-radius: 4px; }" % color)
        self.setToolTip(state)

    def restyle_theme(self) -> None:
        """Перекраситься под новую тему: цвет здесь, а не в общем стиле."""
        self.set_state(self._state)


class Badge(QLabel):
    """Метка-счётчик: «13/15», «готово», «не гонялся».

    Цвет по смыслу, а не по оформлению: зелёный — пройдено, янтарный —
    частично, красный — провал. Так счёт читается без чтения числа.

    Палитра — метод, а не словарь на уровне класса: словарь посчитался бы
    один раз при импорте, и метки остались бы в цветах старой темы.
    """

    def __init__(self, text: str = "", kind: str = "mute", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self._kind = kind
        self.set_kind(kind)

    @staticmethod
    def _styles() -> dict[str, tuple[str, str]]:
        return {
            "pass": (theme.OK, theme.OK_DIM),
            "warn": (theme.WARN, theme.WARN_DIM),
            "fail": (theme.FAIL, theme.FAIL_DIM),
            "accent": (theme.LABEL, theme.ACCENT_DIM),
            "mute": (theme.TEXT_3, theme.SURFACE_3),
        }

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        styles = self._styles()
        fg, bg = styles.get(kind, styles["mute"])
        self.setStyleSheet(
            "QLabel { color: %s; background: %s; border: none;"
            " border-radius: 4px; padding: 1px 8px; font-size: 11px;"
            " font-weight: 600; }" % (fg, bg)
        )

    def restyle_theme(self) -> None:
        """Перечитать цвета метки под новую тему."""
        self.set_kind(self._kind)


def score_badge(passed: int, total: int, parent: QWidget | None = None) -> Badge:
    """Метка счёта прогона: цвет по доле пройденных кейсов."""
    if not total:
        return Badge("без проверок", "accent", parent)
    share = passed / total
    kind = "pass" if share >= 0.9 else ("warn" if share >= 0.7 else "fail")
    return Badge("%d/%d" % (passed, total), kind, parent)


class Metric(QWidget):
    """Плитка «ключ — значение»: 24px цифра под мелкой подписью.

    Цифра крупнее подписи намеренно: на экране метрик взгляд ищет значение,
    а не название.
    """

    def __init__(self, key: str, value: str = "—", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.key_label = QLabel(key)
        self.key_label.setProperty("role", "metricKey")
        lay.addWidget(self.key_label)

        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "metricValue")
        lay.addWidget(self.value_label)

    def set_value(self, text: str, status: str = "") -> None:
        self.value_label.setText(text)
        self.value_label.setProperty("status", status)
        theme.restyle(self.value_label)


class EmptyState(QWidget):
    """Заглушка «здесь пока пусто» с иконкой.

    Пустая таблица без объяснения читается как поломка; заглушка говорит,
    что именно нужно сделать, чтобы данные появились. Текст меняется на
    ходу: «прогонов нет вовсе» и «под фильтры ничего не попало» — разные
    положения, и советы в них разные.
    """

    def __init__(
        self,
        icon_name: str = "clock",
        title: str = "",
        text: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._icon_name = icon_name

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 40, 24, 40)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignCenter)

        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignCenter)
        self._draw_icon()
        lay.addWidget(self.pic)

        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "emptyTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title_label)

        self.text_label = QLabel(text)
        self.text_label.setProperty("role", "emptyText")
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setWordWrap(True)
        self.text_label.setVisible(bool(text))
        lay.addWidget(self.text_label)

    def _draw_icon(self) -> None:
        from . import icons

        self.pic.setPixmap(icons.pixmap(self._icon_name, "BORDER_STRONG", 34))

    def restyle_theme(self) -> None:
        """Перерисовать иконку: `QPixmap` помнит цвет на момент рисования."""
        self._draw_icon()

    def set_text(self, title: str, text: str = "") -> None:
        self.title_label.setText(title)
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))
