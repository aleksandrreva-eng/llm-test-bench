"""Проверка модуля database.

База — SQLite-обёртка, и проверка строится вокруг неё: временная база,
запись, чтение, удаление, откат при ошибке. Проверяет, что обёртка
не теряет данные и не оставляет висячих case_results при удалении run.

Запуск:  .venv/Scripts/python.exe check_database.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from llmtestbench.database import DBCaseResult, DBRun, DatabaseManager

ok_count = 0
fail_count = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  OK   {label}")
    else:
        fail_count += 1
        print(f"  ПРОВАЛ {label}  ({detail})")


def main() -> int:
    print("Проверка модуля database")
    print("=" * 72)

    tmp = Path(__file__).resolve().parent / "tmp_check_database.db"
    tmp.unlink(missing_ok=True)

    mgr = DatabaseManager(tmp)

    # 1. Сохранение прогона и кейсов
    run = DBRun(
        run_id="r1",
        model="test-model",
        set_id="chat_single",
        set_name="Чат",
        started="2026-09-26T10:00:00",
        finished="2026-09-26T10:01:00",
        status="finished",
        total=2,
        passed=1,
        failed=1,
        scored=5.0,
        duration=60.0,
        params={"runs": 1},
    )
    cases = [
        DBCaseResult(
            run_id="r1",
            case_id="c1",
            name="Первый",
            prompt="привет",
            answer="привет",
            reasoning="",
            prompt_tokens=1,
            completion_tokens=1,
            ttft_ms=100.0,
            total_ms=200.0,
            passed=True,
            reason="ok",
            check_type="exact_match",
            judge_score=None,
            judge_response=None,
            judge_error=None,
            extra={},
        ),
        DBCaseResult(
            run_id="r1",
            case_id="c2",
            name="Второй",
            prompt="пока",
            answer="пока",
            reasoning="",
            prompt_tokens=1,
            completion_tokens=1,
            ttft_ms=100.0,
            total_ms=200.0,
            passed=False,
            reason="fail",
            check_type="exact_match",
            judge_score=None,
            judge_response=None,
            judge_error=None,
            extra={"tag": "demo"},
        ),
    ]
    mgr.save_run(run, cases)

    all_runs = mgr.get_all_runs()
    check("прогон записан", len(all_runs) == 1, str(len(all_runs)))
    check("run_id совпадает", all_runs[0]["run_id"] == "r1")
    check("params разобраны", all_runs[0]["params"] == '{"runs": 1}')

    # 2. Детали с кейсами
    details = mgr.get_run_details("r1")
    check("детали найдены", details is not None)
    if details:
        check("два кейса", len(details["cases"]) == 2, str(len(details["cases"])))
        check("params обратно в dict", details.get("params") == {"runs": 1})

    # 3. Фильтр по set_id
    filtered = mgr.get_all_runs(set_id="chat_single")
    check("фильтр по set_id", len(filtered) == 1)
    filtered_empty = mgr.get_all_runs(set_id="нет_такого")
    check("фильтр пустой", len(filtered_empty) == 0)

    # 4. Удаление прогона каскадом
    mgr.delete_run("r1")
    check("прогон удалён", len(mgr.get_all_runs()) == 0)
    # case_results тоже должны исчезнуть
    raw = mgr.query_runs("SELECT COUNT(*) AS n FROM case_results WHERE run_id = 'r1'")
    check("кейсы удалены каскадно", raw[0]["n"] == 0, str(raw[0]["n"]))

    # 5. Прямой SQL
    mgr.save_run(run, cases)
    rows = mgr.query_runs("SELECT * FROM runs WHERE status = ?", ("finished",))
    check("прямой SQL", len(rows) == 1)

    # 6. Откат при ошибке (искусственный сбой)
    bad_run = DBRun(
        run_id="r2",
        model="m",
        set_id="s",
        set_name="S",
        started="2026-09-26T10:00:00",
        finished=None,
        status="failed",
        total=0,
        passed=0,
        failed=0,
        scored=None,
        duration=0.0,
    )
    try:
        # Передаём неправильный тип вместо списка — executemany упадёт
        mgr.save_run(bad_run, "не список")  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — сбой здесь ожидаем: проверяем откат, а не ошибку
        pass
    # После отката r2 не должен появиться
    check("откат при сбое", len(mgr.get_all_runs()) == 1, str(len(mgr.get_all_runs())))

    tmp.unlink(missing_ok=True)

    print(f"\n{'=' * 72}")
    print(f"Пройдено: {ok_count}   Провалено: {fail_count}")
    print("=" * 72)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
