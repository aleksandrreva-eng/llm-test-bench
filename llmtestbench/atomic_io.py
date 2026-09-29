"""Атомарная запись файлов: временный файл рядом + `os.replace`.

Зачем это нужно. Прогон набора длится минуты, а результат пишется в конце.
Обычный `Path.write_text` в момент записи усекает файл и наполняет его заново:
если процесс в этот момент убили (Ctrl+C, закрытие окна, кончилось место на
диске), на диске остаётся обрезанный JSON — и прогон теряется целиком, хотя все
кейсы уже прошли и ответы были получены.

`os.replace` в пределах одного тома атомарен: читатель видит либо старый файл
целиком, либо новый целиком, промежуточного состояния нет. Поэтому пишем во
временный файл **рядом с целевым** (на другом томе `replace` не атомарен) и
только потом подменяем им старый.

То же касается отчёта и конфига: битый `config.json` приложение переживёт (он
отодвигается в сторону), но настройки при этом потеряются.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ENCODING = "utf-8"
# CSV отдаём с BOM: без него Excel открывает русский текст кракозябрами.
CSV_ENCODING = "utf-8-sig"
TMP_SUFFIX = ".tmp"


def write_text_atomic(path: str | Path, text: str, encoding: str = ENCODING) -> Path:
    """Записать текст так, чтобы файл не мог остаться наполовину записанным.

    Возвращает путь к записанному файлу: вызывающему он обычно нужен, а лишний
    `Path(path)` в каждой точке записи читается хуже.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f"{target.name}.",
        suffix=TMP_SUFFIX,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            # Без fsync содержимое может остаться в кэше ОС: файл подменится,
            # а данных в нём ещё не будет.
            os.fsync(stream.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        # Сбой (в том числе Ctrl+C) не должен оставлять мусор в рабочей папке.
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def write_json_atomic(
    path: str | Path,
    data: Any,
    *,
    indent: int | None = 2,
    encoding: str = ENCODING,
) -> Path:
    """Записать данные как JSON — так же атомарно, как обычный текст.

    `indent=None` даёт компактный JSON: для кэша метаданных, который читает
    только приложение, отступы — лишние килобайты на диске.
    """
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    return write_text_atomic(path, text, encoding=encoding)


def write_csv_atomic(
    path: str | Path,
    rows: Iterable[Sequence[Any]],
    encoding: str = CSV_ENCODING,
    delimiter: str = ",",
) -> Path:
    """Записать таблицу в CSV через тот же временный файл."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter)
    writer.writerows(rows)
    return write_text_atomic(path, buffer.getvalue(), encoding=encoding)
