"""Иконки интерфейса, нарисованные кодом (без файлов-ресурсов).

Свои иконки вместо `QStyle.StandardPixmap` нужны по двум причинам: набор
стандартных иконок не совпадает с макетом (нужны колба, сервер, часы, столбики),
а класть в сборку .svg-файлы ради шести картинок — лишние данные в бандле.

Все фигуры рисуются в системе координат 16×16 и масштабируются под нужный
размер, поэтому иконка одинаково выглядит и в рельсе, и в подсказке.

**Цвет задаётся именем токена темы, а не значением.** `icons.icon("folder",
"TEXT_3")`, а не `theme.TEXT_3`: иконка запоминает имя и берёт цвет в момент
отрисовки. Иначе после смены темы у неё остался бы цвет прошлой — а таких
мест в коде около тридцати. Готовый «#rrggbb» тоже принимается: белая иконка
на акцентной кнопке от темы не зависит.

Растровые `pixmap()` так не умеют — они отдают готовую картинку. Их немного
(рельс, пустое состояние, значок папки), и владельцы перерисовывают их сами
в `restyle_theme()`.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QIconEngine,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from . import theme


def _pen(color: str, width: float = 1.5) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw(name: str, p: QPainter, color: str) -> None:
    """Нарисовать фигуру `name` в координатах 16×16."""
    p.setPen(_pen(color))
    p.setBrush(Qt.NoBrush)

    if name == "flask":
        p.drawLine(QPointF(5.6, 2.4), QPointF(10.4, 2.4))
        p.drawLine(QPointF(6.6, 2.4), QPointF(6.6, 7.2))
        p.drawLine(QPointF(9.4, 2.4), QPointF(9.4, 7.2))
        p.drawPolyline(
            [
                QPointF(6.6, 7.2),
                QPointF(3.4, 13.0),
                QPointF(12.6, 13.0),
                QPointF(9.4, 7.2),
            ]
        )

    elif name == "server":
        p.drawRoundedRect(QRectF(2.4, 3.0, 11.2, 4.2), 1.2, 1.2)
        p.drawRoundedRect(QRectF(2.4, 8.8, 11.2, 4.2), 1.2, 1.2)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(4.9, 5.1), 0.7, 0.7)
        p.drawEllipse(QPointF(4.9, 10.9), 0.7, 0.7)
        p.setBrush(Qt.NoBrush)

    elif name == "clock":
        p.drawEllipse(QPointF(8.0, 8.0), 6.0, 6.0)
        p.drawPolyline(
            [
                QPointF(8.0, 4.6),
                QPointF(8.0, 8.2),
                QPointF(10.6, 9.7),
            ]
        )

    elif name == "chart":
        p.drawLine(QPointF(2.2, 14.0), QPointF(13.8, 14.0))
        p.drawLine(QPointF(3.6, 13.0), QPointF(3.6, 8.6))
        p.drawLine(QPointF(8.0, 13.0), QPointF(8.0, 3.4))
        p.drawLine(QPointF(12.4, 13.0), QPointF(12.4, 6.6))

    elif name == "grid":
        for x, y in ((2.6, 2.6), (8.8, 2.6), (2.6, 8.8), (8.8, 8.8)):
            p.drawRoundedRect(QRectF(x, y, 4.6, 4.6), 1.0, 1.0)

    elif name == "chevron_right":
        p.drawPolyline([QPointF(6.2, 3.6), QPointF(10.6, 8.0), QPointF(6.2, 12.4)])

    elif name == "chevron_down":
        p.drawPolyline([QPointF(3.6, 6.2), QPointF(8.0, 10.6), QPointF(12.4, 6.2)])

    elif name == "search":
        p.drawEllipse(QPointF(7.2, 7.2), 4.4, 4.4)
        p.drawLine(QPointF(10.5, 10.5), QPointF(13.4, 13.4))

    elif name == "folder":
        path = QPainterPath()
        path.moveTo(1.9, 4.4)
        path.lineTo(6.0, 4.4)
        path.lineTo(7.3, 6.1)
        path.lineTo(14.1, 6.1)
        path.lineTo(14.1, 12.4)
        path.lineTo(1.9, 12.4)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "trash":
        p.drawLine(QPointF(2.8, 4.4), QPointF(13.2, 4.4))
        p.drawLine(QPointF(6.4, 4.4), QPointF(6.4, 2.9))
        p.drawLine(QPointF(6.4, 2.9), QPointF(9.6, 2.9))
        p.drawLine(QPointF(9.6, 2.9), QPointF(9.6, 4.4))
        p.drawPolyline(
            [
                QPointF(4.2, 4.4),
                QPointF(4.9, 13.1),
                QPointF(11.1, 13.1),
                QPointF(11.8, 4.4),
            ]
        )

    elif name == "refresh":
        path = QPainterPath()
        path.arcMoveTo(QRectF(2.6, 2.6, 10.8, 10.8), 60)
        path.arcTo(QRectF(2.6, 2.6, 10.8, 10.8), 60, 290)
        p.drawPath(path)
        p.drawPolyline(
            [
                QPointF(9.6, 1.6),
                QPointF(12.0, 4.2),
                QPointF(9.0, 5.6),
            ]
        )

    elif name == "copy":
        p.drawRoundedRect(QRectF(5.6, 5.6, 8.0, 8.0), 1.2, 1.2)
        p.drawPolyline(
            [
                QPointF(10.4, 3.4),
                QPointF(2.4, 3.4),
                QPointF(2.4, 11.4),
                QPointF(4.0, 11.4),
            ]
        )

    elif name == "check":
        p.setPen(_pen(color, 2.0))
        p.drawPolyline([QPointF(3.4, 8.4), QPointF(6.4, 11.4), QPointF(12.6, 4.8)])

    elif name == "close":
        p.drawLine(QPointF(4.4, 4.4), QPointF(11.6, 11.6))
        p.drawLine(QPointF(11.6, 4.4), QPointF(4.4, 11.6))

    elif name == "play":
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawPolygon(
            [
                QPointF(4.8, 3.1),
                QPointF(12.6, 8.0),
                QPointF(4.8, 12.9),
            ]
        )

    elif name == "stop":
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(4.2, 4.2, 7.6, 7.6), 1.0, 1.0)

    elif name == "external":
        p.drawPolyline(
            [
                QPointF(8.6, 2.8),
                QPointF(13.2, 2.8),
                QPointF(13.2, 7.4),
            ]
        )
        p.drawPolyline(
            [
                QPointF(13.2, 2.8),
                QPointF(7.4, 8.6),
            ]
        )
        p.drawPolyline(
            [
                QPointF(11.0, 9.4),
                QPointF(11.0, 13.2),
                QPointF(2.8, 13.2),
                QPointF(2.8, 5.0),
                QPointF(6.6, 5.0),
            ]
        )

    elif name == "bolt":
        p.drawPolyline(
            [
                QPointF(8.8, 1.8),
                QPointF(4.4, 8.8),
                QPointF(7.6, 8.8),
                QPointF(7.0, 14.2),
                QPointF(11.6, 7.0),
                QPointF(8.4, 7.0),
                QPointF(8.8, 1.8),
            ]
        )

    elif name == "print":
        # Лист, выходящий из принтера сверху, корпус и лист снизу.
        p.drawPolyline(
            [
                QPointF(4.8, 6.6),
                QPointF(4.8, 2.6),
                QPointF(11.2, 2.6),
                QPointF(11.2, 6.6),
            ]
        )
        p.drawRoundedRect(QRectF(2.2, 6.6, 11.6, 4.6), 1.2, 1.2)
        p.drawRect(QRectF(4.8, 11.2, 6.4, 2.4))
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(11.3, 8.4), 0.7, 0.7)
        p.setBrush(Qt.NoBrush)


def pixmap(name: str, color: str, size: int = 16) -> QPixmap:
    """Готовая иконка в виде QPixmap нужного размера.

    Цвет — значение, а не имя токена: картинка рисуется сразу. Кто ставит её
    через `setPixmap`, обязан сам перерисовать её при смене темы.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 16.0, size / 16.0)
    try:
        _draw(name, p, theme.color(color))
    finally:
        p.end()
    return pm


