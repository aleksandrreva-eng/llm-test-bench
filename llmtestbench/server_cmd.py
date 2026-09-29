"""Команда запуска llama-server и поиск исполняемого файла (раздел 3.4 ТЗ).

Источник параметров (п. 3.4.2), в порядке убывания приоритета:
    1. ручные правки пользователя (overrides);
    2. метаданные GGUF (general.sampling.*, general.context_length);
    3. дефолты llama.cpp.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, app_root
from .gguf_scanner import ModelInfo

EXE_NAME = "llama-server.exe" if os.name == "nt" else "llama-server"

# Дефолты llama.cpp (п. 3.4.2: «дефолты llama.cpp»)
DEFAULTS = {
    "n_ctx": 4096,
    "temp": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "seed": -1,
    "n_gpu_layers": -1,  # -1 = все слои на GPU, как в llama.cpp
}

# Ключи метаданных GGUF с параметрами сэмплирования
_SAMPLING_KEYS = {
    "temp": ("general.sampling.temp",),
    "top_p": ("general.sampling.top_p",),
    "top_k": ("general.sampling.top_k",),
}


@dataclass
class LaunchParams:
    """Итоговые параметры запуска сервера."""

    model_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    n_ctx: int = 0
    temp: float = 0.0
    top_p: float = 0.0
    top_k: int = 0
    seed: int = -1
    n_gpu_layers: int = -1
    extra_flags: list[str] = field(default_factory=list)
    # Что откуда взялось — пригодится в UI («почему именно 8192?»)
    sources: dict[str, str] = field(default_factory=dict)


def _fmt_num(value) -> str:
    """Число в строку без хвостового «.0»: 0.8, 40, 4096."""
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return "%g" % value
    return str(value)


def quote_arg(arg: str) -> str:
    """Заквотировать аргумент, если в нём есть пробелы или кавычки."""
    s = str(arg)
    if not s:
        return '""'
    if re.search(r'[\s"]', s):
        return '"%s"' % s.replace('"', r"\"")
    return s


def resolve_params(
    model: ModelInfo | None,
    cfg: AppConfig,
    overrides: dict | None = None,
    extra_flags: list[str] | None = None,
) -> LaunchParams:
    """Свести параметры из трёх источников по приоритету."""
    ov = dict(overrides or {})
    meta = dict(getattr(model, "sampling", None) or {}) if model else {}
    p = LaunchParams()
    p.model_path = model.path if model else ""
    p.sources = {}

    def pick(name: str, from_meta, default):
        if name in ov and ov[name] not in (None, ""):
            p.sources[name] = "override"
            return ov[name]
        if from_meta not in (None, "", 0):
            p.sources[name] = "gguf"
            return from_meta
        p.sources[name] = "default"
        return default

    p.host = str(ov.get("host") or cfg.server_host)
    p.port = int(ov.get("port") or cfg.server_port)

    meta_ctx = int(getattr(model, "n_ctx_train", 0) or 0)
    # Тренировочный контекст — это максимум, а не рабочий размер. Если он
    # есть, берём его: так тест длинного контекста имеет смысл (п. 11.3.6).
    p.n_ctx = int(pick("n_ctx", meta_ctx, DEFAULTS["n_ctx"]))

    def meta_sampling(key: str):
        for k in _SAMPLING_KEYS.get(key, ()):
            if k in meta:
                return meta[k]
        return None

    p.temp = float(pick("temp", meta_sampling("temp"), DEFAULTS["temp"]))
    p.top_p = float(pick("top_p", meta_sampling("top_p"), DEFAULTS["top_p"]))
    p.top_k = int(pick("top_k", meta_sampling("top_k"), DEFAULTS["top_k"]))
    p.seed = int(pick("seed", None, DEFAULTS["seed"]))
    p.n_gpu_layers = int(
        pick("n_gpu_layers", None, ov.get("n_gpu_layers", DEFAULTS["n_gpu_layers"]))
    )
    p.extra_flags = list(extra_flags or ov.get("extra_flags") or [])
    return p


def build_command(
    server_exe: str | Path,
    params: LaunchParams,
    *,
    as_list: bool = False,
) -> list[str] | str:
    """Собрать команду запуска сервера.

    as_list=True — список аргументов для subprocess (без кавычек внутри),
    as_list=False — строка для показа пользователю в редактируемом поле.
    """
    tokens = [
        str(server_exe),
        "-m",
        params.model_path,
        "--host",
        params.host,
        "--port",
        str(params.port),
        "-c",
        str(params.n_ctx),
        "--temp",
        _fmt_num(params.temp),
        "--top-p",
        _fmt_num(params.top_p),
        "--top-k",
        str(params.top_k),
        "--seed",
        str(params.seed),
        "-ngl",
        str(params.n_gpu_layers),
    ]
    tokens.extend(str(f) for f in params.extra_flags)
    if as_list:
        return tokens
    return " ".join(quote_arg(t) for t in tokens)


def parse_command(line: str) -> list[str]:
    """Разобрать строку команды обратно в аргументы — по правилам Windows.

    Нужно, когда пользователь отредактировал команду руками (п. 3.4.2):
    приложение должно уметь её выполнить. Разбор парный к `quote_arg`.

    Ни `shlex.split(posix=False)`, ни `posix=True` здесь не годятся, и оба
    врут молча:

      * `posix=False` оставляет кавычки внутри токена: `-m "F:\\a b.gguf"`
        превращается в `'"F:\\a b.gguf"'`, и дочерний процесс получит
        кавычки как часть пути. А `--flag="a b"` он вообще рвёт на
        `--flag="a` и `b"` — кавычка в середине токена для него не
        открывающая.
      * `posix=True` считает обратный слэш экранирующим и съедает его в
        `"F:\\Models\\x.gguf"` — для путей Windows это смерть.

    Поэтому разбираем сами: кавычка переключает режим, обратный слэш перед
    кавычкой — это кавычка (ровно так, как их расставляет `quote_arg`).
    """
    text = (line or "").strip()
    if not text:
        return []
    args: list[str] = []
    cur: list[str] = []
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == '"':
            cur.append('"')
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if ch.isspace() and not in_quotes:
            if cur:
                args.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        args.append("".join(cur))
    return args


# ---------------------------------------------------------------------------
# что известно о сервере, против которого шёл прогон


_NOTE_UNKNOWN = "сервер поднят не приложением — команда запуска неизвестна"
_NOTE_FOREIGN = "команда задана снаружи, приложение её не применяло"
_NOTE_EMPTY = "команда не записана"


def describe_server(command: str = "", *, applied: bool = False) -> dict:
    """Описать сервер для записи в `params` прогона.

    Нужно это ровно затем, чтобы два прогона одного набора на одной модели
    были отличимы задним числом. Скорость решают флаги запуска (`--load-mode`,
    `-ot`, `--n-cpu-moe`), а их в записи не было: 42 и 56 t/s лежали рядом и
    ничем не отличались.

    Ключей три, и все три — факты, а не догадки:

      * `applied` — приложение ли подняло процесс этой командой;
      * `command` — команда запуска как есть; пустая строка — «неизвестна»;
      * `note` — почему пустая (пустая строка — если сказать нечего).

    `applied=False` при непустой команде — не противоречие: так выглядит
    консольный прогон, где сервер поднял кто-то другой, но команду назвал
    человек. Врать за сервер нельзя, терять сказанное — тоже незачем.
    """
    cmd = str(command or "").strip()
    if applied:
        return {"applied": True, "command": cmd, "note": "" if cmd else _NOTE_EMPTY}
    return {
        "applied": False,
        "command": cmd,
        "note": _NOTE_FOREIGN if cmd else _NOTE_UNKNOWN,
    }


# ---------------------------------------------------------------------------
# поиск llama-server.exe


def _candidate_paths() -> list[tuple[str, Path]]:
    """Кандидаты на llama-server.exe, в порядке проверки."""
    cands: list[tuple[str, Path]] = []
    root = app_root()
    cands.append(("рядом с приложением", root / EXE_NAME))
    cands.append(("подпапка llama.cpp", root / "llama.cpp" / EXE_NAME))

    which = shutil.which("llama-server")
    if which:
        cands.append(("PATH", Path(which)))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(
            (
                "%LOCALAPPDATA%\\Programs\\llama.cpp",
                Path(local) / "Programs" / "llama.cpp" / EXE_NAME,
            )
        )

    cands.append(("C:\\llama.cpp\\build\\bin", Path(r"C:\llama.cpp\build\bin") / EXE_NAME))

    # Специфика этой машины: сборка, из которой работает лаунчер Инка.
    roaming = os.environ.get("APPDATA")
    if roaming:
        cands.append(
            (
                "лаунчер LlamaServerLauncherAvalonia",
                Path(roaming) / "LlamaServerLauncherAvalonia" / "llama.cpp" / EXE_NAME,
            )
        )

    # LM Studio держит свои сборки бэкендов — берём CUDA-варианты, свежие сверху.
    home = Path.home()
    backends = home / ".lmstudio" / "extensions" / "backends"
    if backends.is_dir():
        try:
            dirs = sorted(
                (d for d in backends.iterdir() if d.is_dir()),
                key=lambda d: d.name,
                reverse=True,
            )
        except OSError:
            dirs = []
        for d in dirs:
            if "cuda" not in d.name.lower():
                continue
            cands.append(("LM Studio: %s" % d.name, d / EXE_NAME))
    return cands


def find_server_candidates() -> list[tuple[str, Path]]:
    """Только существующие кандидаты — (описание, путь)."""
    out = []
    seen: set[str] = set()
    for label, path in _candidate_paths():
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            out.append((label, path))
    return out


def autodetect_server() -> Path | None:
    """Первый найденный llama-server.exe (п. 3.4.1: «Автопоиск»)."""
    found = find_server_candidates()
    return found[0][1] if found else None


def validate_server_exe(path: str | Path) -> tuple[bool, str]:
    """Проверить, что это рабочий llama-server (п. 3.4.1).

    Возвращает (годен, сообщение). Запускаем `--version` с коротким таймаутом:
    битый или чужой exe либо упадёт, либо не ответит.
    """
    p = Path(path)
    if not p.is_file():
        return False, "файл не найден"
    if os.name == "nt" and p.suffix.lower() != ".exe":
        return False, "ожидался .exe"
    try:
        proc = subprocess.run(
            [str(p), "--version"],
            capture_output=True,
            timeout=20,
            cwd=str(p.parent),
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
    except subprocess.TimeoutExpired:
        return False, "не ответил на --version за 20 сек"
    except OSError as exc:
        return False, "не удалось запустить: %s" % exc
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    text += (proc.stderr or b"").decode("utf-8", errors="replace")
    text = text.strip()
    if proc.returncode != 0:
        return False, "код возврата %d: %s" % (proc.returncode, text[:200])
    first = text.splitlines()[0] if text else "версия не сообщена"
    return True, first[:200]


def describe_environment() -> dict:
    """Сводка окружения для лога и о проблемах с GPU (п. 9 ТЗ)."""
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "exe_dir": str(app_root()),
        "server_candidates": [
            {"label": lbl, "path": str(p)} for lbl, p in find_server_candidates()
        ],
    }
