"""Сканирование директории с моделями и чтение метаданных GGUF.

Раздел 3.1 ТЗ: список моделей с именем, размером и параметрами из GGUF.
Раздел 11 требует понимать контекст модели — его берём из `*.context_length`.

Метаданные читаются своим разбором заголовка GGUF, а не библиотекой `gguf`:
она тянет numpy-массив под весь файл и на 20-гигабайтной модели ведёт себя
нервно. Заголовок и KV-секция лежат в начале файла, читаются за миллисекунды,
и нам оттуда нужно десятка полтора ключей. Библиотека `gguf` остаётся
резервным путём, если свой разбор не осилит формат.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .atomic_io import write_json_atomic
from .config import app_root
from .logging_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# константы формата GGUF

GGUF_MAGIC = 0x46554747  # 'GGUF' в little-endian

# типы значений в KV-секции
(
    GGUF_UINT8,
    GGUF_INT8,
    GGUF_UINT16,
    GGUF_INT16,
    GGUF_UINT32,
    GGUF_INT32,
    GGUF_FLOAT32,
    GGUF_BOOL,
    GGUF_STRING,
    GGUF_ARRAY,
    GGUF_UINT64,
    GGUF_INT64,
    GGUF_FLOAT64,
) = range(13)

_FIXED = {
    GGUF_UINT8: ("<B", 1),
    GGUF_INT8: ("<b", 1),
    GGUF_UINT16: ("<H", 2),
    GGUF_INT16: ("<h", 2),
    GGUF_UINT32: ("<I", 4),
    GGUF_INT32: ("<i", 4),
    GGUF_FLOAT32: ("<f", 4),
    GGUF_BOOL: ("<?", 1),
    GGUF_UINT64: ("<Q", 8),
    GGUF_INT64: ("<q", 8),
    GGUF_FLOAT64: ("<d", 8),
}

# ключи, которые нам действительно нужны
_KV_KEEP = {
    "general.architecture",
    "general.name",
    "general.basename",
    "general.size_label",
    "general.file_type",
    "general.parameter_count",
    "general.sampling.temp",
    "general.sampling.top_p",
    "general.sampling.top_k",
    "general.sampling.repeat_penalty",
    "tokenizer.ggml.model",
    "split.count",
}
_KV_KEEP_SUFFIX = (
    ".context_length",
    ".block_count",
    ".embedding_length",
    ".attention.head_count",
    ".expert_count",
)

# Квантование угадываем по имени файла: в метаданных лежит только числовой
# general.file_type, а человеку нужно «Q4_K_M». Имя файла тут надёжнее.
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"IQ[1-4]_[A-Z0-9_]+|Q[2-8]_[A-Z0-9_]+|Q[2-8]_[0-9]|"
    r"Q[2-8]|F16|F32|BF16|MXFP4|NVFP4|FP8|FP16|FP32"
    r")(?![A-Za-z0-9])"
)

_SIDECAR_RE = re.compile(r"^(mmproj|mtp|ggml-vocab)[-_.]", re.IGNORECASE)

# Файлы-спутники: запустить их как модель нельзя, поэтому в списке моделей
# они лишние. `ggml-vocab-*` — словари токенизаторов из репозитория llama.cpp
# (лежат в models/, весят килобайты, архитектура у них «настоящая»).
_SIDECAR_KINDS = {"mmproj": "mmproj", "mtp": "mtp", "ggml-vocab": "vocab"}


def guess_quant(file_name: str) -> str:
    """Вытащить обозначение квантования из имени файла («Q4_K_M», «Q8_0»)."""
    stem = Path(file_name).stem
    found = _QUANT_RE.findall(stem)
    return found[-1].upper() if found else ""


def classify_sidecar(file_name: str) -> str:
    """Определить, что это не самостоятельная модель.

    `mmproj-*` — проектор для мультимодальных моделей, `mtp-*` — голова
    multi-token prediction, `ggml-vocab-*` — словарь токенизатора из
    репозитория llama.cpp. Ни один из них не запускается как модель.
    """
    m = _SIDECAR_RE.match(file_name)
    if not m:
        return ""
    return _SIDECAR_KINDS.get(m.group(1).lower(), "sidecar")


# ---------------------------------------------------------------------------
# модель данных


@dataclass
class ModelInfo:
    """Одна найденная .gguf-модель."""

    path: str
    file_name: str
    size_bytes: int
    name: str = ""
    arch: str = ""
    n_ctx_train: int = 0
    n_params: int = 0
    n_layers: int = 0
    n_embd: int = 0
    quant: str = ""
    file_type: int = -1
    sidecar: str = ""  # "", "mmproj", "mtp"
    sampling: dict = field(default_factory=dict)
    error: str = ""

    @property
    def is_model(self) -> bool:
        return not self.sidecar

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3)

    @property
    def size_human(self) -> str:
        b = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024 or unit == "TB":
                return "%.1f %s" % (b, unit) if unit != "B" else "%d B" % b
            b /= 1024
        return "%.1f TB" % b

    @property
    def params_human(self) -> str:
        """Параметры в человеческом виде: 7.6B, 26.4B."""
        if not self.n_params:
            return ""
        p = float(self.n_params)
        if p >= 1e9:
            return "%.1fB" % (p / 1e9)
        if p >= 1e6:
            return "%.0fM" % (p / 1e6)
        return "%d" % self.n_params

    @property
    def display_name(self) -> str:
        return self.name or self.file_name


# ---------------------------------------------------------------------------
# чтение заголовка GGUF


class _Reader:
    """Последовательное чтение из файла с подсчётом прочитанного."""

    def __init__(self, fh):
        self.fh = fh

    def read(self, n: int) -> bytes:
        data = self.fh.read(n)
        if len(data) != n:
            raise EOFError("неожиданный конец файла при разборе GGUF")
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self) -> str:
        n = self.u64()
        return self.read(n).decode("utf-8", errors="replace")

    def value(self, vtype: int, *, keep: bool):
        """Прочитать значение. keep=False — прочитать и выбросить."""
        if vtype in _FIXED:
            fmt, size = _FIXED[vtype]
            raw = self.read(size)
            return struct.unpack(fmt, raw)[0] if keep else None
        if vtype == GGUF_STRING:
            s = self.string()
            return s if keep else None
        if vtype == GGUF_ARRAY:
            elem_type = self.u32()
            count = self.u64()
            # Массивы строк (словарь токенизатора) бывают на сотни тысяч
            # элементов — их точно не сохраняем, но пройти обязаны,
            # иначе потеряем позицию.
            if elem_type == GGUF_STRING:
                for _ in range(count):
                    self.string()
                return None
            if elem_type in _FIXED:
                fmt, size = _FIXED[elem_type]
                raw = self.read(size * count)
                if keep and count <= 64:
                    return list(struct.unpack("<%d%s" % (count, fmt[1]), raw))
                return None
            raise ValueError("массив из массивов в GGUF не поддерживается")
        raise ValueError("неизвестный тип значения GGUF: %d" % vtype)


def read_gguf_header(path: str | Path) -> dict:
    """Прочитать шапку и KV-секцию GGUF. Возвращает словарь нужных ключей.

    Бросает исключение только если файл вообще не GGUF или обрезан.
    """
    p = Path(path)
    out: dict = {}
    with open(p, "rb") as fh:
        r = _Reader(fh)
        if r.u32() != GGUF_MAGIC:
            raise ValueError("не GGUF-файл: %s" % p.name)
        version = r.u32()
        if version == 1:
            tensor_count = r.u32()
            kv_count = r.u32()
        else:
            tensor_count = r.u64()
            kv_count = r.u64()
        out["_version"] = version
        out["_tensor_count"] = tensor_count

        for _ in range(kv_count):
            key = r.string()
            vtype = r.u32()
            keep = key in _KV_KEEP or key.endswith(_KV_KEEP_SUFFIX)
            val = r.value(vtype, keep=keep)
            if keep:
                out[key] = val

        # Число параметров: конвертеры пишут его не всегда, поэтому считаем
        # по описаниям тензоров — это следующие tensor_count записей.
        if not out.get("general.parameter_count"):
            total = 0
            try:
                for _ in range(tensor_count):
                    r.string()  # имя тензора
                    n_dims = r.u32()
                    dims = [r.u64() for _ in range(n_dims)]
                    r.u32()  # тип тензора
                    r.u64()  # смещение
                    prod = 1
                    for d in dims:
                        prod *= d
                    total += prod
            except (EOFError, OSError, struct.error, ValueError):
                total = 0
            if total:
                out["_computed_params"] = total

    return out


def _read_with_gguf_lib(path: str | Path) -> dict:
    """Резервный путь: библиотека gguf, если свой разбор споткнулся."""
    import gguf  # импорт внутри — без библиотеки модуль всё равно работает

    reader = gguf.GGUFReader(str(path))
    fields = {}
    for key, fld in reader.fields.items():
        if key == "general.parameter_count":
            fields[key] = int(fld.contents())
        elif key in _KV_KEEP or key.endswith(_KV_KEEP_SUFFIX):
            try:
                val = fld.contents()
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                fields[key] = val
            except Exception:  # noqa: BLE001 — библиотека капризна к типам
                continue
    return fields


def _pick_context(raw: dict) -> int:
    """Достать длину контекста: ключ вида `<arch>.context_length`."""
    for key, val in raw.items():
        if isinstance(key, str) and key.endswith(".context_length"):
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0
    return 0


def _pick_suffixed(raw: dict, suffix: str) -> int:
    for key, val in raw.items():
        if isinstance(key, str) and key.endswith(suffix):
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0
    return 0


def read_metadata(path: str | Path) -> dict:
    """Метаданные модели в нормализованном виде.

    Ключи: name, arch, n_ctx_train, n_params, n_layers, n_embd, file_type.
    Ошибку не пробрасываем — возвращаем её в поле `error`, чтобы одна битая
    модель не рушила весь список (п. 6 ТЗ: «GGUF повреждён → пропустить»).
    """
    p = Path(path)
    result = {
        "name": "",
        "arch": "",
        "n_ctx_train": 0,
        "n_params": 0,
        "n_layers": 0,
        "n_embd": 0,
        "file_type": -1,
        "sampling": {},
        "error": "",
    }
    raw: dict = {}
    try:
        raw = read_gguf_header(p)
    except Exception:  # noqa: BLE001
        try:
            raw = _read_with_gguf_lib(p)
        except Exception as exc2:  # noqa: BLE001
            result["error"] = "не удалось прочитать GGUF: %s" % exc2
            return result

    if not raw:
        result["error"] = "метаданные GGUF пусты"
        return result

    def as_int(key: str, default: int = 0) -> int:
        val = raw.get(key)
        try:
            return int(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    result["arch"] = str(raw.get("general.architecture") or "")
    result["name"] = str(raw.get("general.name") or raw.get("general.basename") or "").strip()
    result["n_ctx_train"] = _pick_context(raw)
    result["n_params"] = as_int("general.parameter_count") or as_int("_computed_params")
    result["n_layers"] = _pick_suffixed(raw, ".block_count")
    result["n_embd"] = _pick_suffixed(raw, ".embedding_length")
    result["file_type"] = as_int("general.file_type", -1)

    # Параметры сэмплирования из GGUF (п. 3.4.2: приоритет ниже ручных правок,
    # но выше дефолтов). Пишут их немногие конвертеры — что есть, то и берём.
    sampling: dict = {}
    for name, keys in (
        ("temp", ("general.sampling.temp",)),
        ("top_p", ("general.sampling.top_p",)),
        ("top_k", ("general.sampling.top_k",)),
    ):
        for k in keys:
            if raw.get(k) is not None:
                sampling[name] = raw[k]
                break
    result["sampling"] = sampling
    return result


# ---------------------------------------------------------------------------
# кэш метаданных

CACHE_DIR = ".cache"
CACHE_FILE = "gguf_meta.json"


def _cache_path() -> Path:
    return app_root() / CACHE_DIR / CACHE_FILE


def _load_cache() -> dict:
    p = _cache_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    p = _cache_path()
    try:
        write_json_atomic(p, cache, indent=None)
    except OSError as exc:
        # Кэш метаданных — ускоритель, а не данные: без него сканер просто
        # перечитает заголовки GGUF заново. Но причину знать полезно.
        log.debug("кэш метаданных не записан: %s", exc)


# ---------------------------------------------------------------------------
# сканирование


def find_gguf_files(directory: str | Path, recursive: bool = True) -> list[Path]:
    """Найти все .gguf в директории. Кириллица в путях — норма."""
    d = Path(directory)
    if not d.is_dir():
        return []
    pattern = "**/*.gguf" if recursive else "*.gguf"
    try:
        files = [p for p in d.glob(pattern) if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: str(p).lower())


def describe_model(path: str | Path, *, use_cache: bool = True) -> ModelInfo:
    """Собрать информацию об одной модели, по возможности из кэша."""
    p = Path(path)
    try:
        st = p.stat()
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError as exc:
        return ModelInfo(
            path=str(p),
            file_name=p.name,
            size_bytes=0,
            error="файл недоступен: %s" % exc,
        )

    key = str(p.resolve()).lower()
    cache = _load_cache() if use_cache else {}
    entry = cache.get(key) if isinstance(cache, dict) else None
    meta = None
    if entry and entry.get("size") == size and entry.get("mtime") == mtime:
        meta = entry.get("meta")
        if not isinstance(meta, dict):
            meta = None

    fresh = False
    if meta is None:
        meta = read_metadata(p)
        fresh = True

    info = ModelInfo(
        path=str(p),
        file_name=p.name,
        size_bytes=size,
        name=str(meta.get("name") or ""),
        arch=str(meta.get("arch") or ""),
        n_ctx_train=int(meta.get("n_ctx_train") or 0),
        n_params=int(meta.get("n_params") or 0),
        n_layers=int(meta.get("n_layers") or 0),
        n_embd=int(meta.get("n_embd") or 0),
        file_type=int(meta.get("file_type", -1)),
        sidecar=classify_sidecar(p.name),
        sampling=dict(meta.get("sampling") or {}),
        error=str(meta.get("error") or ""),
    )
    info.quant = guess_quant(p.name)

    if fresh and use_cache:
        cache[key] = {"size": size, "mtime": mtime, "meta": meta}
        _save_cache(cache)
    return info


def scan_models(
    directory: str | Path,
    *,
    recursive: bool = True,
    include_sidecars: bool = True,
    use_cache: bool = True,
    progress: Callable[[int, int, Path], None] | None = None,
) -> list[ModelInfo]:
    """Просканировать директорию и вернуть список моделей.

    Сортировка: сначала настоящие модели, потом сайдкары; внутри — по имени.
    `progress(готово, всего, файл)` вызывается на каждом файле.
    """
    files = find_gguf_files(directory, recursive=recursive)
    out: list[ModelInfo] = []
    total = len(files)
    for i, f in enumerate(files, start=1):
        info = describe_model(f, use_cache=use_cache)
        if include_sidecars or info.is_model:
            out.append(info)
        if progress is not None:
            progress(i, total, f)
    out.sort(key=lambda m: (bool(m.sidecar), m.file_name.lower()))
    return out


def filter_models(
    models: Iterable[ModelInfo], query: str, *, only_models: bool = True
) -> list[ModelInfo]:
    """Фильтр по имени (раздел 3.1 ТЗ: «Фильтрация списка по имени»)."""
    q = (query or "").strip().lower()
    out = []
    for m in models:
        if only_models and not m.is_model:
            continue
        if not q or q in m.file_name.lower() or q in m.display_name.lower():
            out.append(m)
    return out


def model_to_dict(info: ModelInfo) -> dict:
    """Для сохранения в JSON-результаты."""
    d = asdict(info)
    d["size_gb"] = round(info.size_gb, 2)
    d["is_model"] = info.is_model
    return d
