"""Проверка памяти модели: разбирается ли лог загрузки и попадает ли в отчёт.

Повод — пункт 1 плана (`.workbuddy-ai/memory/PLAN.md`): в отчёт нужно добавить,
сколько модель заняла VRAM, RAM и диска, и куда ушли слои и эксперты.

**Откуда цифры.** По HTTP их не отдаёт ничто: в `/props` только свойства модели,
в `/metrics` — счётчики пропускной способности. Печатает их сервер, один раз, при
загрузке модели, в stdout. Здесь разбор проверяется на настоящих строках лога
сборки `b10976` (25.09.2026), снятого с живого сервера; строки скопированы как
есть. Номера слоёв в фикстуре сгенерированы тем же форматом — в живом логе их 25.

**Три ловушки, все проверяются ниже:**

1. без `-v` в логе нет ни одной строки про память: разбор обязан сказать «данных
   нет», а не показать нули;
2. `-lv 4` даёт размеры буферов, но не даёт построчной раскладки слоёв — она
   помечена уровнем `D` и появляется только с `-v`;
3. блок загрузки печатается дважды, и первый проход нулевой. Побеждать должно
   последнее значение, иначе в отчёте окажутся нули.

Части:

1. устройство и путь к модели;
2. буферы: последнее значение побеждает;
3. корзины VRAM / RAM / диск;
4. слои и признак выгрузки на GPU;
5. лог без подробного вывода — «данных нет»;
6. лог `-lv 4` — буферы есть, слоёв нет;
7. блок `memory` в JSON прогона;
8. раздел «Память» в HTML-отчёте;
9. страж — рабочий `config.json` проекта не изменён.

Запуск:  .venv/Scripts/python.exe check_memory_metrics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench import report  # noqa: E402
from llmtestbench.runner import RunResult  # noqa: E402
from llmtestbench.server_log import (  # noqa: E402
    bucket_of,
    parse_load_log,
    read_memory_report,
)

ROOT = Path(__file__).resolve().parent

ok_count = 0
fail_count = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print("  ok   %s" % name)
    else:
        fail_count += 1
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def _fingerprint(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


# ----------------------------------------------------------------------
# фикстуры: настоящие строки лога загрузки

DEVICE_LINE = (
    "0.00.169.703 I cmn  common_param:   - CUDA0   : "
    "NVIDIA GeForce RTX 5060 Ti (16283 MiB, 15172 MiB free)"
)
MODEL_LINE = "0.00.268.637 I srv    load_model: loading model 'F:/Models/Qwen3.5-0.8B.Q8_0.gguf'"
# `layer   0` — именно так, с выравниванием по ширине три.
LAYER_LINE = "0.00.536.%03d D load_tensors: layer %3d assigned to device CUDA0, is_swa = 0"

OFFLOAD_LINES = [
    "0.00.540.362 I load_tensors: offloading output layer to GPU",
    "0.00.540.363 I load_tensors: offloading 23 repeating layers to GPU",
    "0.00.540.363 I load_tensors: offloaded 25/25 layers to GPU",
]

# Первый проход загрузки: всё по нулям. Он идёт ПЕРВЫМ — ловушка №3.
PASS_ONE = [
    "0.00.519.435 I load_tensors:        CUDA0 model buffer size =     0.00 MiB",
    "0.00.519.435 I load_tensors:    CUDA_Host model buffer size =     0.00 MiB",
    "0.00.525.503 I llama_context:  CUDA_Host  output buffer size =     3.79 MiB",
    "0.00.525.595 I llama_kv_cache:      CUDA0 KV buffer size =     0.00 MiB",
]

# Второй проход: настоящие значения.
PASS_TWO = [
    "0.00.979.528 I load_tensors:   CPU_Mapped model buffer size =   257.66 MiB",
    "0.00.979.529 I load_tensors:        CUDA0 model buffer size =   763.78 MiB",
    "0.01.227.385 I llama_context:  CUDA_Host  output buffer size =     3.79 MiB",
    "0.01.227.601 I llama_kv_cache:      CUDA0 KV buffer size =    24.00 MiB",
    "0.01.230.153 I llama_memory_recurrent:      CUDA0 RS buffer size =    77.06 MiB",
    "0.01.236.047 I sched_reserve:      CUDA0 compute buffer size =    42.58 MiB",
    "0.01.236.051 I sched_reserve:  CUDA_Host compute buffer size =     6.02 MiB",
]

# Лог сервера, запущенного без `-v`: настоящие десять строк, и ни одной про память.
QUIET_LOG = "\n".join(
    [
        "0.00.001.215 I srv  llama_server: initializing ...",
        "0.00.141.547 I cmn  common_param: common_params_print_info: "
        "verbosity = 3 (adjust with the `-lv N` CLI arg)",
        "0.00.141.668 I srv  init_listene: The UI is disabled",
        "0.00.141.670 I srv  init_listene: Use --ui/--no-ui "
        "(or deprecated --webui/--no-webui) to enable/disable",
        "0.00.141.740 W srv  llama_server: security: no API key is set "
        "and CORS allows all origins",
        "0.00.155.981 I srv    load_model: loading model 'F:/Models/Qwen3.5-0.8B.Q8_0.gguf'",
        "0.01.283.499 I cmn          init: llama threadpool init, n_threads = 4",
        "0.01.352.960 I srv    load_model: initializing, n_slots = 4, "
        "n_ctx_slot = 2048, kv_unified = 'true'",
        "0.01.372.616 I srv  llama_server: model loaded",
        "0.01.372.619 I srv  llama_server: listening on http://127.0.0.1:18199",
    ]
)


def verbose_log() -> str:
    """Лог с `-v`: устройство, модель, 25 слоёв, два прохода буферов."""
    parts = [DEVICE_LINE, MODEL_LINE]
    parts += [LAYER_LINE % (900 + i, i) for i in range(25)]
    parts += OFFLOAD_LINES
    parts += PASS_ONE
    parts += PASS_TWO
    return "\n".join(parts) + "\n"


def lv4_log() -> str:
    """Лог с `-lv 4`: буферы есть, построчной раскладки слоёв нет."""
    parts = [DEVICE_LINE, MODEL_LINE, *OFFLOAD_LINES, *PASS_TWO]
    return "\n".join(parts) + "\n"


# ----------------------------------------------------------------------


def part_device_and_model() -> None:
    print("\n1. Устройство и путь к модели")
    rep = parse_load_log(verbose_log())
    check("лог разобран", rep is not None)
    if rep is None:
        return
    check(
        "путь к модели взят из строки загрузки",
        rep.model_path == "F:/Models/Qwen3.5-0.8B.Q8_0.gguf",
        rep.model_path,
    )
    dev = rep.devices.get("CUDA0") or {}
    check("устройство найдено", bool(dev), str(rep.devices))
    check("имя устройства", dev.get("name") == "NVIDIA GeForce RTX 5060 Ti", str(dev.get("name")))
    check(
        "всего памяти устройства, МиБ", dev.get("total_mib") == 16283.0, str(dev.get("total_mib"))
    )
    check("свободно на старте, МиБ", dev.get("free_mib") == 15172.0, str(dev.get("free_mib")))


def part_buffers_last_wins() -> None:
    print("\n2. Буферы: побеждает последнее значение (ловушка №3)")
    rep = parse_load_log(verbose_log())
    if rep is None:
        check("лог разобран", False)
        return
    check(
        "буфер модели на GPU — из второго прохода, а не нули",
        rep.buffers.get("CUDA0 model") == 763.78,
        str(rep.buffers.get("CUDA0 model")),
    )
    check(
        "KV-кэш — из второго прохода, а не нули",
        rep.buffers.get("CUDA0 KV") == 24.0,
        str(rep.buffers.get("CUDA0 KV")),
    )
    check(
        "отображённый с диска буфер прочитан",
        rep.buffers.get("CPU_Mapped model") == 257.66,
        str(rep.buffers.get("CPU_Mapped model")),
    )
    check(
        "буфер host-памяти прочитан",
        rep.buffers.get("CUDA_Host output") == 3.79,
        str(rep.buffers.get("CUDA_Host output")),
    )
    check("буферов ровно восемь", len(rep.buffers) == 8, str(sorted(rep.buffers)))


def part_buckets() -> None:
    print("\n3. Корзины VRAM / RAM / диск")
    check("CUDA_Host — это RAM, а не видеопамять", bucket_of("CUDA_Host") == "ram")
    check("CPU_Mapped — это диск", bucket_of("CPU_Mapped") == "disk")
    check("CPU — это RAM", bucket_of("CPU") == "ram")
    check("CUDA0 — это VRAM", bucket_of("CUDA0") == "vram")
    check("Vulkan0 — это VRAM", bucket_of("Vulkan0") == "vram")

    rep = parse_load_log(verbose_log())
    if rep is None:
        check("лог разобран", False)
        return
    # 763.78 + 24.00 + 77.06 + 42.58
    check("VRAM — сумма буферов видеопамяти", rep.total("vram") == 907.42, str(rep.total("vram")))
    # 3.79 + 6.02
    check("RAM — сумма host-буферов", rep.total("ram") == 9.81, str(rep.total("ram")))
    check("диск — отображённый файл", rep.total("disk") == 257.66, str(rep.total("disk")))
    check(
        "корзины не пересекаются: сумма всех трёх равна сумме буферов",
        round(rep.total("vram") + rep.total("ram") + rep.total("disk"), 2)
        == round(sum(rep.buffers.values()), 2),
        "%.2f против %.2f"
        % (rep.total("vram") + rep.total("ram") + rep.total("disk"), sum(rep.buffers.values())),
    )


def part_layers() -> None:
    print("\n4. Слои и выгрузка на GPU")
    rep = parse_load_log(verbose_log())
    if rep is None:
        check("лог разобран", False)
        return
    check(
        "слоёв ровно 25, дубли из двух проходов схлопнулись",
        len(rep.layers) == 25,
        str(len(rep.layers)),
    )
    check("нулевой слой на CUDA0", rep.layers.get(0) == "CUDA0")
    check("последний слой на CUDA0", rep.layers.get(24) == "CUDA0")
    check(
        "раскладка по устройствам",
        rep.layers_by_device() == {"CUDA0": 25},
        str(rep.layers_by_device()),
    )
    check("всего слоёв из строки offloaded", rep.layers_total == 25, str(rep.layers_total))
    check("выгружено на GPU", rep.offloaded == 25, str(rep.offloaded))
    check("выходной слой на GPU", rep.output_on_gpu is True)


def part_quiet_log() -> None:
    print("\n5. Лог без `-v`: данных о памяти нет (ловушка №1)")
    check("разбор возвращает None, а не нули", parse_load_log(QUIET_LOG) is None)
    check(
        "в самом логе нет ни строки про буферы",
        "buffer size" not in QUIET_LOG,
        "фикстура перестала соответствовать живому логу без -v",
    )


def part_lv4_log() -> None:
    print("\n6. Лог с `-lv 4`: буферы есть, слоёв нет (ловушка №2)")
    rep = parse_load_log(lv4_log())
    check("лог разобран", rep is not None)
    if rep is None:
        return
    check("VRAM считается", rep.total("vram") == 907.42, str(rep.total("vram")))
    check("раскладки по слоям нет", rep.layers == {}, str(rep.layers))
    check(
        "но общее число слоёв известно из строки offloaded",
        rep.layers_total == 25,
        str(rep.layers_total),
    )


def part_run_json() -> None:
    print("\n7. Блок `memory` в JSON прогона")
    run = RunResult(
        run_id="probe_run",
        model="Qwen3.5-0.8B.Q8_0.gguf",
        base_url="http://127.0.0.1:8080",
        set_id="probe",
        set_name="Проверка",
    )
    check(
        "по умолчанию блок пуст — «не измерено», а не ноль",
        run.as_dict().get("memory") == {},
        str(run.as_dict().get("memory")),
    )

    rep = parse_load_log(verbose_log())
    if rep is None:
        check("лог разобран", False)
        return
    run.memory = rep.as_dict()
    data = run.as_dict().get("memory") or {}
    check("блок попал в JSON", bool(data))
    check("VRAM в JSON", data.get("vram_mib") == 907.42, str(data.get("vram_mib")))
    check("RAM в JSON", data.get("ram_mib") == 9.81, str(data.get("ram_mib")))
    check("диск в JSON", data.get("disk_mib") == 257.66, str(data.get("disk_mib")))
    check(
        "источник помечен как лог загрузки",
        data.get("source") == "load-log",
        str(data.get("source")),
    )
    check(
        "слои по устройствам в JSON", data.get("layers") == {"CUDA0": 25}, str(data.get("layers"))
    )
    check(
        "построчная раскладка в JSON есть",
        data.get("layer_devices", {}).get("24") == "CUDA0",
        str(data.get("layer_devices")),
    )
    check("устройства в JSON", "CUDA0" in (data.get("devices") or {}), str(data.get("devices")))


def part_report_html() -> None:
    print("\n8. Раздел «Память» в HTML-отчёте")
    rep = parse_load_log(verbose_log())
    if rep is None:
        check("лог разобран", False)
        return

    run = RunResult(
        run_id="probe_run",
        model="Qwen3.5-0.8B.Q8_0.gguf",
        base_url="http://127.0.0.1:8080",
        set_id="probe",
        set_name="Проверка",
    )
    run.memory = rep.as_dict()
    run.compute_summary()
    html = report.build_html([run.as_dict()])
    check("плитка VRAM есть", "VRAM (буферы)" in html)
    check("плитка RAM есть", "RAM (host)" in html)
    check("плитка диска есть", "С диска (mmap)" in html)
    check("значение VRAM показано", "907.4 МиБ" in html)
    check("значение RAM показано", "9.8 МиБ" in html)
    check("значение диска показано", "257.7 МиБ" in html)
    check("раскладка слоёв показана", "CUDA0: 25" in html)
    check("имя устройства показано", "NVIDIA GeForce RTX 5060 Ti" in html)
    check("строка буферов показана", "CUDA0 model — 763.8 МиБ" in html)

    bare = RunResult(
        run_id="bare_run",
        model="Qwen3.5-0.8B.Q8_0.gguf",
        base_url="http://127.0.0.1:8080",
        set_id="probe",
        set_name="Проверка",
    )
    bare.compute_summary()
    bare_html = report.build_html([bare.as_dict()])
    check("без данных отчёт говорит «нет данных», а не показывает нули", "нет данных" in bare_html)
    check("и не рисует плитку VRAM", "VRAM (буферы)" not in bare_html)


def part_read_missing_file() -> None:
    print("\n9. Чтение лога с диска")
    missing = ROOT / ".cache" / "нет-такого-лога.txt"
    check(
        "отсутствующий файл — это не ошибка, а «нет данных»", read_memory_report(missing) is None
    )


def main() -> int:
    print("Проверка памяти модели: лог загрузки → отчёт")
    print("=" * 72)

    config_path = ROOT / "config.json"
    before = _fingerprint(config_path)

    part_device_and_model()
    part_buffers_last_wins()
    part_buckets()
    part_layers()
    part_quiet_log()
    part_lv4_log()
    part_run_json()
    part_report_html()
    part_read_missing_file()

    print("\n10. Страж: рабочий config.json проекта")
    check(
        "config.json проекта не изменён проверкой",
        _fingerprint(config_path) == before,
        "%s → %s" % (before, _fingerprint(config_path)),
    )

    print("\n" + "=" * 72)
    print("Пройдено: %d   Провалено: %d" % (ok_count, fail_count))
    print("=" * 72)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
