"""Проверка «Истории»: прогон целиком одной строкой с разворотом на кейсы.

Четыре части:

1. **Модель данных** — сборка файлов в партии (`results_index.RunGroup`),
   включая вывод партий по времени для файлов без `batch_id`;
2. **Исполнитель** — что `batch_id` действительно проставляется один на весь
   запуск, а не на набор (иначе группировать было бы нечего);
3. **Интерфейс** — дерево в таблице: строки, разворот, выделение, фильтры,
   пагинация, диспетчер двойного клика, отрисовка делегата;
4. **Предпросмотр и виджеты ячеек** — что разворот показывает первые
   `CASE_PREVIEW_LIMIT` кейсов и строку «ещё N», и что кнопки прошлой
   отрисовки не остаются висеть поверх колонки «Статус».

Окно поднимается в offscreen. Настоящий сервер не нужен: исполнитель получает
агента-заглушку, а фикстуры — это JSON-файлы, записанные во временную папку.
Рабочий `config.json` проекта не трогается: конфиг привязан к временному файлу,
а в конце сверяется отпечаток рабочего.

Запуск:  .venv/Scripts/python.exe check_history_tree.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from llmtestbench import results_index  # noqa: E402
from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.local_agent import Completion  # noqa: E402
from llmtestbench.results_index import group_records, load_runs  # noqa: E402
from llmtestbench.runner import RunResult, TestRunner, make_batch_id  # noqa: E402
from llmtestbench.testsets import discover_test_sets  # noqa: E402
from llmtestbench.ui import theme  # noqa: E402
from llmtestbench.ui.case_card_dialog import CaseCardDialog  # noqa: E402
from llmtestbench.ui.history_tab import CASE_PREVIEW_LIMIT, HistoryTab  # noqa: E402

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
    """Отпечаток файла: размер + время правки. Пусто — если файла нет."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


# ----------------------------------------------------------------------
# фикстуры

BASE = date.today()
MODEL = "test-model-Q4_0.gguf"
OTHER_MODEL = "other-model-Q8_0.gguf"


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(BASE, time(hh, mm, ss))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d_%H%M%S")


def case_dict(
    case_id: str, *, passed: bool = True, reason: str = "", total_ms: float = 1000.0
) -> dict:
    return {
        "case_id": case_id,
        "name": "Кейс %s" % case_id,
        "passed": passed,
        "reason": reason,
        "check_type": "contains_all",
        "total_ms": total_ms,
        "ttft_ms": 120.0,
        "tokens_per_sec": 50.0,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "reasoning_tokens": 0,
        "answer_tokens": 20,
        "prompt_ms": 30.0,
        "stop_reason": "stop",
        "max_tokens_answer": 256,
        "max_tokens_effective": 2304,
        "prompt": "Задание кейса %s" % case_id,
        "answer": "Ответ кейса %s" % case_id,
        "reasoning": "",
        "empty": False,
        "stand_error": "",
        "error": "",
        "judge_score": None,
        "judge_response": "",
        "judge_error": "",
    }


