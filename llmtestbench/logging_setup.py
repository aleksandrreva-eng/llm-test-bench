"""Настройка логирования: один файл на приложение плюс консоль для CLI.

Раньше диагностика была размазана по трём местам: журнал в окне (виден только
там и теряется после закрытия), лог сервера (его пишет llama-server, а не
приложение) и два `print()` в библиотеке, которые в сборке без консоли уходят
в никуда. Когда прогон падал у пользователя, собрать факты было нечем: ни
времени, ни причины, ни контекста.

Здесь — стандартный `logging`: файл `logs/app.log` рядом с приложением с
ротацией по размеру и, если консоль есть, вывод туда же. Модули берут логгер
через `get_logger(__name__)` и не думают о том, куда он пишет.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FILE_NAME = "app.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_LEVEL = "info"

LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_configured = False
#: Куда реально пишет текущий обработчик. Хранится отдельно от аргумента:
#: иначе повторный вызов с другой папкой вернул бы новый путь, а писал бы
#: по-прежнему в старый файл — и «лог пустой» искали бы не там.
_log_path: Path | None = None


def setup_logging(
    logs_dir: str | Path,
    level: str = DEFAULT_LEVEL,
    *,
    console: bool = True,
) -> Path:
    """Включить логирование в `<logs_dir>/app.log`. Возвращает путь к файлу.

    Идемпотентно: повторный вызов (например, GUI поднялся в том же процессе
    после `--selfcheck`) не добавляет второй обработчик — дубли строк в логе
    хуже, чем отсутствие вызова. Возвращается путь уже настроенного лога,
    даже если аргумент указывает на другую папку: путь должен соответствовать
    тому, куда записи действительно идут.
    """
    global _configured, _log_path
    folder = Path(logs_dir)
    if _configured and _log_path is not None:
        return _log_path

    log_path = folder / LOG_FILE_NAME
    folder.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(LEVELS.get(str(level).lower(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # В оконной сборке потока ошибок может не быть вовсе — тогда только файл.
    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _configured = True
    _log_path = log_path
    get_logger(__name__).info("Логирование включено: %s (уровень %s)", log_path, level)
    return log_path


def get_logger(name: str) -> logging.Logger:
    """Логгер модуля. Обёртка нужна, чтобы модули не тянули `logging` ради
    одной строки и чтобы настройка была в одном месте."""
    return logging.getLogger(name)


def shutdown_logging() -> None:
    """Снять обработчики и разрешить повторную настройку.

    Нужна там, где логирование перенастраивают в одном процессе (проверки,
    повторный запуск GUI): без сброса флага второй `setup_logging` молча
    вернул бы прежний путь к логу, и записи ушли бы не туда, куда ждут.
    """
    global _configured, _log_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured = False
    _log_path = None
