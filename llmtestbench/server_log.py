"""Разбор лога загрузки llama-server: память модели и раскладка слоёв.

**Почему лог, а не запрос к серверу.** По HTTP этих цифр не отдаёт ничто: в
`/props` лежат только свойства модели (`default_generation_settings`,
`model_path`, `chat_template`, `build_info`), в `/metrics` — счётчики
пропускной способности и токенов. Сколько весов легло в VRAM, сколько осталось
в оперативной памяти и сколько ушло на диск, сервер печатает один раз, при
загрузке модели, в stdout. Проверено по README llama.cpp и на живой сборке
`b10976` 25.09.2026.

**Две ловушки, обе проверены на живом сервере:**

1. **Без `-v` этих строк нет вообще.** На умолчании (`-lv 3`) сервер печатает
   десять строк, и ни одной про память. `-lv 4` даёт размеры буферов, но не даёт
   построчной раскладки слоёв: она помечена уровнем `D` и появляется только с
   `-v`. Значит, чтобы ответить на вопрос «какие слои и эксперты куда ушли»,
   сервер надо запускать с `-v`.
2. **Блок загрузки печатается дважды** — сначала примерочный проход, где все
   буферы нулевые, потом настоящий. Поэтому по каждому ключу берётся **последнее**
   значение: если оставить первое, в отчёт попадут нули.

Модуль без Qt: разбор нужен и проверкам, и отчёту, и прогону.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# `0.00.979.528 I load_tensors:        CUDA0 model buffer size =   763.78 MiB`
# Перед именем модуля стоят время и уровень (`0.00.979.528 I `). Вид буфера —
# `model`, `KV`, `RS`, `output`, `compute`; список не закрываем, берём любое
# слово, чтобы разбор не сломался на новом виде буфера в новой сборке.
_RE_BUFFER = re.compile(
    r"^(?:\S+ [A-Z] )?(?P<module>\w+):\s+(?P<device>\S+)\s+"
    r"(?P<kind>[A-Za-z_]+) buffer size =\s+(?P<mib>\d+\.\d+) MiB",
    re.MULTILINE,
)

# `0.00.536.904 D load_tensors: layer   0 assigned to device CUDA0, is_swa = 0`
_RE_LAYER = re.compile(r"layer\s+(\d+) assigned to device (\w+)")

# `0.00.540.363 I load_tensors: offloaded 25/25 layers to GPU`
_RE_OFFLOAD = re.compile(r"offloaded (\d+)/(\d+) layers to (\w+)")
_RE_OUTPUT_LAYER = re.compile(r"offloading output layer to")

# `- CUDA0   : NVIDIA GeForce RTX 5060 Ti (16283 MiB, 15172 MiB free)`
_RE_DEVICE = re.compile(r"(\w+)\s*:\s*([^(:]+?)\s*\((\d+) MiB,\s*(\d+) MiB free\)")

# `0.00.268.637 I srv    load_model: loading model 'F:/Models/Qwen3.5-0.8B.Q8_0.gguf'`
_RE_MODEL = re.compile(r"load_model: loading model '([^']+)'")


def bucket_of(device: str) -> str:
    """Куда лёг буфер: `vram`, `ram` или `disk`.

    Три имени устройств, которые здесь важно не перепутать:

    * `CPU_Mapped` — отображённый файл. Веса остаются в файле на диске и
      подтягиваются страницами по мере обращения, в оперативную память они не
      скопированы. Это и есть честный ответ на «сколько ушло на SSD».
    * `CUDA_Host` — **host**-буфер: память на стороне процессора (в том числе
      закреплённая), а не видеопамять. Название сбивает: `CUDA` в нём есть, а
      VRAM нет.
    * `CUDA0`, `Metal`, `Vulkan0` — собственно видеопамять.
    """
    upper = device.upper()
    if "MAPPED" in upper:
        return "disk"
    if upper.startswith("CPU") or upper.endswith("_HOST"):
        return "ram"
    return "vram"


@dataclass
class MemoryReport:
    """Что лог загрузки говорит о памяти и о раскладке слоёв."""

    # Ключ — «устройство вид», например `CUDA0 model`. Значение — МиБ.
    buffers: dict[str, float] = field(default_factory=dict)
    # Тот же ключ → в какую корзину его класть. Хранится отдельно, чтобы не
    # разбирать имя обратно: у устройства имя без пробелов, но полагаться на это
    # при суммировании не хочется.
    buckets: dict[str, str] = field(default_factory=dict)
    # Номер слоя → устройство. Две записи на слой из лога схлопываются сами.
    layers: dict[int, str] = field(default_factory=dict)
    # Устройство → его паспорт из лога: имя, всего МиБ, свободно МиБ.
    devices: dict[str, dict[str, object]] = field(default_factory=dict)
    # Путь к модели из последней загрузки в логе. Нужен, чтобы не выдать цифры
    # прошлого сервера за память текущей модели: лог дописывается, и в нём
    # запросто лежит чужая загрузка.
    model_path: str = ""
    offloaded: int = 0
    layers_total: int = 0
    output_on_gpu: bool = False

    def total(self, bucket: str) -> float:
        """Сумма буферов одной корзины, МиБ."""
        return round(
            sum(mib for key, mib in self.buffers.items() if self.buckets.get(key) == bucket),
            2,
        )

    def layers_by_device(self) -> dict[str, int]:
        """Сколько слоёв ушло на каждое устройство."""
        out: dict[str, int] = {}
        for device in self.layers.values():
            out[device] = out.get(device, 0) + 1
        return out

    def as_dict(self) -> dict[str, object]:
        """Представление для JSON прогона (блок `memory`)."""
        return {
            "source": "load-log",
            "model_path": self.model_path,
            "vram_mib": self.total("vram"),
            "ram_mib": self.total("ram"),
            "disk_mib": self.total("disk"),
            "buffers": dict(self.buffers),
            "layers": self.layers_by_device(),
            "layer_devices": {str(i): d for i, d in sorted(self.layers.items())},
            "layers_total": self.layers_total,
            "offloaded": self.offloaded,
            "output_on_gpu": self.output_on_gpu,
            "devices": {k: dict(v) for k, v in self.devices.items()},
        }


def parse_load_log(text: str) -> MemoryReport | None:
    """Разобрать текст лога загрузки. `None` — если про память в нём ничего нет.

    `None` — обычный ответ: так выглядит лог сервера, запущенного без `-v`.
    Отчёт обязан отличать «данных нет» от «память нулевая», поэтому здесь не
    пустой отчёт, а именно `None`.
    """
    report = MemoryReport()

    for match in _RE_BUFFER.finditer(text):
        device = match["device"]
        kind = match["kind"]
        key = "%s %s" % (device, kind)
        # Последнее значение побеждает: второй проход загрузки — настоящий.
        report.buffers[key] = float(match["mib"])
        report.buckets[key] = bucket_of(device)

    for match in _RE_LAYER.finditer(text):
        report.layers[int(match.group(1))] = match.group(2)

    offload = _RE_OFFLOAD.search(text)
    if offload:
        report.offloaded = int(offload.group(1))
        report.layers_total = int(offload.group(2))
    report.output_on_gpu = bool(_RE_OUTPUT_LAYER.search(text))

    for match in _RE_DEVICE.finditer(text):
        report.devices[match.group(1)] = {
            "name": match.group(2),
            "total_mib": float(match.group(3)),
            "free_mib": float(match.group(4)),
        }

    loaded = _RE_MODEL.findall(text)
    if loaded:
        report.model_path = loaded[-1]

    if not report.buffers and not report.layers:
        return None
    return report


def read_memory_report(path: str | Path) -> MemoryReport | None:
    """Прочитать лог загрузки с диска и разобрать его.

    Файла нет — это не ошибка: лог пишется только тогда, когда сервер поднимает
    само приложение. В режиме «сервер уже поднят» (`use_existing_server`) файла
    не будет, и отчёт должен честно сказать «данных нет».
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_load_log(text)
