"""Проверка «Сравнения»: столбец — прогон целиком, строка — метрика.

Пять частей:

1. **Модель данных** — `RunGroup.stats` и `RunGroup.set_stats`: счёт совпадает
   с `summary` из файла, разбивка по наборам складывается в прогон, дубликат
   набора в партии сливается, оборванный файл без `cases[]` не показывает нули;
2. **Столбцы — прогоны** — плоский список файлов собирается в прогоны: партия
   из трёх наборов даёт **один** столбец, а не три; два прогона одной модели
   различаются датой в шапке; порядок колонок хронологический;
3. **Детализация** — разворот строки по наборам, прочерк для набора, которого
   в прогоне не было, «Только различия» прячет совпавшие строки, а клик по
   подписи подстроки не сворачивает родителя;
4. **Предупреждение о версиях** (п. 11.7 ТЗ), выгрузки CSV и HTML;
5. **Диалог набора** — кейсы × прогоны, вердикты, прогоны без набора отсеяны;
6. **История** — «Сравнить выбранные» включается по числу **прогонов**, а не
   файлов.

Окно поднимается в offscreen. Сервер не нужен: фикстуры — это JSON-файлы,
записанные во временную папку, а `config.json` проекта не трогается (конфиг
привязан к временному файлу, в конце сверяется отпечаток рабочего).

Запуск:  .venv/Scripts/python.exe check_compare.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.results_index import group_records, load_runs  # noqa: E402
from llmtestbench.ui import theme  # noqa: E402
from llmtestbench.ui.compare_tab import ROWS, EXPANDABLE_ROWS, CompareTab  # noqa: E402
from llmtestbench.ui.history_tab import HistoryTab  # noqa: E402
from llmtestbench.ui.set_compare_dialog import SetCompareDialog  # noqa: E402

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
MODEL_A = "alpha-model-Q4_K_M.gguf"
MODEL_B = "beta-model-Q8_0.gguf"


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(BASE, time(hh, mm, ss))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d_%H%M%S")


def case_dict(
    case_id: str,
    *,
    passed: bool = True,
    total_ms: float = 1000.0,
    tps: float = 50.0,
    empty: bool = False,
    stand_error: str = "",
) -> dict:
    return {
        "case_id": case_id,
        "name": "Кейс %s" % case_id,
        "passed": passed,
        "counted": passed is not None,
        "reason": "" if passed else "нет ключевого слова",
        "check_type": "none" if passed is None else "contains_all",
        "total_ms": total_ms,
        "ttft_ms": 120.0,
        "tokens_per_sec": tps,
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
        "empty": empty,
        "stand_error": stand_error,
        "error": stand_error,
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
    batch_id: str,
    model: str = MODEL_A,
    set_version: str = "2.0.0",
    status: str = "finished",
    cases: list | None = None,
    omit_cases: bool = False,
) -> Path:
    """Записать файл прогона так, как его пишет `TestRunner.save`."""
    cases = list(cases or [])
    passed = sum(1 for c in cases if c["passed"] is True)
    failed = sum(1 for c in cases if c["passed"] is False)
    skipped = sum(1 for c in cases if c["passed"] is None)
    counted = passed + failed
    data = {
        "run_id": run_id,
        "batch_id": batch_id,
        "model": model,
        "base_url": "http://127.0.0.1:8080",
        "set_id": set_id,
        "set_name": set_name,
        "set_version": set_version,
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
            "stand_errors": 0,
            "empty_answers": 0,
            "counted": counted,
            "score": round(passed / counted, 4) if counted else None,
            "mean_judge_score": None,
            "by_tag": {},
        },
        "cases": [] if omit_cases else cases,
        "path": "",
        "app_version": "0.1.0",
    }
    path = directory / (run_id + ".json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# Раскладка. Времена — на сегодняшнюю дату: фильтр периода по умолчанию
# «последний месяц», и январские прогоны в интерфейсную часть не попали бы.
#
#   Прогон A (model A, 10:00) — три набора: alpha, beta, speed.
#   Прогон B (model B, 11:00) — три набора: alpha, beta, gamma.
#                               beta версии 2.1.0 → расхождение по п. 11.7.
#   Прогон C (model A, 12:00) — один набор: alpha. Повтор модели → в шапке
#                               сравнения должна появиться дата.
#   Прогон D (model B, 13:00) — два файла ОДНОГО набора delta: проверка
#                               слияния дубликата набора внутри партии.
BATCH_A = "alpha-model-Q4_K_M_%s" % stamp(at(10, 0))
BATCH_B = "beta-model-Q8_0_%s" % stamp(at(11, 0))
BATCH_C = "alpha-model-Q4_K_M_%s" % stamp(at(12, 0))
BATCH_D = "beta-model-Q8_0_%s" % stamp(at(13, 0))


def build_fixtures(directory: Path) -> dict[str, list[str]]:
    """Записать фикстуры и вернуть пути файлов по прогонам."""
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[str]] = {}

    out["A"] = [
        str(
            write_run(
                directory,
                "a_alpha_%s" % stamp(at(10, 0)),
                set_id="alpha",
                set_name="Альфа",
                started=at(10, 0),
                finished=at(10, 0, 30),
                batch_id=BATCH_A,
                cases=[case_dict("alpha_1"), case_dict("alpha_2", passed=False)],
            )
        ),
        str(
            write_run(
                directory,
                "a_beta_%s" % stamp(at(10, 0, 31)),
                set_id="beta",
                set_name="Бета",
                started=at(10, 0, 31),
                finished=at(10, 0, 45),
                batch_id=BATCH_A,
                cases=[case_dict("beta_1")],
            )
        ),
        str(
            write_run(
                directory,
                "a_speed_%s" % stamp(at(10, 0, 46)),
                set_id="speed",
                set_name="Скорость",
                started=at(10, 0, 46),
                finished=at(10, 0, 55),
                batch_id=BATCH_A,
                cases=[case_dict("speed_1", passed=None, tps=90.0)],
            )
        ),
    ]
    out["B"] = [
        str(
            write_run(
                directory,
                "b_alpha_%s" % stamp(at(11, 0)),
                set_id="alpha",
                set_name="Альфа",
                started=at(11, 0),
                finished=at(11, 0, 30),
                batch_id=BATCH_B,
                model=MODEL_B,
                cases=[case_dict("alpha_1"), case_dict("alpha_2")],
            )
        ),
        str(
            write_run(
                directory,
                "b_beta_%s" % stamp(at(11, 0, 31)),
                set_id="beta",
                set_name="Бета",
                set_version="2.1.0",
                started=at(11, 0, 31),
                finished=at(11, 0, 45),
                batch_id=BATCH_B,
                model=MODEL_B,
                cases=[case_dict("beta_1")],
            )
        ),
        str(
            write_run(
                directory,
                "b_gamma_%s" % stamp(at(11, 0, 46)),
                set_id="gamma",
                set_name="Гамма",
                started=at(11, 0, 46),
                finished=at(11, 0, 55),
                batch_id=BATCH_B,
                model=MODEL_B,
                cases=[case_dict("gamma_1")],
            )
        ),
    ]
    out["C"] = [
        str(
            write_run(
                directory,
                "c_alpha_%s" % stamp(at(12, 0)),
                set_id="alpha",
                set_name="Альфа",
                started=at(12, 0),
                finished=at(12, 0, 30),
                batch_id=BATCH_C,
                cases=[case_dict("alpha_1"), case_dict("alpha_2")],
            )
        ),
    ]
    out["D"] = [
        str(
            write_run(
                directory,
                "d_delta_%s" % stamp(at(13, 0)),
                set_id="delta",
                set_name="Дельта",
                started=at(13, 0),
                finished=at(13, 0, 20),
                batch_id=BATCH_D,
                model=MODEL_B,
                cases=[case_dict("delta_1")],
            )
        ),
        str(
            write_run(
                directory,
                "d_delta_%s" % stamp(at(13, 0, 21)),
                set_id="delta",
                set_name="Дельта",
                started=at(13, 0, 21),
                finished=at(13, 0, 40),
                batch_id=BATCH_D,
                model=MODEL_B,
                cases=[case_dict("delta_2")],
            )
        ),
    ]
    return out


def make_cfg(tmp: Path, results: Path) -> AppConfig:
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        tests_dir=str(Path(__file__).resolve().parent / "tests"),
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
# 1. модель данных


def test_stats(tmp: Path) -> None:
    print("\n1. Метрики прогона и наборов (results_index)")
    results = tmp / "results"
    build_fixtures(results)
    cfg = make_cfg(tmp, results)
    groups = {g.key: g for g in group_records(load_runs(cfg))}

    check("партий собрано четыре", len(groups) == 4, str(len(groups)))

    a = groups.get(BATCH_A)
    if a is None:
        check("партия A найдена", False, BATCH_A)
        return
    check("в партии A три набора", len(a.set_ids) == 3, str(a.set_ids))
    check("A: один столбец на партию, а не три файла", len(a.runs) == 3)

    stats = a.stats
    check("A: кейсов 4 (2 альфа + 1 бета + 1 скорость)", stats.cases == 4, str(stats.cases))
    check(
        "A: счёт совпадает со summary (2 из 3)",
        (stats.passed, stats.counted) == (a.passed, a.counted) == (2, 3),
        "%s/%s против %s/%s" % (stats.passed, stats.counted, a.passed, a.counted),
    )
    check("A: пропуск не идёт в знаменатель", stats.skipped == 1, str(stats.skipped))
    check(
        "A: средний tokens/sec посчитан по кейсам",
        stats.tokens_per_sec == round((50.0 + 50.0 + 50.0 + 90.0) / 4, 1),
        str(stats.tokens_per_sec),
    )

    per_set = {st.set_id: st for st in a.set_stats}
    check("разбивка по наборам — три строки", len(per_set) == 3, str(list(per_set)))
    check(
        "набор speed: без проверок, но с метрикой",
        per_set["speed"].score is None and per_set["speed"].tokens_per_sec == 90.0,
        "%s / %s" % (per_set["speed"].score, per_set["speed"].tokens_per_sec),
    )
    check(
        "сумма по наборам складывается в прогон",
        (
            sum(st.passed for st in a.set_stats),
            sum(st.counted for st in a.set_stats),
            sum(st.cases for st in a.set_stats),
        )
        == (stats.passed, stats.counted, stats.cases),
    )

    d = groups.get(BATCH_D)
    check(
        "дубликат набора в партии слит в одну строку",
        d is not None and len(d.set_stats) == 1 and d.set_stats[0].cases == 2,
        str([st.cases for st in d.set_stats]) if d else "партии нет",
    )

    # Файл без разобранных кейсов: счёт обязан прийти из summary, иначе
    # сравнение показало бы нули там, где в файле честные «2 из 2».
    broken_path = write_run(
        results,
        "broken",
        set_id="alpha",
        set_name="Альфа",
        started=at(15, 0),
        finished=at(15, 0, 10),
        batch_id="broken_batch",
        cases=[case_dict("alpha_1"), case_dict("alpha_2")],
        omit_cases=True,
    )
    broken = next((g for g in group_records(load_runs(cfg)) if g.key == "broken_batch"), None)
    check(
        "файл без cases[] берёт счёт из summary",
        broken is not None and broken.stats.cases == 2 and broken.stats.passed == 2,
        "%s" % (broken.stats if broken else "партии нет"),
    )
    broken_path.unlink()


# ----------------------------------------------------------------------
# 2. столбцы — прогоны


def test_columns(tmp: Path, fixtures: dict[str, list[str]]) -> None:
    print("\n2. Столбец сравнения — прогон целиком")
    results = tmp / "results"
    cfg = make_cfg(tmp, results)
    tab = CompareTab(cfg)

    tab.load(fixtures["A"])
    check(
        "партия из трёх файлов даёт один столбец",
        tab.table.columnCount() == 2,
        "столбцов %d" % tab.table.columnCount(),
    )
    check(
        "в шапке — модель, а не имя файла",
        tab.table.horizontalHeaderItem(1).text() == "alpha-model-q4_k_m",
        tab.table.horizontalHeaderItem(1).text(),
    )

    tab.load(fixtures["A"] + fixtures["B"])
    check(
        "два прогона — две колонки",
        tab.table.columnCount() == 3,
        "столбцов %d" % tab.table.columnCount(),
    )
    labels = [tab.table.horizontalHeaderItem(i).text() for i in (1, 2)]
    check(
        "колонки подписаны моделями A и B",
        labels == ["alpha-model-q4_k_m", "beta-model-q8_0"],
        str(labels),
    )

    # Прогоны одной модели: имя в шапке совпадает, и различает их строка
    # «Прогон» с датой — в шапку она не влезает (см. `column_labels`).
    tab.load(fixtures["A"] + fixtures["C"])
    labels = [tab.table.horizontalHeaderItem(i).text() for i in (1, 2)]
    check("два прогона одной модели подписаны одинаково", labels[0] == labels[1], str(labels))
    when = _row_of(tab, "when")
    check(
        "различает их строка «Прогон» с датой",
        when is not None and tab.table.item(when, 1).text() != tab.table.item(when, 2).text(),
        "%s / %s"
        % (
            tab.table.item(when, 1).text() if when else "—",
            tab.table.item(when, 2).text() if when else "—",
        ),
    )
    # Длинное имя модели: заголовок обязан быть обрезан. `QHeaderView`
    # длинный текст не укорачивает, а рисует поверх соседней колонки — на
    # снимке экрана это выглядело как сдвинутая шапка.
    long_model = "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf"
    long_path = write_run(
        results,
        "long_name_%s" % stamp(at(16, 0)),
        set_id="alpha",
        set_name="Альфа",
        started=at(16, 0),
        finished=at(16, 0, 30),
        batch_id="long_batch",
        model=long_model,
        cases=[case_dict("alpha_1")],
    )
    tab.load(fixtures["A"] + [str(long_path)])
    header = tab.table.horizontalHeaderItem(2).text()
    check(
        "длинное имя модели в шапке обрезано, а не наезжает на соседний столбец",
        len(header) <= theme.HEADER_CHARS and header.endswith("…"),
        header,
    )
    check(
        "полное имя модели осталось в подсказке шапки",
        Path(long_model).stem.lower() in tab.table.horizontalHeaderItem(2).toolTip().lower(),
        tab.table.horizontalHeaderItem(2).toolTip(),
    )
    long_path.unlink()
    check(
        "порядок колонок хронологический",
        tab._groups[0].started < tab._groups[1].started,
        "%s / %s" % (tab._groups[0].started, tab._groups[1].started),
    )

    # Файлы одного запуска не должны разъехаться по колонкам, даже если
    # пришли вперемешку.
    mixed = [
        fixtures["B"][2],
        fixtures["A"][0],
        fixtures["B"][0],
        fixtures["A"][2],
        fixtures["B"][1],
        fixtures["A"][1],
    ]
    tab.load(mixed)
    check(
        "перемешанные файлы всё равно дают две колонки",
        tab.table.columnCount() == 3,
        "столбцов %d" % tab.table.columnCount(),
    )
    check(
        "наборов в первой колонке три",
        len(tab._groups[0].set_ids) == 3,
        str(tab._groups[0].set_ids),
    )

    tab.set_empty(True)
    check(
        "после закрытия столбец только с подписью",
        tab.table.columnCount() == 1 and tab.placeholder.isVisibleTo(tab),
    )


# ----------------------------------------------------------------------
# 3. детализация


def test_drilldown(tmp: Path, fixtures: dict[str, list[str]]) -> None:
    print("\n3. Детализация: разворот строки по наборам")
    results = tmp / "results"
    cfg = make_cfg(tmp, results)
    tab = CompareTab(cfg)
    tab.load(fixtures["A"] + fixtures["B"])

    base_rows = tab.table.rowCount()
    check(
        "сводных строк — столько же, сколько в ROWS",
        base_rows == len(ROWS),
        "%d против %d" % (base_rows, len(ROWS)),
    )

    score_row = _row_of(tab, "score")
    check("строка «Качество» найдена", score_row is not None, str(score_row))
    tab._on_cell_clicked(score_row, 0)
    added = tab.table.rowCount() - base_rows
    check(
        "разворот добавил строку на каждый набор объединения (4)",
        added == 4,
        "добавлено %d" % added,
    )

    subs = [
        tab.table.item(r, 0).text()
        for r in range(tab.table.rowCount())
        if tab._row_set(r) is not None
    ]
    check(
        "подстроки подписаны именами наборов",
        any("Альфа" in s for s in subs) and any("Гамма" in s for s in subs),
        str(subs),
    )
    check("подстроки с отступом", all(s.startswith(" ") for s in subs), str(subs))

    # Треугольник — признак разворачиваемой строки. Он обязан стоять ровно
    # у тех строк, что складываются из наборов: «Модель» разворачивать не во
    # что, а «Качество» — есть во что.
    triangles = {
        tab._row_key(r)
        for r in range(tab.table.rowCount())
        if tab._row_set(r) is None and not tab.table.item(r, 0).icon().isNull()
    }
    check(
        "треугольник разворота — только у строк из наборов",
        triangles == EXPANDABLE_ROWS,
        str(sorted(triangles)),
    )

    # Гамма есть только у B: у A в этой подстроке обязан стоять прочерк.
    gamma_row = _row_of_set(tab, "gamma")
    check("подстрока набора gamma найдена", gamma_row is not None, str(gamma_row))
    check(
        "набор, которого в прогоне не было, показан прочерком",
        tab.table.item(gamma_row, 1).text() == "—" and tab.table.item(gamma_row, 2).text() != "—",
        "%s / %s" % (tab.table.item(gamma_row, 1).text(), tab.table.item(gamma_row, 2).text()),
    )

    # Значение подстроки — по набору, а не по прогону.
    alpha_row = _row_of_set(tab, "alpha")
    check(
        "в подстроке набора — счёт набора (1 из 2 против 2 из 2)",
        tab.table.item(alpha_row, 1).text() == "1/2 · 50%"
        and tab.table.item(alpha_row, 2).text() == "2/2 · 100%",
        "%s / %s" % (tab.table.item(alpha_row, 1).text(), tab.table.item(alpha_row, 2).text()),
    )

    # Клик по подписи подстроки ничего не разворачивает.
    before = tab.table.rowCount()
    tab._on_cell_clicked(alpha_row, 0)
    check(
        "клик по подстроке не трогает разворот родителя",
        tab.table.rowCount() == before,
        "%d → %d" % (before, tab.table.rowCount()),
    )

    # Повторный клик сворачивает.
    tab._on_cell_clicked(_row_of(tab, "score"), 0)
    check(
        "повторный клик сворачивает разворот",
        tab.table.rowCount() == base_rows,
        str(tab.table.rowCount()),
    )

    # «Только различия»: строки, где прогоны совпали, обязаны исчезнуть.
    tab.diff_btn.setChecked(True)
    diff_rows = tab.table.rowCount()
    labels = [tab.table.item(r, 0).text() for r in range(diff_rows)]
    check(
        "«Только различия» прячет совпавшие строки",
        diff_rows < base_rows,
        "%d против %d" % (diff_rows, base_rows),
    )
    check(
        "«Контекст» спрятан — у обоих прогонов он одинаков", "Контекст" not in labels, str(labels)
    )
    check("«Модель» осталась — модели разные", "Модель" in labels, str(labels))
    tab.diff_btn.setChecked(False)

    # Графики: по линии на прогон.
    check(
        "на графике tokens/sec — линия на каждый прогон",
        len(tab.tps_chart.series) == 2,
        str(len(tab.tps_chart.series)),
    )
    check(
        "точек у линии — по числу кейсов прогона",
        [len(v) for _n, _c, v in tab.tps_chart.series] == [4, 4],
        str([len(v) for _n, _c, v in tab.tps_chart.series]),
    )
    check(
        "подписи оси X — кейсы, а не прогоны",
        tab.tps_chart.x_labels == ["кейс 1", "кейс 2", "кейс 3", "кейс 4"],
        str(tab.tps_chart.x_labels),
    )


def _row_of(tab: CompareTab, key: str) -> int | None:
    """Номер строки по ключу метрики. Ищем каждый раз заново.

    Запоминать номера нельзя: разворот сдвигает строки, и сохранённый индекс
    после него указывает на другую строку — ровно та ловушка, из-за которой
    ключ строки лежит в самой ячейке.
    """
    for row in range(tab.table.rowCount()):
        if tab._row_key(row) == key and tab._row_set(row) is None:
            return row
    return None


def _row_of_set(tab: CompareTab, set_id: str) -> int | None:
    for row in range(tab.table.rowCount()):
        if tab._row_set(row) == set_id:
            return row
    return None


# ----------------------------------------------------------------------
# 4. предупреждение о версиях и выгрузки


def test_warning_and_exports(tmp: Path, fixtures: dict[str, list[str]]) -> None:
    print("\n4. Версии наборов и выгрузки")
    results = tmp / "results"
    cfg = make_cfg(tmp, results)
    tab = CompareTab(cfg)

    tab.load(fixtures["A"])
    check(
        "один прогон — предупреждения нет",
        not tab._version_conflict and not tab.version_warning.isVisibleTo(tab),
    )

    tab.load(fixtures["A"] + fixtures["B"])
    check(
        "разные версии набора — предупреждение показано (п. 11.7 ТЗ)",
        tab._version_conflict and tab.version_warning.isVisibleTo(tab),
    )
    text = tab.version_warning.text()
    check(
        "в предупреждении — набор и обе версии",
        "beta" in text and "2.0.0" in text and "2.1.0" in text,
        text,
    )

    # Версии у наборов alpha и gamma совпадают: их в предупреждении быть не должно.
    check("совпавшие версии в предупреждение не попали", "alpha" not in text, text)

    csv_path = tmp / "comparison.csv"
    tab.export_csv(str(csv_path))
    rows = list(csv.reader(csv_path.open(encoding="utf-8-sig"), delimiter=";"))
    check(
        "CSV: первая строка — шапка со столбцами прогонов",
        rows[0][0] == "Параметр" and len(rows[0]) == 3,
        str(rows[0]),
    )
    check(
        "CSV: в шапке имена моделей",
        rows[0][1:] == ["alpha-model-q4_k_m", "beta-model-q8_0"],
        str(rows[0]),
    )
    check("CSV: строка на каждую метрику", len(rows) == 1 + tab.table.rowCount(), str(len(rows)))

    tab._on_cell_clicked(_row_of(tab, "score"), 0)
    tab.export_csv(str(csv_path))
    rows = list(csv.reader(csv_path.open(encoding="utf-8-sig"), delimiter=";"))
    check(
        "CSV: подстроки наборов тоже выгружены",
        any(row[0].strip() == "Бета" for row in rows[1:]),
        str([r[0] for r in rows[1:]]),
    )

    html_path = tmp / "comparison.html"
    tab.export_html(str(html_path))
    html = html_path.read_text(encoding="utf-8")
    check("HTML: есть шапка таблицы с прогонами", "<thead>" in html and "beta-model-q8_0" in html)
    check("HTML: предупреждение о версиях попало в файл", "2.1.0" in html)
    check("HTML: подстрока набора выгружена", "Бета" in html)


# ----------------------------------------------------------------------
# 5. диалог набора


def test_set_dialog(tmp: Path, fixtures: dict[str, list[str]]) -> None:
    print("\n5. Покейсовое сравнение набора")
    results = tmp / "results"
    cfg = make_cfg(tmp, results)
    tab = CompareTab(cfg)
    tab.load(fixtures["A"] + fixtures["B"])

    dialog = SetCompareDialog("alpha", "Альфа", tab._groups, cfg)
    check(
        "в диалоге — по столбцу на прогон с этим набором",
        dialog.table.columnCount() == 3,
        str(dialog.table.columnCount()),
    )
    check(
        "строк — две итоговые плюс кейсы",
        dialog.table.rowCount() == 4,
        str(dialog.table.rowCount()),
    )
    check(
        "первая строка — дата прогона",
        dialog.table.item(0, 0).text() == "Прогон"
        and dialog.table.item(0, 1).text()
        and dialog.table.item(0, 1).text() != dialog.table.item(0, 2).text(),
        "%s / %s" % (dialog.table.item(0, 1).text(), dialog.table.item(0, 2).text()),
    )
    check("итоговая строка подписана", dialog.table.item(1, 0).text() == "Итого")
    check(
        "итог: счёт набора по прогонам",
        dialog.table.item(1, 1).text() == "1/2 · 50%"
        and dialog.table.item(1, 2).text() == "2/2 · 100%",
        "%s / %s" % (dialog.table.item(1, 1).text(), dialog.table.item(1, 2).text()),
    )
    check(
        "строки подписаны идентификаторами кейсов",
        dialog.table.item(2, 0).text() == "alpha_1"
        and dialog.table.item(3, 0).text() == "alpha_2",
        "%s / %s" % (dialog.table.item(2, 0).text(), dialog.table.item(3, 0).text()),
    )
    check(
        "вердикты расставлены знаками",
        dialog.table.item(2, 1).text() == "✔" and dialog.table.item(3, 1).text() == "✘",
        "%s / %s" % (dialog.table.item(2, 1).text(), dialog.table.item(3, 1).text()),
    )
    entry = dialog.table.item(2, 1).data(Qt.UserRole)
    check(
        "в ячейке лежит пара (запись, кейс) — для карточки кейса",
        isinstance(entry, tuple) and len(entry) == 2 and entry[1].get("case_id") == "alpha_1",
        str(type(entry)),
    )

    # Прогон без этого набора не должен занимать столбец.
    gamma = SetCompareDialog("gamma", "Гамма", tab._groups, cfg)
    check(
        "прогон без набора отсеян из диалога",
        gamma.table.columnCount() == 2,
        str(gamma.table.columnCount()),
    )
    check(
        "отсеянный прогон назван в подписи",
        len(gamma.skipped) == 1,
        str([g.model for g in gamma.skipped]),
    )
    check(
        "в отсеянном прогоне — alpha-model",
        gamma.skipped[0].model == "alpha-model-q4_k_m",
        gamma.skipped[0].model,
    )


# ----------------------------------------------------------------------
# 6. история: кнопка сравнения


def test_history_button(tmp: Path, fixtures: dict[str, list[str]]) -> None:
    print("\n6. История: «Сравнить выбранные» включается по прогонам")
    results = tmp / "results"
    cfg = make_cfg(tmp, results)
    tab = HistoryTab(cfg)
    check("в истории четыре прогона", tab.total_groups() == 4, str(tab.total_groups()))
    check("без выделения кнопка выключена", not tab.compare_btn.isEnabled())

    group_a = next(g for g in tab._groups if g.batch_id == BATCH_A)
    tab._checked.update(group_a.run_ids)
    tab._sync_bulk_bar()
    check("один прогон из трёх файлов кнопку не включает", not tab.compare_btn.isEnabled())
    check(
        "в подписи полосы — один прогон, а не три файла",
        "1 прогон" in tab.bulk_label.text(),
        tab.bulk_label.text(),
    )

    group_b = next(g for g in tab._groups if g.batch_id == BATCH_B)
    tab._checked.update(group_b.run_ids)
    tab._sync_bulk_bar()
    check("два прогона кнопку включают", tab.compare_btn.isEnabled())

    emitted: list[list[str]] = []
    tab.compare_requested.connect(lambda paths: emitted.append(list(paths)))
    tab._compare()
    check(
        "в сравнение ушли файлы обоих прогонов целиком",
        len(emitted) == 1 and len(emitted[0]) == 6,
        str([len(e) for e in emitted]),
    )
    check(
        "файлы — все шесть, включая набор speed",
        any("speed" in p for p in emitted[0]),
        str(emitted[0]),
    )

    # Проверяем и обратный ход: то, что отдала история, вкладка сравнения
    # обязана собрать в те же два прогона — иначе «Сравнить» и «Сравнение»
    # показывали бы разное число столбцов.
    compare = CompareTab(cfg)
    compare.load(emitted[0])
    check(
        "сравнение собрало ровно два прогона из шести файлов",
        compare.table.columnCount() == 3,
        "столбцов %d" % compare.table.columnCount(),
    )


# ----------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("Проверка «Сравнения»: столбец — прогон, строка — метрика")
    print("=" * 72)
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    tmp = Path(tempfile.mkdtemp(prefix="llmtestbench_compare_"))
    project_config = Path(__file__).resolve().parent / "config.json"
    config_before = _fingerprint(project_config)
    try:
        test_stats(tmp / "stats")
        # Части 2–6 работают с одной папкой: файлы одни и те же, а каждая
        # часть поднимает своё окно и не зависит от предыдущего.
        results = tmp / "results"
        fixtures = build_fixtures(results)
        test_columns(tmp, fixtures)
        test_drilldown(tmp, fixtures)
        test_warning_and_exports(tmp, fixtures)
        test_set_dialog(tmp, fixtures)
        test_history_button(tmp, fixtures)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print("\n7. Страж: рабочий config.json проекта")
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
