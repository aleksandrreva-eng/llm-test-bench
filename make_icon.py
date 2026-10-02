"""Сборка `icon.ico` из векторного исходника `assets/icons/app-icon.svg`.

Зачем отдельный скрипт. Раньше в `icon.ico` лежал единственный кадр 118x132 —
обрезанный растр куба «GGUF». Из-за единственного размера иконка мылилась в
Проводнике на крупных значках и была мелкой в кадрах рекламного ролика. Исходник
векторный, поэтому правильный путь — отрисовать каждый размер отдельно, а не
уменьшать один большой растр: на 16x16 разница между векторным рендером и
даунсэмплом видна невооружённым глазом.

Кадры пишутся **несжатым BMP (32 бита + AND-маска), а не PNG**. PNG-кадры внутри
ICO понимает Windows Vista и новее, но их не понимает WiX, который собирает MSI
(`installer/LLMTestBench.wxs` берёт `..\\icon.ico` для значка в «Установка и
удаление программ»). Цена совместимости — размер файла, и она того стоит.

Pillow здесь не нужен намеренно: его нет в `requirements.txt` приложения, а
скрипт должен запускаться в `.venv` проекта. Байты кадра берутся прямо из QImage
(в памяти `Format_ARGB32` лежит как BGRA — ровно то, что ждёт ICO).

Запуск:  .venv/Scripts/python.exe make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SVG = ROOT / "assets" / "icons" / "app-icon.svg"
ICO = ROOT / "icon.ico"

#: Кадры. 24x24 Windows берёт для мелких списков, 256x256 — для «Крупных значков»
#: и плиток; пропускать его нельзя, иначе Проводник растянет 128-й.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int):
    """Отрисовать SVG в квадрат `size`x`size` с прозрачным фоном."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise RuntimeError(f"не разобрать SVG: {SVG}")

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


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
    if not SVG.is_file():
        print(f"нет исходника: {SVG}")
        return 1

    from PySide6.QtWidgets import QApplication

    # Приложение нужно до первого рендера: без него не подхватываются системные
    # шрифты, и надпись «GGUF» выходит пустыми квадратами. Платформа — обычная
    # `windows`, а не `offscreen`: у offscreen нет системных шрифтов.
    QApplication.instance() or QApplication([])

    frames = [bmp_frame(render(size)) for size in SIZES]
    data = build(frames)

    temporary = ICO.with_name(ICO.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, ICO)

    print("кадры: " + ", ".join(f"{size}x{size}" for size in SIZES))
    print(f"записано: {ICO}  ({len(data) / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
