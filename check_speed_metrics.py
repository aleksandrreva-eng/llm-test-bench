"""Проверка метрик скорости: получаются ли они и показываются ли везде.

Поводом был вопрос Инка: «получает ли приложение метрики скорости и везде ли
они показываются». Проверка отвечает на обе половины.

**Получаются.** Скорости приходят от сервера в блоке `timings`, а он есть только
у сборок, понимающих `stream_options`. Старая сборка это поле игнорирует, и три
метрики (`predicted_per_second`, `prompt_ms`, `prompt_per_second`) молча
оставались нулями: в отчёте и в сравнении было «0 t/s» у модели, которая выдавала
сотню. Поэтому проверка поднимает два мини-сервера — с `timings` и без — и
сверяет оба пути. Второй путь обязан дать **оценку** по своим замерам и пометить
её флагом `speeds_estimated`.

**Показываются.** Скорость префилла (`prompt_tokens_per_sec`) считалась и
писалась в JSON, но не выводилась нигде: ни в отчёте, ни в карточке кейса, ни в
логе прогона, ни в сравнении. А это главная метрика набора `speed` — префилл
различает модели в разы, тогда как генерация у них держится примерно одинаково.
В утверждённом макете колонка «Префилл, t/s» есть (`docs/ui-prototype.html`),
так что это была дырка, а не решение.

Части:

1. **Сервер отдаёт `timings`** — скорости берутся как есть, оценка не включается;
2. **Сервер `timings` не отдаёт** — скорости считаются по замерам, флаг стоит,
   токены при этом посчитаны (а не ноль);
3. **Цепочка до результата прогона** — `TestRunner.run_set` кладёт в кейс
   `prompt_tokens_per_sec` и `speeds_estimated`, и то же уезжает в `as_dict()`;
4. **Строка метрик** (`report.case_metrics_text` — она же в отчёте и в карточке
   кейса) содержит скорость префилла;
5. **Агрегат** — `RunStats.prompt_tokens_per_sec` усредняется по кейсам и
   сливается взвешенно по нескольким файлам набора;
6. **Сравнение** — строка «Префилл (avg)» есть в таблице и заполняется;
7. **Занятость сервера** — чужой запрос виден, назван, записан в прогон и
   прогон из-за него не отменяется (решает человек);
8. **Чем поднят сервер** — запись о запуске есть в `params` всегда, включая
   честное «неизвестно», когда источник молчит или падает;
9. **Страж** — рабочий `config.json` проекта проверкой не изменён.

Запуск:  .venv/Scripts/python.exe check_speed_metrics.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from llmtestbench import report  # noqa: E402
from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.local_agent import (  # noqa: E402
    LocalAgent,
    estimate_generation_speed,
    estimate_prompt_speed,
)
from llmtestbench.results_index import (  # noqa: E402
    group_records,
    load_runs,
)
from llmtestbench.runner import TestRunner  # noqa: E402
from llmtestbench.server_cmd import describe_server  # noqa: E402
from llmtestbench.testsets import load_test_set  # noqa: E402
from llmtestbench.ui.compare_tab import ROWS, CompareTab  # noqa: E402

ROOT = Path(__file__).resolve().parent
PORT = 18101
PROMPT_TOKENS = 40
ANSWER_TOKENS = 5

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
# мини-сервер: два режима — с timings и без

MODE = {
    "timings": True,
    # Состояние слотов для проверки занятости сервера: `slots` выключает
    # эндпоинт целиком (сборка без `--slots`), `busy` — занятый слот,
    # `tick` — «номер задачи меняется на каждом обращении».
    "slots": True,
    "busy": False,
    "task_id": 1,
    "tick": False,
}


class Handler(BaseHTTPRequestHandler):
    """llama-server в миниатюре: /props, /tokenize, поток ответа.

    В режиме `MODE["timings"] = False` ведёт себя как старая сборка: поле
    `stream_options` игнорируется, `usage` и `timings` в поток не попадают.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — глушим стандартный лог
        pass

    def _json(self, payload: dict, code: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        if self.path == "/props":
            self._json(
                {
                    "model_path": "probe-model-Q4_K_M.gguf",
                    "default_generation_settings": {"n_ctx": 4096},
                }
            )
        elif self.path == "/v1/models":
            self._json(
                {"object": "list", "data": [{"id": "probe-model-Q4_K_M.gguf", "object": "model"}]}
            )
        elif self.path == "/health":
            self._json({"status": "ok"})
        elif self.path == "/slots":
            if not MODE["slots"]:
                self._json({"error": "not found"}, 404)
                return
            if MODE["tick"]:
                MODE["task_id"] += 1
            self._json(
                [
                    {
                        "id": 0,
                        "n_ctx": 4096,
                        "is_processing": MODE["busy"],
                        "id_task": MODE["task_id"],
                    }
                ]
            )
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/tokenize":
            content = str(body.get("content") or "")
            self._json({"tokens": list(range(max(1, len(content) // 4)))})
            return

        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(obj: dict) -> None:
            data = ("data: %s\n\n" % json.dumps(obj)).encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

        for word in ("Ответ", " модели", " на", " задание", "."):
            time.sleep(0.04)
            chunk({"choices": [{"delta": {"content": word}, "index": 0}]})
        chunk({"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]})

        if MODE["timings"]:
            chunk(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": PROMPT_TOKENS, "completion_tokens": ANSWER_TOKENS},
                }
            )
            chunk(
                {
                    "choices": [],
                    "timings": {
                        "prompt_ms": 180.0,
                        "prompt_per_second": 222.2,
                        "predicted_per_second": 90.5,
                    },
                }
            )
        self.wfile.write(b"0\r\n\r\n")


def start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return server


def make_agent() -> LocalAgent:
    return LocalAgent(base_url="http://127.0.0.1:%d" % PORT, retries=1, retry_delay=0.1)


def make_cfg(tmp: Path, results: Path) -> AppConfig:
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        tests_dir=str(ROOT / "tests"),
        results_dir=str(results),
        reports_dir=str(tmp / "reports"),
        logs_dir=str(tmp / "logs"),
        sessions_dir=str(tmp / "sessions"),
        history_db_name=str(tmp / "history.db"),
    )
    cfg.bind(tmp / "config.json")
    cfg.resolve_dirs()
    cfg.ensure_dirs()
    return cfg


# ----------------------------------------------------------------------
# фикстуры для агрегата и сравнения


def case_dict(case_id: str, prompt_tps: float, gen_tps: float, estimated: bool = False) -> dict:
    return {
        "case_id": case_id,
        "name": case_id,
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "prompt_ms": 200.0,
        "prompt_tokens_per_sec": prompt_tps,
        "ttft_ms": 220.0,
        "total_ms": 2400.0,
        "tokens_per_sec": gen_tps,
        "passed": True,
        "counted": True,
        "speeds_estimated": estimated,
    }


def write_run(results: Path, run_id: str, batch: str, cases: list[dict]) -> Path:
    path = results / ("%s.json" % run_id)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "batch_id": batch,
                "model": "probe-model-Q4_K_M.gguf",
                "base_url": "http://127.0.0.1:%d" % PORT,
                "set_id": "probe_set",
                "set_name": "Набор для проверки",
                "set_version": "1.0.0",
                "context_size": 4096,
                "started": "2026-09-25T10:00:00",
                "finished": "2026-09-25T10:01:00",
                "seconds": 60.0,
                "status": "finished",
                "params": {},
                "summary": {
                    "total": len(cases),
                    "passed": len(cases),
                    "failed": 0,
                    "skipped": 0,
                    "stand_errors": 0,
                    "empty_answers": 0,
                    "counted": len(cases),
                    "score": 1.0,
                },
                "cases": cases,
                "app_version": "0.1.0",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


# ----------------------------------------------------------------------
# 1-2. получение метрик


def part_fetch() -> None:
    print("\n1. Сервер отдаёт timings — скорости берутся как есть")
    agent = make_agent()
    MODE["timings"] = True
    comp = agent.complete([{"role": "user", "content": "привет"}], max_tokens=64)
    check("ответ получен", comp.text == "Ответ модели на задание.", comp.text)
    check(
        "скорость генерации от сервера",
        comp.predicted_per_second == 90.5,
        str(comp.predicted_per_second),
    )
    check("время префилла от сервера", comp.prompt_ms == 180.0, str(comp.prompt_ms))
    check(
        "скорость префилла от сервера",
        comp.prompt_per_second == 222.2,
        str(comp.prompt_per_second),
    )
    check("оценка не включалась", comp.speeds_estimated is False)
    check("токены промпта из usage", comp.prompt_tokens == PROMPT_TOKENS, str(comp.prompt_tokens))

    print("\n2. Старая сборка: timings не приходят — считаем по своим замерам")
    MODE["timings"] = False
    comp = agent.complete([{"role": "user", "content": "привет"}], max_tokens=64)
    check("ответ всё равно получен", comp.text == "Ответ модели на задание.", comp.text)
    check(
        "скорость генерации не ноль", comp.predicted_per_second > 0, str(comp.predicted_per_second)
    )
    check("время префилла не ноль", comp.prompt_ms > 0, str(comp.prompt_ms))
    check("оценка помечена флагом", comp.speeds_estimated is True)
    check(
        "токены ответа посчитаны через /tokenize",
        comp.completion_tokens > 0,
        str(comp.completion_tokens),
    )
    check(
        "оценка совпадает с формулой",
        comp.predicted_per_second
        == estimate_generation_speed(comp.completion_tokens, comp.seconds, comp.ttft_ms),
        "%s против %s"
        % (
            comp.predicted_per_second,
            estimate_generation_speed(comp.completion_tokens, comp.seconds, comp.ttft_ms),
        ),
    )
    check("токены промпта не пришли (usage нет)", comp.prompt_tokens == 0, str(comp.prompt_tokens))

    print("\n   вспомогательные формулы")
    check(
        "скорость генерации: время префилла вычтено",
        estimate_generation_speed(100, 2.0, 500.0) == 66.67,
        str(estimate_generation_speed(100, 2.0, 500.0)),
    )
    check(
        "скорость генерации: нулевое время → 0", estimate_generation_speed(100, 0.5, 500.0) == 0.0
    )
    check("скорость генерации: нет токенов → 0", estimate_generation_speed(0, 2.0, 500.0) == 0.0)
    check(
        "скорость префилла: 40 токенов за 52.8 мс ≈ 757.6",
        estimate_prompt_speed(40, 52.8) == 757.58,
        str(estimate_prompt_speed(40, 52.8)),
    )
    check("скорость префилла: нет времени → 0", estimate_prompt_speed(40, 0.0) == 0.0)


# ----------------------------------------------------------------------
# 3. цепочка до результата прогона


def part_chain(tmp: Path) -> None:
    print("\n3. Цепочка: прогон кейса кладёт скорости в результат")
    agent = make_agent()
    cfg = make_cfg(tmp, tmp / "results")
    runner = TestRunner(agent, cfg)
    test_set = load_test_set(ROOT / "tests" / "chat_single")

    MODE["timings"] = False
    run = runner.run_set(test_set, runs=1, limit=1)
    check("прогон вернул один кейс", len(run.cases) == 1, str(len(run.cases)))
    case = run.cases[0]
    check(
        "скорость префилла посчитана",
        case.prompt_tokens_per_sec > 0,
        str(case.prompt_tokens_per_sec),
    )
    check("скорость генерации посчитана", case.tokens_per_sec > 0, str(case.tokens_per_sec))
    check("оценка помечена", case.speeds_estimated is True)
    check(
        "токены промпта уточнены через /tokenize", case.prompt_tokens > 0, str(case.prompt_tokens)
    )
    check(
        "скорость префилла согласована с токенами",
        case.prompt_tokens_per_sec
        == estimate_prompt_speed(case.prompt_tokens, case.prompt_ms or case.ttft_ms),
        "%s против %s"
        % (
            case.prompt_tokens_per_sec,
            estimate_prompt_speed(case.prompt_tokens, case.prompt_ms or case.ttft_ms),
        ),
    )

    payload = run.as_dict()
    first = payload["cases"][0]
    check(
        "в JSON прогона есть скорость префилла",
        first.get("prompt_tokens_per_sec", 0) > 0,
        str(first.get("prompt_tokens_per_sec")),
    )
    check("в JSON прогона есть флаг оценки", first.get("speeds_estimated") is True)

    MODE["timings"] = True
    run = runner.run_set(test_set, runs=1, limit=1)
    case = run.cases[0]
    check("с timings оценка не включается", case.speeds_estimated is False)
    check(
        "с timings скорость префилла от сервера",
        case.prompt_tokens_per_sec == 222.2,
        str(case.prompt_tokens_per_sec),
    )


# ----------------------------------------------------------------------
# 4. строка метрик


def part_metrics_text() -> None:
    print("\n4. Строка метрик содержит скорость префилла")
    text = report.case_metrics_text(case_dict("probe_001", 222.2, 90.5))
    check("скорость префилла в строке", "222.2 t/s" in text, text)
    check("время префилла на месте", "префилл 200.0 мс" in text, text)
    check("скорость генерации на месте", "90.5 t/s" in text, text)

    estimated = report.case_metrics_text(case_dict("probe_002", 757.6, 29.5, estimated=True))
    check(
        "оценочные скорости помечены «≈»",
        "≈757.6 t/s" in estimated and "≈29.5 t/s" in estimated,
        estimated,
    )
    check("точные скорости без пометки", "≈" not in text, text)


# ----------------------------------------------------------------------
# 5-6. агрегат и сравнение


def part_aggregate(tmp: Path) -> None:
    print("\n5. Агрегат: среднее по кейсам и слияние по файлам")
    results = tmp / "agg"
    results.mkdir(parents=True, exist_ok=True)
    write_run(
        results,
        "probe_set_20260925_100000",
        "probe_20260925_100000",
        [case_dict("probe_001", 100.0, 90.0), case_dict("probe_002", 300.0, 110.0)],
    )
    cfg = make_cfg(tmp, results)
    groups = group_records(load_runs(cfg))
    check("собран один прогон", len(groups) == 1, str(len(groups)))
    stats = groups[0].stats
    check(
        "средняя скорость префилла по кейсам",
        stats.prompt_tokens_per_sec == 200.0,
        str(stats.prompt_tokens_per_sec),
    )
    check("средняя скорость генерации", stats.tokens_per_sec == 100.0, str(stats.tokens_per_sec))
    check("оценка не помечена (в фикстуре точные числа)", stats.speeds_estimated is False)

    # два файла одного набора: у второго втрое больше кейсов и скорость 600
    results2 = tmp / "agg2"
    results2.mkdir(parents=True, exist_ok=True)
    write_run(
        results2,
        "probe_set_20260925_100000",
        "probe_20260925_100000",
        [case_dict("probe_001", 100.0, 90.0)],
    )
    write_run(
        results2,
        "probe_set_20260925_100001",
        "probe_20260925_100000",
        [
            case_dict("probe_003", 600.0, 120.0),
            case_dict("probe_004", 600.0, 120.0),
            case_dict("probe_005", 600.0, 120.0),
        ],
    )
    cfg2 = make_cfg(tmp / "agg2cfg", results2)
    groups2 = group_records(load_runs(cfg2))
    merged = groups2[0].set_stats[0]
    check(
        "набор один, хотя файлов два",
        len(groups2[0].set_stats) == 1,
        str(len(groups2[0].set_stats)),
    )
    check(
        "слияние взвешенно по числу кейсов (1×100 + 3×600) / 4 = 475",
        merged.prompt_tokens_per_sec == 475.0,
        str(merged.prompt_tokens_per_sec),
    )

    print("\n6. Сравнение: строка «Префилл (avg)» есть и заполняется")
    keys = [key for key, _label, _better, _expand in ROWS]
    check("строка префилла в таблице", "prompt_tokens_per_sec" in keys, str(keys))
    labels = {key: label for key, label, _b, _e in ROWS}
    check(
        "подпись строки",
        labels.get("prompt_tokens_per_sec") == "Префилл (avg)",
        str(labels.get("prompt_tokens_per_sec")),
    )
    check(
        "строка префилла разворачивается по наборам",
        ("prompt_tokens_per_sec" in {key for key, _l, _b, expand in ROWS if expand}),
    )

    app = QApplication.instance() or QApplication(sys.argv)
    # Второй прогон — чтобы было с чем сравнивать: «★» ставится только при
    # различии, на единственном столбце лучшего значения нет по определению.
    write_run(
        results,
        "probe_set_20260925_110000",
        "probe_20260925_110000",
        [case_dict("probe_001", 80.0, 90.0), case_dict("probe_002", 120.0, 110.0)],
    )
    tab = CompareTab(cfg)
    tab.load(sorted(results.glob("*.json")))
    check("в сравнении два прогона", len(tab._groups) == 2, str(len(tab._groups)))
    cell = tab._run_cell(tab._groups[0], "prompt_tokens_per_sec")
    check("ячейка префилла заполнена", cell.value == 200.0, str(cell.value))
    check("показывается одним знаком после точки", cell.text == "200.0", cell.text)
    check(
        "в подсказке сказано, что это скорость промпта",
        "промпт" in cell.tooltip.lower(),
        cell.tooltip,
    )
    best = tab._best_values(tab._groups)
    check(
        "лучшее значение в строке префилла найдено (200 против 100)",
        best.get("prompt_tokens_per_sec") == 200.0,
        str(best.get("prompt_tokens_per_sec")),
    )
    check("объект приложения жив", app is not None)


# ----------------------------------------------------------------------


def part_busy(tmp: Path) -> None:
    """Занятость сервера: предупреждаем, но замеры из-за неё не ломаем."""
    print("\n7. Занятость сервера: чужой запрос виден и записан в прогон")
    agent = make_agent()
    MODE.update({"slots": True, "busy": False, "task_id": 1, "tick": False})

    check("свободный сервер — предупреждения нет", agent.server_busy() is None)

    MODE["busy"] = True
    reason = agent.server_busy()
    check("занятый слот виден", reason is not None, str(reason))
    check("в причине назван слот", "слот 0" in (reason or ""), str(reason))

    MODE.update({"busy": False, "tick": True})
    reason = agent.server_busy(samples=2, pause=0.0)
    check("смена задачи между пробами замечена", reason is not None, str(reason))
    check("в причине названы номера задач", "→" in (reason or ""), str(reason))

    MODE.update({"tick": False, "slots": False})
    check("без /slots молчим, а не падаем", agent.server_busy() is None)
    MODE["slots"] = True

    cfg = make_cfg(tmp, tmp / "results")
    runner = TestRunner(agent, cfg)
    test_set = load_test_set(ROOT / "tests" / "chat_single")

    MODE["busy"] = True
    run = runner.run_set(test_set, runs=1, limit=1)
    check("на занятом сервере прогон всё равно идёт", len(run.cases) == 1, str(len(run.cases)))
    check(
        "занятость записана в параметры прогона",
        bool(run.params.get("server_busy")),
        str(run.params.get("server_busy")),
    )
    check(
        "в JSON прогона занятость тоже есть",
        bool(run.as_dict()["params"].get("server_busy")),
        str(run.as_dict()["params"].get("server_busy")),
    )

    MODE["busy"] = False
    run = runner.run_set(test_set, runs=1, limit=1)
    check(
        "на свободном сервере пометки нет",
        run.params.get("server_busy") is None,
        str(run.params.get("server_busy")),
    )


def part_server_info(tmp: Path) -> None:
    """Чем поднят сервер: прогон обязан записать это, даже когда не знает."""
    print("\n8. Чем поднят сервер: запись в параметрах прогона")
    agent = make_agent()
    cfg = make_cfg(tmp, tmp / "results")
    test_set = load_test_set(ROOT / "tests" / "chat_single")

    # Без источника ключ всё равно есть. Молчание читалось бы как «это не
    # важно», а важно: скорость решают флаги запуска, и два прогона одной
    # модели без записи о них неотличимы.
    run = TestRunner(agent, cfg).run_set(test_set, runs=1, limit=1)
    server = run.params.get("server") or {}
    check("без источника запись всё равно есть", isinstance(server, dict), str(server))
    check("и она говорит «неизвестно»", server.get("command") == "", str(server))
    check("причина названа", bool(server.get("note")), str(server))
    check(
        "в JSON прогона запись тоже есть",
        run.as_dict()["params"]["server"] == server,
        str(run.as_dict()["params"].get("server")),
    )

    line = "-ngl 99 --load-mode none -ot blk.0.ffn_down_exps.weight=CUDA_Host"
    run = TestRunner(
        agent, cfg, server_provider=lambda: describe_server(line, applied=True)
    ).run_set(test_set, runs=1, limit=1)
    server = run.params.get("server") or {}
    check("команда источника доехала до прогона", server.get("command") == line, str(server))
    check("и помечена применённой", server.get("applied") is True, str(server))
    check("кейс при этом отработал", len(run.cases) == 1, str(len(run.cases)))

    def broken() -> dict:
        raise RuntimeError("источник сломан")

    run = TestRunner(agent, cfg, server_provider=broken).run_set(test_set, runs=1, limit=1)
    check(
        "сломанный источник не роняет прогон",
        (run.params.get("server") or {}).get("command") == "",
        str(run.params.get("server")),
    )
    check("и кейс всё равно отработал", len(run.cases) == 1, str(len(run.cases)))


def main() -> int:
    config_path = ROOT / "config.json"
    before = _fingerprint(config_path)

    server = start_server()
    tmp_root = Path(tempfile.mkdtemp(prefix="check_speed_"))
    try:
        part_fetch()
        part_chain(tmp_root)
        part_metrics_text()
        part_aggregate(tmp_root)
        part_busy(tmp_root)
        part_server_info(tmp_root)
    finally:
        server.shutdown()
        server.server_close()

    print("\n9. Страж: рабочий config.json проекта")
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
