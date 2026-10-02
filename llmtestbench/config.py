"""Конфигурация приложения: config.json (раздел 7 ТЗ).

Правило простое: файл лежит рядом с приложением, читается и пишется в UTF-8,
неизвестные ключи не теряются (чтобы старые версии не съедали новые поля).
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import __version__
from .atomic_io import write_json_atomic, write_text_atomic

CONFIG_NAME = "config.json"

#: Штамп в папке наборов рядом с .exe: какой версией приложения она разложена.
#: Нужен, чтобы отличить «копия от прошлой версии» от «копия уже актуальна» —
#: см. `AppConfig.init_frozen_resources`.
BUNDLE_STAMP_NAME = ".bundle-version"


def app_root() -> Path:
    """Корень приложения.

    В исходниках — папка проекта (на уровень выше пакета), в собранном
    PyInstaller-е — папка рядом с .exe, а не временный _MEIPASS: конфиг,
    логи и результаты должны переживать перезапуск.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Read-only бандлённые ресурсы (tests, docs).

    Frozen one-file: PyInstaller извлекает данные во временный sys._MEIPASS.
    Frozen onedir / dev: рядом с приложением (app_root).
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def read_bundle_stamp(directory: Path) -> str:
    """Версия приложения, которой разложена копия наборов.

    Пустая строка — штампа нет: так выглядят копии, сделанные сборками до
    1.0.2, и они подлежат перезаливке.
    """
    try:
        return (directory / BUNDLE_STAMP_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_aside_bundle_copy(directory: Path, version: str) -> bool:
    """Отложить прежнюю копию наборов рядом: `tests.backup-<версия>-<дата>`.

    Не удаляем: в копии могут лежать наборы, правленные в редакторе. `False`
    означает, что переименовать не удалось (папку держит кто-то ещё), и тогда
    раскладывать поверх нельзя — получилась бы смесь двух версий.
    """
    backup = directory.with_name(
        "%s.backup-%s-%s" % (directory.name, version or "unknown", time.strftime("%Y%m%d-%H%M%S"))
    )
    try:
        directory.rename(backup)
    except OSError:
        return False
    return True


@dataclass
class AppConfig:
    """Настройки приложения. Значения по умолчанию — из раздела 7 ТЗ."""

    # --- пути ---
    models_dir: str = ""
    llama_server_path: str = ""
    tests_dir: str = ""
    results_dir: str = ""
    reports_dir: str = ""
    logs_dir: str = ""
    sessions_dir: str = ""
    # Кэши (метаданные GGUF, вердикты судьи). Пустое значение — `.cache` рядом
    # с приложением. Отдельным полем, а не жёстко: проверки подставляют сюда
    # временную папку и не трогают рабочий кэш проекта.
    cache_dir: str = ""

    # --- сервер ---
    server_host: str = "127.0.0.1"
    server_port: int = 8080
    auto_stop_server: bool = True
    health_check_timeout_sec: int = 60
    use_existing_server: bool = False
    # PID своего llama-server.exe. Нужен ровно для одного: если приложение
    # упало, при следующем старте убить свой осиротевший процесс (п. 4.6).
    # Обнуляется при нормальной остановке. Чужой сервер не трогаем.
    server_pid: int = 0

    # --- тестирование ---
    default_runs: int = 1
    default_test_types: list[str] = field(default_factory=lambda: ["chat_single"])
    default_test_set: str = "chat_single"
    last_case_limit: int = 0
    # Запас токенов сверх бюджета ответа: у думающих моделей рассуждение идёт
    # в тот же счётчик, и тесный запас даёт пустой ответ при finish_reason=length.
    # Значение совпадает с DEFAULT_REASONING_ALLOWANCE в runner.py; импортировать
    # его сюда нельзя — config читается раньше и импорт получился бы круговым.
    reasoning_allowance: int = 2048
    request_timeout_sec: int = 900
    # Прогревать ли сервер перед прогоном. Первый запрос после загрузки
    # модели дороже последующих (CUDA-графы, KV-кэш), и без прогрева этот
    # выброс попадает в первый кейс — а в наборе `speed` первый кейс измеряем.
    warmup: bool = True
    # Кэшировать ли вердикты судьи. Судья — это ещё один запрос к модели, и
    # повторный прогон тех же ответов платил бы токенами за уже полученные
    # оценки. Выключается, когда нужен гарантированно свежий вердикт.
    judge_cache: bool = True

    # --- локальный субагент ---
    # Бюджет на одну задачу. Не равен контексту сервера намеренно: часть
    # контекста должна остаться под ответ, иначе модель упрётся в потолок
    # и оборвёт вывод на середине.
    agent_token_cap: int = 100000
    agent_temperature: float = 0.3
    agent_max_tokens: int = 8192

    # --- прочее ---
    log_level: str = "info"
    recursive_scan: bool = True
    ui_theme: str = "dark"
    ui_rail_collapsed: bool = False
    ui_run_log_open: bool = True
    window_geometry: str = ""
    history_db_name: str = "history.db"

    # ------------------------------------------------------------------
    # пути по умолчанию

    def resolve_dirs(self) -> None:
        """Подставить пути по умолчанию.

        В собранном приложении (frozen) пути должны быть переносимыми —
        рядом с .exe, а не в виде абсолютных путей машины разработчика,
        которые могли попасть в config.json. Поэтому при frozen: если поле
        пустое ИЛИ указывает на несуществующий путь, ставим дефолт у exe.
        """
        root = app_root()
        frozen = getattr(sys, "frozen", False)
        defaults = {
            "tests_dir": root / "tests",
            "results_dir": root / "results",
            "reports_dir": root / "reports",
            "logs_dir": root / "logs",
            "sessions_dir": root / "sessions",
        }
        for attr, default in defaults.items():
            cur = getattr(self, attr) or ""
            if not cur or (frozen and not Path(cur).exists()):
                setattr(self, attr, str(default))

    def init_frozen_resources(self) -> None:
        """Разложить наборы тестов рядом с .exe и держать их в одной версии с
        приложением.

        Зачем копия вообще: в бандле наборы read-only, а редактор тестов правит
        их на диске, и история с результатами должны писаться в переносимое
        место. Поэтому первый запуск разворачивает копию рядом с `.exe`.

        Почему по штампу версии: раньше копия делалась один раз («копируем,
        если папки ещё нет») и после обновления приложения рядом оставались
        наборы прошлой версии — новые в окне не появлялись, изменённые не
        обновлялись, и стенд показывал не то, что лежит в сборке. Теперь в
        копию пишется версия приложения, и при расхождении она раскладывается
        заново, а прежняя откладывается в `tests.backup-<версия>-<дата>`:
        правки, сделанные в редакторе, не пропадают.
        """
        if not getattr(sys, "frozen", False):
            return
        src = bundle_root() / "tests"
        if not src.is_dir():
            return
        dst = self.tests_path

        if dst.is_dir():
            stamp = read_bundle_stamp(dst)
            if stamp == __version__:
                return
            if not set_aside_bundle_copy(dst, stamp):
                return
        try:
            shutil.copytree(src, dst)
            write_text_atomic(dst / BUNDLE_STAMP_NAME, __version__)
        except OSError:
            pass

    @property
    def tests_path(self) -> Path:
        return Path(self.tests_dir or (app_root() / "tests"))

    @property
    def results_path(self) -> Path:
        return Path(self.results_dir or (app_root() / "results"))

    @property
    def reports_path(self) -> Path:
        return Path(self.reports_dir or (app_root() / "reports"))

    @property
    def logs_path(self) -> Path:
        return Path(self.logs_dir or (app_root() / "logs"))

    @property
    def sessions_path(self) -> Path:
        return Path(self.sessions_dir or (app_root() / "sessions"))

    @property
    def cache_path(self) -> Path:
        """Папка кэшей: метаданные GGUF, вердикты судьи."""
        return Path(self.cache_dir) if self.cache_dir else (app_root() / ".cache")

    @property
    def db_path(self) -> Path:
        """Путь к SQLite-базе истории прогонов (рядом с .exe, не CWD)."""
        return app_root() / (self.history_db_name or "history.db")

    @property
    def server_base_url(self) -> str:
        return "http://%s:%d" % (self.server_host, self.server_port)

    # ------------------------------------------------------------------
    # загрузка / сохранение

    def bind(self, path: str | Path) -> "AppConfig":
        """Привязать конфиг к файлу: `save()` без пути будет писать именно туда.

        Нужно не для красоты. `ServerManager` сохраняет PID процесса при
        каждом запуске и остановке, а делает он это через `cfg.save()` без
        пути. Пока путь не запоминался, конфиг, собранный в проверках на
        временной папке, уезжал в рабочий `config.json` проекта — и настройки
        затирались временными путями. Теперь проверки привязывают свой конфиг
        к временному файлу и рабочий не трогают.
        """
        self._config_path = Path(path)
        return self

    @property
    def config_path(self) -> Path:
        """Файл, с которым связан конфиг (по умолчанию — рядом с приложением)."""
        return Path(getattr(self, "_config_path", "") or (app_root() / CONFIG_NAME))

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """Прочитать config.json. Битого или отсутствующего файла не боимся."""
        cfg_path = Path(path) if path else (app_root() / CONFIG_NAME)
        data: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Битый конфиг — не повод падать: работаем на дефолтах,
                # а сломанный файл отодвинем в сторону, чтобы не терять данные.
                try:
                    cfg_path.replace(cfg_path.with_suffix(".json.broken"))
                except OSError:
                    pass
                data = {}
        if not isinstance(data, dict):
            data = {}

        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        # Незнакомые ключи сохраняем, чтобы не затирать настройки другой версии.
        cfg._extra = {k: v for k, v in data.items() if k not in known}  # type: ignore[attr-defined]
        cfg.bind(cfg_path)
        cfg.resolve_dirs()
        cfg.init_frozen_resources()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        """Записать config.json — туда, откуда читали, если путь не задан."""
        cfg_path = Path(path) if path else self.config_path
        data = asdict(self)
        data.pop("_extra", None)
        extra = getattr(self, "_extra", None)
        if isinstance(extra, dict):
            data.update(extra)
        # Через временный файл: config.json пишется на каждом старте и остановке
        # сервера, и обрыв на середине записи оставил бы приложение без настроек.
        return write_json_atomic(cfg_path, data)

    def ensure_dirs(self) -> None:
        """Создать рабочие папки, если их нет."""
        for p in (
            self.results_path,
            self.reports_path,
            self.logs_path,
            self.sessions_path,
            self.cache_path,
        ):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
