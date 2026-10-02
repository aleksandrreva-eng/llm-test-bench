"""Сборка растровых производных иконки из векторных исходников `assets/icons/`.

Два выхода:

* `icon.ico` — из `app-icon.svg` (знак на тёмной плитке). Кадры 16, 24, 32, 48,
  64, 128, 256.
* `assets/icons/app-icon-mark.png` — из `app-icon-mark.svg` (тот же знак **без
  подложки**), обрезанный по контуру с полем. Нужен там, где плитка мешает:
  кадры рекламного ролика (`promo/make_frames.py`), README, светлые фоны.

Зачем отдельный скрипт. Раньше в `icon.ico` лежал единственный кадр 118x132 —
обрезанный растр. Из-за единственного размера иконка мылилась в Проводнике на
крупных значках. Исходник векторный, поэтому правильный путь — отрисовать каждый
размер отдельно, а не уменьшать один большой растр: на 16x16 разница между
векторным рендером и даунсэмплом видна невооружённым глазом.

Кадры пишутся **несжатым BMP (32 бита + AND-маска), а не PNG**. PNG-кадры внутри
ICO понимает Windows Vista и новее, но их не понимает WiX, который собирает MSI
(`installer/LLMTestBench.wxs` берёт `..\\icon.ico` для значка в «Установка и
удаление программ»). Цена совместимости — размер файла, и она того стоит.

Pillow здесь не нужен намеренно: его нет в `requirements.txt` приложения, а
скрипт должен запускаться в `.venv` проекта. Байты кадра берутся прямо из QImage
(в памяти `Format_ARGB32` лежит как BGRA — ровно то, что ждёт ICO), а обрезка
знака — через `QImage.copy()`.

Запуск:  .venv/Scripts/python.exe make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICONS = ROOT / "assets" / "icons"

SVG = ICONS / "app-icon.svg"
ICO = ROOT / "icon.ico"

MARK_SVG = ICONS / "app-icon-mark.svg"
MARK_PNG = ICONS / "app-icon-mark.png"
MARK_PX = 1024
#: Поле вокруг знака в PNG, доля от его большей стороны.
MARK_MARGIN = 0.04

#: Кадры. 24x24 Windows берёт для мелких списков, 256x256 — для «Крупных значков»
#: и плиток; пропускать его нельзя, иначе Проводник растянет 128-й.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(svg: Path, size: int):
    """Отрисовать SVG в квадрат `size`x`size` с прозрачным фоном."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise RuntimeError(f"не разобрать SVG: {svg}")

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def opaque_bounds(image) -> tuple[int, int, int, int] | None:
    """Прямоугольник непрозрачных пикселей: `(left, top, right, bottom)`, right и
    bottom — исключающие. `None`, если непрозрачных нет вовсе.

    Своими руками, а не через `QImage.createAlphaMask()`: тот отдаёт
    чёрно-белую маску, по которой границу пришлось бы искать так же.
    """
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = bytes(image.constBits())

    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        alpha = raw[y * stride : y * stride + width * 4][3::4]
        if not any(alpha):
            continue
        first = next(x for x in range(width) if alpha[x])
        last = next(x for x in range(width - 1, -1, -1) if alpha[x])
        left, right = min(left, first), max(right, last)
        top, bottom = min(top, y), y
    if right < 0:
        return None
    return left, top, right + 1, bottom + 1


def write_mark_png() -> None:
    """Знак без подложки — PNG, обрезанный по контуру с полем.

    Без обрезки знак занимает меньше половины квадрата 512x512 (куб нарисован
    по центру с запасом под плитку), и в кадре ролика он выглядел бы вдвое
    мельче, чем задумано.
    """
    from PySide6.QtCore import QRect

    image = render(MARK_SVG, MARK_PX)
    box = opaque_bounds(image)
    if box is None:
        raise RuntimeError(f"{MARK_SVG}: нечего обрезать — всё прозрачное")

    left, top, right, bottom = box
    pad = int(round(max(right - left, bottom - top) * MARK_MARGIN))
    left, top = max(0, left - pad), max(0, top - pad)
    right = min(image.width(), right + pad)
    bottom = min(image.height(), bottom + pad)
    image.copy(QRect(left, top, right - left, bottom - top)).save(str(MARK_PNG), "PNG")


def bmp_frame(image) -> bytes:
    """Кадр ICO в формате DIB: заголовок, BGRA снизу вверх, затем AND-маска."""
    width = image.width()
    height = image.height()
    stride = image.bytesPerLine()
    raw = bytes(image.constBits())

    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)

    # Qt хранит строки сверху вниз, DIB — снизу вверх.
    xor = bytearray()
    for y in range(height - 1, -1, -1):
        xor += raw[y * stride : y * stride + width * 4]

    # Маска нужна даже при 32 битах: её читают старые оболочки. Строка
    # выравнивается по 4 байта, единица — «прозрачно».
    row_bytes = ((width + 31) // 32) * 4
    mask = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray(row_bytes)
        for x in range(width):
            if raw[y * stride + x * 4 + 3] < 128:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row

    return header + bytes(xor) + bytes(mask)


def build(frames: list[bytes]) -> bytes:
    """Собрать ICO: заголовок, таблица кадров, сами кадры."""
    count = len(frames)
    offset = 6 + 16 * count
    directory = bytearray(struct.pack("<HHH", 0, 1, count))
    body = bytearray()
    for frame, size in zip(frames, SIZES, strict=True):
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset
        )
        body += frame
        offset += len(frame)
    return bytes(directory + body)


def main() -> int:
    for source in (SVG, MARK_SVG):
        if not source.is_file():
            print(f"нет исходника: {source}")
            return 1

    from PySide6.QtWidgets import QApplication

    # Приложение нужно до первого рендера: без него не подхватываются системные
    # шрифты, и текст на иконке выходит пустыми квадратами. Платформа — обычная
    # `windows`, а не `offscreen`: у offscreen нет системных шрифтов.
    QApplication.instance() or QApplication([])

    frames = [bmp_frame(render(SVG, size)) for size in SIZES]
    data = build(frames)

    temporary = ICO.with_name(ICO.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, ICO)

    write_mark_png()

    print("кадры: " + ", ".join(f"{size}x{size}" for size in SIZES))
    print(f"записано: {ICO}  ({len(data) / 1024:.0f} КБ)")
    print(f"записано: {MARK_PNG}  ({MARK_PNG.stat().st_size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