def write_run(
    directory: Path,
    run_id: str,
    *,
    set_id: str,
    set_name: str,
    started: datetime,
    finished: datetime,
    batch_id: str = "",
    model: str = MODEL,
    status: str = "finished",
    cases: list | None = None,
    skipped: int = 0,
    stand: int = 0,
) -> Path:
    """Записать файл прогона так, как его пишет `TestRunner.save`."""
    cases = list(cases or [])
    passed = sum(1 for c in cases if c["passed"] is True)
    failed = sum(1 for c in cases if c["passed"] is False)
    counted = passed + failed
    data = {
        "run_id": run_id,
        "batch_id": batch_id,
        "model": model,
        "base_url": "http://127.0.0.1:8080",
        "set_id": set_id,
        "set_name": set_name,
        "set_version": "2.0.0",
        "model_path": "",
        "context_size": 32768,
        "started": iso(started),
        "finished": iso(finished),
        "seconds": round((finished - started).total_seconds(), 2),
        "status": status,
        "params": {"runs": 1, "limit": 0, "tags": []},
        "summary": {
            "total": len(cases),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "stand_errors": stand,
            "empty_answers": 0,
            "counted": counted,
            "score": round(passed / counted, 4) if counted else None,
            "mean_judge_score": None,
            "by_tag": {},
        },
        "cases": cases,
        "path": "",
        "app_version": "0.1.0",
    }
    path = directory / (run_id + ".json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# Раскладка фикстур. Времена на сегодняшнюю дату, а не на фиксированную:
# фильтр периода по умолчанию — «последний месяц», и январские прогоны в
# интерфейсную часть просто не попали бы.
#
#   a  — партия из двух наборов, batch_id есть          (10:00)
#   b  — два файла без batch_id встык, разные наборы    (11:00)  → одна выведенная
#   c  — два файла без batch_id подряд, ОДИН набор      (12:00)  → две выведенных
#   d  — одинокий файл далеко по времени, другая модель (14:00)
#   e  — партия без проверок (counted = 0)              (09:00)
#   f  — партия остановлена                             (08:00)
#   g  — партия, где один набор упал                    (07:00)
FIXTURES = [
    (
        "m_alpha_%s" % stamp(at(10, 0)),
        dict(
            set_id="alpha",
            set_name="Альфа",
            started=at(10, 0),
            finished=at(10, 0, 5),
            batch_id="%s_%s" % (MODEL, stamp(at(10, 0))),
            cases=[case_dict("alpha_1"), case_dict("alpha_2"), case_dict("alpha_3")],
        ),
    ),
    (
        "m_beta_%s" % stamp(at(10, 0, 6)),
        dict(
            set_id="beta",
            set_name="Бета",
            started=at(10, 0, 6),
            finished=at(10, 0, 10),
            batch_id="%s_%s" % (MODEL, stamp(at(10, 0))),
            cases=[
                case_dict("beta_1"),
                case_dict("beta_2", passed=False, reason="нет ключевого слова «итог»"),
            ],
        ),
    ),
    (
        "m_gamma_%s" % stamp(at(11, 0)),
        dict(
            set_id="gamma",
            set_name="Гамма",
            started=at(11, 0),
            finished=at(11, 0, 10),
            cases=[case_dict("gamma_1")],
        ),
    ),
    (
        "m_delta_%s" % stamp(at(11, 0, 20)),
        dict(
            set_id="delta",
            set_name="Дельта",
            started=at(11, 0, 20),
            finished=at(11, 0, 30),
            cases=[case_dict("delta_1")],
        ),
    ),
    (
        "m_gamma_%s" % stamp(at(12, 0)),
        dict(
            set_id="gamma",
            set_name="Гамма",
            started=at(12, 0),
            finished=at(12, 0, 10),
            cases=[case_dict("gamma_2")],
        ),
    ),
    (
        "m_gamma_%s" % stamp(at(12, 0, 20)),
        dict(
            set_id="gamma",
            set_name="Гамма",
            started=at(12, 0, 20),
            finished=at(12, 0, 30),
            cases=[case_dict("gamma_3")],
        ),
    ),
    (
        "m_epsilon_%s" % stamp(at(14, 0)),
        dict(
            set_id="epsilon",
            set_name="Эпсилон",
            started=at(14, 0),
            finished=at(14, 0, 7),
            model=OTHER_MODEL,
            cases=[case_dict("epsilon_1")],
        ),
    ),
    (
        "m_speed_%s" % stamp(at(9, 0)),
        dict(
            set_id="speed",
            set_name="Скорость",
            started=at(9, 0),
            finished=at(9, 0, 3),
            batch_id="%s_%s" % (MODEL, stamp(at(9, 0))),
            cases=[
                dict(case_dict("speed_1"), passed=None, check_type="none"),
                dict(case_dict("speed_2"), passed=None, check_type="none"),
            ],
        ),
    ),
    (
        "m_zeta_%s" % stamp(at(8, 0)),
        dict(
            set_id="zeta",
            set_name="Дзета",
            started=at(8, 0),
            finished=at(8, 0, 4),
            batch_id="%s_%s" % (MODEL, stamp(at(8, 0))),
            status="cancelled",
            cases=[case_dict("zeta_1")],
        ),
    ),
    (
        "m_eta_%s" % stamp(at(7, 0)),
        dict(
            set_id="eta",
            set_name="Эта",
            started=at(7, 0),
            finished=at(7, 0, 5),
            batch_id="%s_%s" % (MODEL, stamp(at(7, 0))),
            cases=[case_dict("eta_1")],
        ),
    ),
    (
        "m_theta_%s" % stamp(at(7, 0, 5)),
        dict(
            set_id="theta",
            set_name="Тета",
            started=at(7, 0, 5),
            finished=at(7, 0, 9),
            batch_id="%s_%s" % (MODEL, stamp(at(7, 0))),
            status="failed",
            cases=[case_dict("theta_1", passed=False, reason="сбой стенда: timeout")],
        ),
    ),
]

GROUP_COUNT = 8  # a, b, c1, c2, d, e, f, g
FILE_COUNT = len(FIXTURES)  # 11


def build_fixtures(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for run_id, kwargs in FIXTURES:
        write_run(directory, run_id, **kwargs)


def make_cfg(tmp: Path) -> AppConfig:
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        tests_dir=str(Path(__file__).resolve().parent / "tests"),
        results_dir=str(tmp / "results"),
        reports_dir=str(tmp / "reports"),
        logs_dir=str(tmp / "logs"),
        sessions_dir=str(tmp / "sessions"),
        # База истории рядом с временной папкой, а не рядом с приложением:
        # иначе исполнитель из части 2 допишет в рабочую history.db.
        history_db_name=str(tmp / "history.db"),
    )
    cfg.bind(tmp / "config.json")
    cfg.resolve_dirs()
    cfg.ensure_dirs()
    return cfg


# ----------------------------------------------------------------------
# 1. модель данных


def test_grouping(tmp: Path) -> None:
    print("\n1. Сборка прогонов целиком (results_index)")
    results = tmp / "results"
    build_fixtures(results)
    records = load_runs(make_cfg(tmp))
    check(
        "файлов прочитано", len(records) == FILE_COUNT, "%d вместо %d" % (len(records), FILE_COUNT)
    )

    groups = group_records(records)
    check(
        "групп получилось",
        len(groups) == GROUP_COUNT,
        "%d вместо %d: %s" % (len(groups), GROUP_COUNT, [g.key for g in groups]),
    )
    by_key = {g.key: g for g in groups}

    # --- партия с batch_id ------------------------------------------
    batch_a = "%s_%s" % (MODEL, stamp(at(10, 0)))
    check("партия с batch_id найдена по ключу", batch_a in by_key)
    a = by_key.get(batch_a)
    if a is None:
        return
    check("партия с batch_id не выведена", a.inferred is False)
    check("в партии два файла", len(a.runs) == 2, str(len(a.runs)))
    check("наборы собраны по порядку старта", a.set_ids == ["alpha", "beta"], str(a.set_ids))
    check("метка — один набор из двух", a.set_label == "2 набора", a.set_label)
    check("счёт сложен: 4 из 5", (a.passed, a.counted) == (4, 5), "%s/%s" % (a.passed, a.counted))
    check("провал посчитан", a.failed == 1, str(a.failed))
    check("доля посчитана", a.score == 0.8, str(a.score))
    check("время сложено", a.seconds == 9.0, str(a.seconds))
    check("кейсов столько, сколько в файлах", a.cases_count == 5, str(a.cases_count))
    check(
        "список кейсов с указанием файла",
        len(a.cases) == 5 and all(isinstance(r, results_index.RunRecord) for r, _ in a.cases),
    )
    check("short_id — только метка времени", a.short_id == stamp(at(10, 0)), a.short_id)
    check("дата — день старта", a.date_text == at(10, 0).strftime("%d.%m %H:%M"), a.date_text)
    check("статус — готово", a.status_ru == "готово", a.status_ru)
    check(
        "в подсказке статуса есть оба набора", "alpha" in a.status_hint and "beta" in a.status_hint
    )

    # --- выведенная партия из двух наборов --------------------------
    b = next((g for g in groups if "gamma" in g.set_ids and len(g.set_ids) == 2), None)
    check("выведенная партия из двух файлов найдена", b is not None)
    if b is not None:
        check("она помечена как выведенная", b.inferred is True)
        check("наборы разные", b.set_ids == ["gamma", "delta"], str(b.set_ids))
        check("метка со знаком «≈»", b.short_id.startswith("≈ "), b.short_id)
        check("batch_id пуст", b.batch_id == "", repr(b.batch_id))
        check("ключ начинается с legacy:", b.key.startswith("legacy:"), b.key)

    # --- два прогона одного набора подряд → две группы ---------------
    same_set = [g for g in groups if g.set_ids == ["gamma"]]
    check("два прогона одного набора не слиплись", len(same_set) == 2, str(len(same_set)))
    check("оба помечены как выведенные", all(g.inferred for g in same_set))

    # --- одинокий файл далеко по времени ----------------------------
    d = next((g for g in groups if g.set_ids == ["epsilon"]), None)
    check("одинокий файл — отдельная группа", d is not None)
    if d is not None:
        check("модель взята из файла", d.model == "other-model-q8_0", d.model)

    # --- статусы ----------------------------------------------------
    e = next((g for g in groups if g.set_ids == ["speed"]), None)
    check("партия без проверок не считается оценённой", e is not None and e.scored is False)
    check(
        "статус «без проверок»",
        e is not None and e.status_ru == "без проверок",
        e.status_ru if e else "—",
    )
    f = next((g for g in groups if g.set_ids == ["zeta"]), None)
    check(
        "статус «остановлен»",
        f is not None and f.status_ru == "остановлен",
        f.status_ru if f else "—",
    )
    g = next((g for g in groups if "theta" in g.set_ids), None)
    check(
        "сбой старше «готово»", g is not None and g.status_ru == "сбой", g.status_ru if g else "—"
    )

    # --- порядок ----------------------------------------------------
    started = [x.started for x in groups]
    check(
        "группы отсортированы свежие первыми",
        started == sorted(started, reverse=True),
        str(started),
    )

    # --- прежние функции не сломаны ---------------------------------
    latest = results_index.latest_by_set(records)
    check(
        "latest_by_set по-прежнему возвращает свежий прогон набора",
        latest.get("gamma") is not None and latest["gamma"].started == iso(at(12, 0, 20)),
        latest.get("gamma").started if latest.get("gamma") else "—",
    )
    check(
        "available_models видит обе модели",
        sorted(results_index.available_models(records)) == ["other-model-q8_0", "test-model-q4_0"],
        str(results_index.available_models(records)),
    )
    check(
        "available_sets видит все наборы",
        len(results_index.available_sets(records)) == 9,
        str(results_index.available_sets(records)),
    )


# ----------------------------------------------------------------------
# 2. исполнитель: batch_id один на запуск


class StubAgent:
    """Агент без сети: отвечает одним и тем же текстом на любой запрос."""

    base_url = "http://127.0.0.1:8080"
    context_size = 32768

    def __init__(self, model: str = "stub-model-Q4_0.gguf"):
        self.model_name = model

    def count_tokens(self, text):
        return max(1, len(str(text)) // 4)

    def props(self):
        return {"model_path": "F:/Models/" + self.model_name}

    def health(self, timeout=5.0):
        return {}

    def complete(self, messages, **kw):
        return Completion(
            text="заглушка: ответ есть",
            stop_reason="stop",
            ttft_ms=12.0,
            prompt_tokens=100,
            completion_tokens=20,
            prompt_ms=5.0,
            prompt_per_second=20000.0,
            predicted_per_second=100.0,
        )


def test_runner(tmp: Path) -> None:
    print("\n2. batch_id: один на запуск, а не на набор (runner)")
    cfg = make_cfg(tmp)
    sets = discover_test_sets(cfg.tests_path)
    two = [s for s in sets if s.id in ("chat_single", "chat_multi")]
    check("нашлись два набора для прогона", len(two) == 2, ", ".join(s.id for s in two))
    if len(two) != 2:
        return

    runner = TestRunner(StubAgent(), cfg)
    check("до прогона batch_id ещё не посчитан", runner._batch is None)
    first = runner.batch_id()
    check("batch_id устойчив между вызовами", runner.batch_id() == first, first)

    paths = []
    for test_set in two:
        result = runner.run_set(test_set, runs=1, limit=1)
        paths.append(runner.save(result))

    check("файлы прогонов записаны", all(p.is_file() for p in paths), str([p.name for p in paths]))
    data = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    check(
        "batch_id записан в оба файла",
        all(d.get("batch_id") == first for d in data),
        str([d.get("batch_id") for d in data]),
    )
    check("run_id остались разными", data[0]["run_id"] != data[1]["run_id"])
    # Новые поля `RunResult` дописываются в конец, старые не двигаются: вставка
    # в середину сдвигает позиции и ломает тех, кто собирает результат
    # позиционно. Раньше здесь стояло «batch_id — последнее поле», но это был
    # слепок момента, а не правило: сам `batch_id` когда-то дописали в конец
    # таким же образом, и блок `server` (память модели) встал за ним. Поэтому
    # проверяем не «кто последний», а что порядок уже бывших полей не изменился.
    frozen = [
        "run_id",
        "model",
        "base_url",
        "set_id",
        "set_name",
        "set_version",
        "model_path",
        "context_size",
        "started",
        "finished",
        "seconds",
        "status",
        "params",
        "summary",
        "cases",
        "path",
        "batch_id",
    ]
    fields = list(RunResult.__dataclass_fields__)
    check(
        "порядок полей RunResult не менялся — новые дописаны в конец",
        fields[: len(frozen)] == frozen,
        "получено: %s" % fields[: len(frozen)],
    )
    check(
        "формат batch_id: модель + метка времени",
        make_batch_id("a/b/C-Q4_0.gguf").startswith("C-Q4_0_")
        and len(make_batch_id("m.gguf").split("_")[-2]) == 8,
        make_batch_id("m.gguf"),
    )
    check(
        "явный batch_id перебивает вычисленный",
        runner.run_set(two[0], runs=1, limit=1, batch_id="hand-made").batch_id == "hand-made",
    )

    # Файлы, записанные этим прогоном, должны собраться в одну партию.
    fresh = group_records(load_runs(cfg))
    check(
        "два набора одного запуска собрались в один прогон",
        any(len(x.runs) == 2 and x.batch_id == first for x in fresh),
        str([(x.batch_id, len(x.runs)) for x in fresh]),
    )


# ----------------------------------------------------------------------
# 3. интерфейс


def _row_of_group(tab: HistoryTab, key: str) -> int:
    for row in range(tab.table.rowCount()):
        meta = tab._row_meta(row)
        if meta and meta[0] == "group" and meta[1].key == key:
            return row
    return -1


def test_ui(tmp: Path) -> None:
    print("\n3. Дерево в таблице (history_tab)")
    build_fixtures(tmp / "results")
    cfg = make_cfg(tmp)
    tab = HistoryTab(cfg)

    check(
        "строк при свёрнутом виде = числу прогонов",
        tab.table.rowCount() == GROUP_COUNT,
        "%d вместо %d" % (tab.table.rowCount(), GROUP_COUNT),
    )
    check(
        "total_groups() считает прогоны",
        tab.total_groups() == GROUP_COUNT,
        str(tab.total_groups()),
    )
    check("total_runs() считает файлы", tab.total_runs() == FILE_COUNT, str(tab.total_runs()))
    check(
        "пагинация считает прогоны, а не файлы",
        tab._page_count() == 1,
        "страниц %d при %d файлах" % (tab._page_count(), FILE_COUNT),
    )
    check(
        "в подвале «из 8 прогонов»",
        "из %d прогонов" % GROUP_COUNT in tab.page_label.text(),
        tab.page_label.text(),
    )
    check(
        "все строки — прогоны",
        all((tab._row_meta(r) or ("",))[0] == "group" for r in range(tab.table.rowCount())),
    )
    # Ширины колонок: идентификатор прогона обрезать нельзя. Пока «Счёт» и
    # остальные тянулись «по содержимому», растягиваемая колонка «Прогон»
    # сжималась до 17 px, и вместо «≈ 20260924_062605» показывалось «≈ 202609…».
    check(
        "колонка «Прогон» не сжата",
        tab.table.columnWidth(1) >= 150,
        "%d px" % tab.table.columnWidth(1),
    )
    check(
        "«Счёт» остаётся фиксированной",
        tab.table.columnWidth(4) == 118,
        "%d px" % tab.table.columnWidth(4),
    )

    # --- разворот ----------------------------------------------------
    batch_a = "%s_%s" % (MODEL, stamp(at(10, 0)))
    row = _row_of_group(tab, batch_a)
    check("строка партии найдена", row >= 0, batch_a)
    if row < 0:
        return
    tab._on_cell_clicked(row, 1)
    check(
        "разворот добавил строки кейсов",
        tab.table.rowCount() == GROUP_COUNT + 5,
        str(tab.table.rowCount()),
    )
    check("первая строка под прогоном — кейс", (tab._row_meta(row + 1) or ("",))[0] == "case")
    check(
        "кейсов ровно столько, сколько в JSON",
        sum(1 for r in range(tab.table.rowCount()) if (tab._row_meta(r) or ("",))[0] == "case")
        == 5,
    )
    case_item = tab.table.item(row + 1, 1)
    check(
        "id кейса в столбце «Прогон»",
        case_item is not None and "alpha_1" in case_item.text(),
        case_item.text() if case_item else "—",
    )
    check(
        "строка кейса ниже строки прогона",
        tab.table.rowHeight(row + 1) < tab.table.rowHeight(row),
        "%d / %d" % (tab.table.rowHeight(row + 1), tab.table.rowHeight(row)),
    )
    check("название кейса в соседнем столбце", "Кейс alpha_1" in tab.table.item(row + 1, 2).text())
    check(
        "набор показан, когда их в партии несколько",
        tab.table.item(row + 1, 3).text() == "alpha",
        tab.table.item(row + 1, 3).text() or "пусто",
    )
    verdict = tab.table.item(row + 1, 4).data(Qt.UserRole)
    check(
        "вердикт кейса — словарь для делегата",
        isinstance(verdict, dict) and verdict.get("label") == "зачтён",
        str(verdict),
    )
    check(
        "время кейса показано",
        tab.table.item(row + 1, 5).text().endswith("с"),
        tab.table.item(row + 1, 5).text(),
    )
    check("кнопка карточки кейса есть", tab.table.cellWidget(row + 1, 8) is not None)
    check(
        "чекбокса у строки кейса нет",
        not (tab.table.item(row + 1, 0).flags() & Qt.ItemIsUserCheckable),
    )

    # строка провала: причина обрезана, полный текст в подсказке
    fail_row = next(
        (
            r
            for r in range(tab.table.rowCount())
            if (tab._row_meta(r) or ("",))[0] == "case"
            and tab.table.item(r, 4).data(Qt.UserRole).get("verdict") == "fail"
        ),
        -1,
    )
    check("строка провала найдена", fail_row > 0)
    if fail_row > 0:
        cell = tab.table.item(fail_row, 7)
        check("причина провала в столбце «Статус»", "ключевого слова" in cell.text(), cell.text())
        check(
            "полный текст причины — в подсказке",
            "нет ключевого слова «итог»" in cell.toolTip(),
            cell.toolTip(),
        )

    # --- свёртывание --------------------------------------------------
    tab._on_cell_clicked(row, 3)
    check(
        "клик мимо треугольника не сворачивает партию",
        tab.table.rowCount() == GROUP_COUNT + 5,
        str(tab.table.rowCount()),
    )
    tab._on_cell_clicked(row, 1)
    check(
        "повторный клик по треугольнику свернул партию",
        tab.table.rowCount() == GROUP_COUNT,
        str(tab.table.rowCount()),
    )
    tab._on_cell_clicked(row, 3)
    check(
        "клик по столбцу 3 не разворачивает",
        tab.table.rowCount() == GROUP_COUNT,
        str(tab.table.rowCount()),
    )

    # --- выделение ----------------------------------------------------
    tab._on_cell_clicked(row, 1)
    tab.table.item(row, 0).setCheckState(Qt.Checked)
    check(
        "чекбокс прогона выделил все его файлы",
        tab._checked
        == {r.run_id for r in next(x for x in tab._filtered if x.key == batch_a).runs},
        str(sorted(tab._checked)),
    )
    check(
        "_selected_paths вернул все файлы прогона",
        len(tab._selected_paths()) == 2,
        str(tab._selected_paths()),
    )
    check("пути существуют", all(Path(p).is_file() for p in tab._selected_paths()))
    # `isVisible()` здесь бесполезен: вкладка не показана, и он вернёт False
    # даже при явном setVisible(True). Спрашиваем про явную скрытость.
    check("полоса массовых действий показана", not tab.bulk_bar.isHidden())
    check(
        "в полосе — прогоны и наборы",
        "прогон" in tab.bulk_label.text() and "набор" in tab.bulk_label.text(),
        tab.bulk_label.text(),
    )
    # Сравниваются прогоны, а не файлы: два файла одной партии — это один
    # прогон, и кнопка обязана быть выключена. По числу файлов она включалась
    # бы и предлагала сравнить прогон сам с собой.
    check("«Сравнить» выключена при одном прогоне из двух файлов", not tab.compare_btn.isEnabled())

    tab._checked = {sorted(tab._checked)[0]}
    state = tab._group_check_state(next(x for x in tab._filtered if x.key == batch_a))
    check("часть файлов выделена → PartiallyChecked", state == Qt.PartiallyChecked, str(state))

    tab._check_all(True)
    check(
        "«Выделить все» выделило все файлы",
        len(tab._checked) == FILE_COUNT,
        str(len(tab._checked)),
    )
    check("при выделении всех прогонов «Сравнить» доступна", tab.compare_btn.isEnabled())
    tab._check_all(False)
    check("«Снять выделение» очистило выбор", not tab._checked)

    # --- диспетчер двойного клика ------------------------------------
    opened: list[tuple] = []
    tab._open_group = lambda g: opened.append(("group", g))
    tab._open_case = lambda r, c: opened.append(("case", r, c))
    tab._on_double_click(row, 2)
    check(
        "двойной клик по прогону открывает отчёт",
        opened and opened[-1][0] == "group",
        str(opened[-1][:1]),
    )
    case_row = row + 1
    tab._on_double_click(case_row, 2)
    check(
        "двойной клик по кейсу открывает карточку",
        opened and opened[-1][0] == "case",
        str(opened[-1][:1]),
    )
    before = len(opened)
    tab._on_double_click(row, 1)
    check(
        "двойной клик по треугольнику ничего не открывает",
        len(opened) == before,
        str(len(opened) - before),
    )

    # --- делегат и карточка ------------------------------------------
    # Размер задаём явно: невидимое окно иначе отрисуется в заглушку, и по
    # снимку нельзя будет судить, что делегат что-то нарисовал.
    tab.resize(1360, 860)
    pixmap = tab.table.grab()
    check(
        "таблица отрисовалась (делегат обеих строк не упал)",
        pixmap is not None and not pixmap.isNull() and pixmap.width() > 100,
        "снимок %dx%d" % (pixmap.width(), pixmap.height()) if pixmap else "—",
    )

    meta = tab._row_meta(case_row)
    dialog = CaseCardDialog(meta[1], meta[2], tab)
    check(
        "карточка кейса собирается из строки дерева",
        dialog.windowTitle().startswith("Кейс "),
        dialog.windowTitle(),
    )
    dialog.deleteLater()

    # --- фильтры ------------------------------------------------------
    tab._reset_filters()
    check(
        "без фильтров видны все прогоны",
        len(tab._filtered) == GROUP_COUNT,
        str(len(tab._filtered)),
    )

    tab.type_combo.setCurrentIndex(tab.type_combo.findData("beta"))
    check(
        "фильтр по набору находит партию по любому её файлу",
        [g.key for g in tab._filtered] == [batch_a],
        str([g.key for g in tab._filtered]),
    )

    tab.type_combo.setCurrentIndex(tab.type_combo.findData("gamma"))
    check(
        "фильтр по набору находит все партии с этим набором",
        len(tab._filtered) == 3,
        str(len(tab._filtered)),
    )
    tab._reset_filters()

    tab.model_combo.setCurrentIndex(tab.model_combo.findData("other-model-q8_0"))
    check("фильтр по модели", len(tab._filtered) == 1, str(len(tab._filtered)))
    tab._reset_filters()

    for status, expected in (("failed", 1), ("cancelled", 1), ("no_score", 1), ("finished", 6)):
        tab.status_combo.setCurrentIndex(tab.status_combo.findData(status))
        check(
            "фильтр по статусу «%s» даёт %d" % (status, expected),
            len(tab._filtered) == expected,
            str(len(tab._filtered)),
        )
    tab._reset_filters()

    tab.search_edit.setText("gamma")
    check("поиск по набору", len(tab._filtered) == 3, str(len(tab._filtered)))
    tab.search_edit.setText(stamp(at(10, 0)))
    check("поиск по batch_id", len(tab._filtered) == 1, str(len(tab._filtered)))
    tab.search_edit.setText("ничего-такого")
    check("поиск без совпадений даёт пусто", not tab._filtered)
    check(
        "показана заглушка «под фильтры ничего не попало»", tab.stack.currentWidget() is tab.empty
    )
    tab._reset_filters()

    # --- сортировка ---------------------------------------------------
    tab._on_header_clicked(2)
    models = [g.model for g in tab._filtered]
    check("сортировка по модели по убыванию", models == sorted(models, reverse=True), str(models))
    tab._on_header_clicked(2)
    models = [g.model for g in tab._filtered]
    check("повторный клик меняет направление", models == sorted(models), str(models))

    # --- отчёт по партии ----------------------------------------------
    group_a = next(g for g in tab._groups if g.key == batch_a)
    html = tab._report_html_path(group_a.paths, group_a)
    check("отчёт по партии собран", html is not None and html.is_file(), str(html))
    if html is not None and html.is_file():
        text = html.read_text(encoding="utf-8")
        check("имя файла — batch_id", html.stem == batch_a, html.stem)
        check(
            "в отчёте обе секции наборов",
            text.count("<section>") == 2,
            str(text.count("<section>")),
        )
        check(
            "в заголовке — сколько наборов и кейсов",
            "2 набора, 5 кейсов" in text,
            [ln for ln in text.splitlines() if "Тестирование" in ln][:1],
        )

    # --- регрессия: прежние проверки истории --------------------------
    check("пустой список прогонов не ломает разбор", group_records([]) == [])
    check(
        "файл без batch_id и без started не роняет сборку",
        len(group_records([results_index.RunRecord(run_id="x")])) == 1,
    )


# ----------------------------------------------------------------------
# 4. предпросмотр кейсов и виджеты ячеек


def _kinds(tab: HistoryTab) -> list[str]:
    """Вид каждой строки таблицы: `group`, `case` или `more`."""
    return [(tab._row_meta(r) or ("",))[0] for r in range(tab.table.rowCount())]


def _pump() -> None:
    """Пропустить события Qt.

    Нужно перед замером координат: виджеты ячеек раскладываются по местам не
    в момент `setCellWidget`, а при следующей раскладке таблицы, и до неё все
    они стоят в нуле.
    """
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def test_preview_and_widgets(tmp: Path) -> None:
    print("\n4. Предпросмотр кейсов и виджеты ячеек (history_tab)")
    results = tmp / "results"
    results.mkdir(parents=True, exist_ok=True)

    wide_key = "%s_%s" % (MODEL, stamp(at(16, 0)))
    narrow_key = "%s_%s" % (MODEL, stamp(at(17, 0)))
    write_run(
        results,
        "m_wide_%s" % stamp(at(16, 0)),
        set_id="wide",
        set_name="Широкий",
        started=at(16, 0),
        finished=at(16, 5),
        batch_id=wide_key,
        cases=[case_dict("wide_%02d" % n) for n in range(1, 21)],
    )
    write_run(
        results,
        "m_narrow_%s" % stamp(at(17, 0)),
        set_id="narrow",
        set_name="Узкий",
        started=at(17, 0),
        finished=at(17, 1),
        batch_id=narrow_key,
        cases=[case_dict("narrow_%d" % n) for n in range(1, 4)],
    )

    tab = HistoryTab(make_cfg(tmp))
    # Размер задаём явно и показываем вкладку: у невидимого окна геометрия —
    # заглушка, и координаты кнопок в ячейках получились бы не такими, как в
    # настоящем окне.
    tab.resize(1360, 860)
    tab.show()
    _pump()
    check(
        "две фикстуры собрались в два прогона",
        tab.table.rowCount() == 2,
        str(tab.table.rowCount()),
    )

    # --- короткий прогон: остатка быть не должно ----------------------
    tab._toggle_group(narrow_key)
    kinds = _kinds(tab)
    check("короткий прогон развернулся на все 3 кейса", kinds.count("case") == 3, str(kinds))
    check("в коротком прогоне строки «ещё N» нет", "more" not in kinds, str(kinds))
    tab._toggle_group(narrow_key)
    check("свёртывание вернуло две строки", tab.table.rowCount() == 2, str(tab.table.rowCount()))

    # --- длинный прогон: 15 кейсов и строка «ещё N» -------------------
    tab._toggle_group(wide_key)
    kinds = _kinds(tab)
    check(
        "длинный прогон: %d кейсов и одна строка «ещё N»" % CASE_PREVIEW_LIMIT,
        kinds.count("case") == CASE_PREVIEW_LIMIT and kinds.count("more") == 1,
        str(kinds),
    )
    check(
        "строк стало 2 + %d + 1" % CASE_PREVIEW_LIMIT,
        tab.table.rowCount() == CASE_PREVIEW_LIMIT + 3,
        str(tab.table.rowCount()),
    )

    wide_row = _row_of_group(tab, wide_key)
    check("строка длинного прогона найдена", wide_row >= 0)
    more_row = kinds.index("more") if "more" in kinds else -1
    check("строка «ещё N» найдена", more_row > 0)
    if wide_row >= 0 and more_row > 0:
        check(
            "строка «ещё N» идёт сразу за последним кейсом",
            more_row == wide_row + CASE_PREVIEW_LIMIT + 1,
            "%d при прогоне в строке %d" % (more_row, wide_row),
        )
        check(
            "в развороте показаны первые кейсы по порядку",
            tab.table.item(wide_row + 1, 1).text().strip() == "wide_01",
            tab.table.item(wide_row + 1, 1).text(),
        )

        item = tab.table.item(more_row, 1)
        check(
            "в строке «ещё N» — про остаток",
            item is not None and "ещё 5 кейсов" in item.text(),
            item.text() if item else "—",
        )
        check(
            "в подсказке — сколько кейсов всего",
            item is not None and "В прогоне 20 кейсов" in item.toolTip(),
            item.toolTip() if item else "—",
        )
        check(
            "у строки «ещё N» нет чекбокса",
            not (tab.table.item(more_row, 0).flags() & Qt.ItemIsUserCheckable),
        )
        check(
            "_row_meta строки «ещё N» — кортеж, а не None",
            (tab._row_meta(more_row) or ("",))[0] == "more",
        )
        # Подсказка объединена на три колонки: в одной «Прогон» (200 px) она
        # обрезалась на «двойн…» — ровно там, где сказано, что делать.
        check(
            "подсказка «ещё N» растянута на три колонки",
            tab.table.columnSpan(more_row, 1) == 3,
            str(tab.table.columnSpan(more_row, 1)),
        )

        opened: list = []
        tab._open_group = lambda g: opened.append(g)
        tab._on_double_click(more_row, 2)
        check(
            "двойной клик по «ещё N» открывает отчёт прогона", len(opened) == 1, str(len(opened))
        )

    # --- орфаны: кнопки прошлой отрисовки -----------------------------
    # `setRowCount()` уничтожает элементы таблицы, но **не** виджеты ячеек:
    # виджет с родителем остаётся жив и продолжает рисоваться там, где стоял.
    # После свёртывания и разворота кнопки прошлой отрисовки оставались поверх
    # колонки «Статус» — на снимке это выглядело как вторая колонка кнопок.
    for _ in range(3):
        tab._toggle_group(wide_key)
        tab._toggle_group(wide_key)
    _pump()
    kinds = _kinds(tab)
    buttons = tab.table.findChildren(QPushButton)
    expected = sum(1 for kind in kinds if kind in ("group", "case"))
    check(
        "кнопок в таблице ровно по числу строк с действием",
        len(buttons) == expected,
        "%d вместо %d" % (len(buttons), expected),
    )
    action_left = tab.table.columnViewportPosition(8)
    action_right = action_left + tab.table.columnWidth(8)
    # Кнопку Qt центрирует в ячейке, поэтому x кнопки не равен левому краю
    # столбца — проверяем не равенство, а попадание в столбец. Орфан со старой
    # отрисовки остаётся левее: его столбец стоял там, где был до того, как
    # ширины колонок задали явно.
    stray = [
        b for b in buttons if not (action_left <= b.x() and b.x() + b.width() <= action_right)
    ]
    check(
        "все кнопки внутри столбца действий, ни одна не осталась на старом месте",
        not stray,
        "столбец %d…%d, лишние: %s"
        % (action_left, action_right, [(b.x(), b.y()) for b in stray][:5]),
    )
    check(
        "координата столбца действий не нулевая (замер осмыслен)",
        action_left > 0,
        str(action_left),
    )

    # --- метка в рельсе ------------------------------------------------
    from llmtestbench.ui.main_window import MainWindow

    win = MainWindow(make_cfg(tmp))
    win._update_rail_badges()
    badge = win.rail._items["history"].badge.text()
    check("в рельсе — число прогонов, а не файлов", badge == "2", badge)
    check(
        "число в рельсе совпадает с числом строк",
        badge == str(win.history_tab.total_groups()),
        "%s / %d" % (badge, win.history_tab.total_groups()),
    )
    win.close()


def main() -> int:
    print("=" * 72)
    print("Проверка «Истории»: прогон целиком с разворотом на кейсы")
    print("=" * 72)
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    tmp = Path(tempfile.mkdtemp(prefix="llmtestbench_history_"))
    project_config = Path(__file__).resolve().parent / "config.json"
    config_before = _fingerprint(project_config)
    try:
        # У каждой части своя папка: исполнитель из части 2 записал бы свои
        # файлы в ту же results/, и интерфейсная часть увидела бы лишние
        # прогоны — числа в проверках поехали бы.
        test_grouping(tmp / "grouping")
        test_runner(tmp / "runner")
        test_ui(tmp / "ui")
        test_preview_and_widgets(tmp / "preview")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print("\n5. Страж: рабочий config.json проекта")
    check(
        "config.json проекта не изменён проверкой",
        _fingerprint(project_config) == config_before,
        "файл %s изменился" % project_config,
    )

    print("\n" + "=" * 72)
    print("Пройдено: %d   Провалено: %d" % (ok_count, fail_count))
    print("=" * 72)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