class TokenIconEngine(QIconEngine):
    """Иконка, которая берёт цвет в момент отрисовки.

    `QIcon` спрашивает движок заново на каждую отрисовку (проверено на
    PySide6 6.11.2: два `pixmap()` подряд дали два вызова движка и два разных
    цвета), поэтому иконка, помнящая имя токена, перекрашивается сама.
    Пересоздавать её при смене темы не нужно — достаточно перерисовать окно.

    Выключенное состояние рисуем приглушённым независимо от запрошенного
    цвета: ослабить полупрозрачностью Qt умеет только для своих иконок.
    """

    def __init__(self, name: str, token: str, size: int):
        super().__init__()
        self._name = name
        self._token = token
        self._size = size

    def pixmap(self, size, mode, state):  # noqa: N802 (Qt API)
        """Иконка **своего** размера, а не запрошенного.

        Размер, переданный при создании, — это и есть желаемый размер иконки,
        а `size` здесь — то, что просит виджет: `QTableWidget` всегда просит
        16 px, сколько бы ни просили при создании. Если отвечать запрошенным,
        иконки молча вырастают: чеврон, заказанный на 12 px, рисовался на 16,
        и в истории это видно замером — чернила шириной 10 px вместо 8.

        Прежний `QIcon(QPixmap)` вёл себя именно так: он отдавал ровно ту
        картинку, которую в него положили (проверено: `QIcon(12px).pixmap(16)`
        возвращает 12×12). Движок повторяет это поведение.
        """
        if mode == QIcon.Disabled:
            return pixmap(self._name, theme.color("DISABLED"), self._size)
        return pixmap(self._name, theme.color(self._token), self._size)

    def actualSize(self, size, mode, state):  # noqa: N802 (Qt API)
        """Сказать правду о размере.

        По умолчанию движок отвечает «запрошенный», и делегат растягивает
        картинку до него — 12 px превращаются в размытые 16. Отвечаем своим
        размером, и отрисовка идёт один к одному.
        """
        return QSize(self._size, self._size)

    def paint(self, painter: QPainter, rect, mode, state) -> None:
        # В Qt5 `rect.size()` — QSizeF, в Qt6 — уже QSize; `toSize` есть
        # только у первого, поэтому проверяем наличие, а не версию.
        size = rect.size()
        if hasattr(size, "toSize"):
            size = size.toSize()
        painter.drawPixmap(rect, self.pixmap(size, mode, state))

    def clone(self) -> QIconEngine:
        return TokenIconEngine(self._name, self._token, self._size)


def icon(name: str, token: str, size: int = 16) -> QIcon:
    """Иконка для кнопок и списков.

    `token` — имя токена темы («TEXT_3», «FAIL») или готовый «#rrggbb».
    Выключенное состояние дорисовывает движок, поэтому приглушённая кнопка
    не светит яркой иконкой.
    """
    return QIcon(TokenIconEngine(name, token, size))


__all__ = ["TokenIconEngine", "icon", "pixmap"]
