"""Единый вход на все проверки проекта.

Зачем это появилось. К этому моменту в корне лежат тринадцать файлов
`check_*.py`, и запускались они руками по памяти — то есть «прогнал проверки»
означало «вспомнил те, что вспомнил». CI гонял только часть из них. Скрипт
собирает всё в один прогон и печатает одну сводку, чтобы полнота проверки не
зависела от памяти автора.

Живой llama-server на 127.0.0.1:8080 не требуется. Проверки, которым он нужен
для части блоков (`check_ui_server`, `check_server_manager`), умеют
деградировать: пропускают блок и пишут об этом в выводе. Две другие
(`check_set_selection`, `check_speed_metrics`) поднимают собственный сервер на
свободном порту и от внешнего не зависят вовсе.

Запуск:
    .venv/Scripts/python.exe check_all.py               # всё
    .venv/Scripts/python.exe check_all.py --fast        # без GUI-проверок
    .venv/Scripts/python.exe check_all.py --list        # только список
    .venv/Scripts/python.exe check_all.py check_infra check_review
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

#: Разбор сводки вида «Пройдено: 25   Провалено: 0».
SUMMARY_RE = re.compile(r"Пройдено:\s*(\d+)\s+Провалено:\s*(\d+)")

#: Вывод проверок длинный (в `check_history_tree` 128 случаев) — не режем.
MAX_OUTPUT_LINES = 400


@dataclass(frozen=True)
class Check:
    """Одна проверка в общем прогоне."""

    script: str
    title: str
    gui: bool = False
    #: Дополнительные ключи: у `check_review.py` режим обязателен.
    args: tuple[str, ...] = ()
    #: Сколько строк вывода показывать при провале или в подробном режиме.
    tail_lines: int = MAX_OUTPUT_LINES


#: Порядок — от дешёвых и общих к дорогим и предметным, чтобы падение
#: на первых строчках не заставляло ждать минуту до понятной ошибки.
CHECKS: tuple[Check, ...] = (
    Check("check_imports.py", "неиспользуемые импорты"),
    # Режим у check_review.py обязателен, а аудит легаси печатает сотни строк —
    # в сводке нужен только хвост с «Блокирующих: N».
    Check("check_review.py", "правила ревью по всему проекту", args=("--all",), tail_lines=30),
    Check("check_infra.py", "атомарная запись и логирование"),
    Check("check_database.py", "обёртка SQLite: запись, чтение, откат"),
    Check("check_testsets.py", "наборы кейсов и типы проверок"),
    Check("check_generators.py", "генераторы промптов"),
    Check("check_memory_metrics.py", "память модели: лог → отчёт"),
    Check("check_server_manager.py", "жизненный цикл сервера"),
    Check("check_history_tree.py", "дерево истории", gui=True),
    Check("check_compare.py", "сравнение прогонов", gui=True),
    Check("check_ui_server.py", "страница «Сервер»", gui=True),
    Check("check_set_selection.py", "прогон по флажкам наборов", gui=True),
    Check("check_speed_metrics.py", "метрики скорости", gui=True),
)


def make_env() -> dict[str, str]:
    """Окружение для дочерней проверки: без GUI-окна и с UTF-8 на выводе."""
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Без этого дочерний процесс на Windows пишет в cp1251 и кириллица в
    # перехваченном выводе превращается в мусор — сводку не прочитать.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_command(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Запустить команду и вернуть код возврата вместе с выводом."""
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return 127, "не удалось запустить: %s" % exc
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tail(text: str, limit: int = MAX_OUTPUT_LINES) -> str:
    """Обрезать слишком длинный вывод, сохранив начало и конец."""
    lines = text.rstrip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    half = limit // 2
    skipped = len(lines) - limit
    return "\n".join(lines[:half] + ["", "… пропущено строк: %d …" % skipped, ""] + lines[-half:])


@dataclass
class Outcome:
    """Результат одной проверки для сводки."""

    script: str
    title: str
    code: int
    seconds: float
    passed: int | None = None
    failed: int | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def run_check(check: Check, env: dict[str, str], verbose: bool) -> Outcome:
    path = ROOT / check.script
    if not path.exists():
        return Outcome(check.script, check.title, 127, 0.0, note="файла нет")

    started = time.perf_counter()
    code, output = run_command([PY, str(path), *check.args], env)
    seconds = time.perf_counter() - started

    match = SUMMARY_RE.search(output)
    passed = int(match.group(1)) if match else None
    failed = int(match.group(2)) if match else None

    if verbose or code != 0:
        print("=" * 72)
        print("%s — %s" % (check.script, check.title))
        print("=" * 72)
        print(tail(output, check.tail_lines) if output.strip() else "(пустой вывод)")
        print()

    return Outcome(check.script, check.title, code, seconds, passed, failed)


def run_ruff(env: dict[str, str], verbose: bool) -> list[Outcome]:
    """Линтер и проверка формата: тот же набор, что в pre-commit и в CI."""
    results = []
    for args, title in (
        (["check", "."], "ruff check (E, F, B)"),
        (["format", "--check", "."], "ruff format --check"),
    ):
        started = time.perf_counter()
        code, output = run_command([PY, "-m", "ruff", *args], env)
        seconds = time.perf_counter() - started
        name = "ruff %s" % args[0]
        if code == 127 or "No module named" in output:
            results.append(Outcome(name, title, 0, seconds, note="ruff не установлен — пропущено"))
            continue
        if verbose or code != 0:
            print("=" * 72)
            print("%s — %s" % (name, title))
            print("=" * 72)
            print(tail(output) if output.strip() else "(пустой вывод)")
            print()
        results.append(Outcome(name, title, code, seconds))
    return results


def print_summary(outcomes: list[Outcome]) -> int:
    print("=" * 72)
    print("СВОДКА")
    print("=" * 72)
    failed = 0
    for item in outcomes:
        if item.note:
            mark = "--  "
        elif item.ok:
            mark = "OK  "
        else:
            mark = "ПРОВАЛ"
            failed += 1

        counts = ""
        if item.passed is not None:
            counts = "  %d/%d" % (item.passed, item.passed + (item.failed or 0))
        print("%-7s %-28s %-34s %6.1f с%s" % (mark, item.script, item.title, item.seconds, counts))
        if item.note:
            print("        %s" % item.note)

    print("-" * 72)
    print("Проверок: %d   провалов: %d" % (len(outcomes), failed))
    if failed:
        print("Разбирать по одной: .venv/Scripts/python.exe <файл>")
    else:
        print("Всё чисто.")
    print("=" * 72)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон всех проверок проекта одной командой")
    parser.add_argument("only", nargs="*", help="имена проверок (без .py), иначе все")
    parser.add_argument("--fast", action="store_true", help="без GUI-проверок")
    parser.add_argument("--list", action="store_true", help="показать список и выйти")
    parser.add_argument("--quiet", action="store_true", help="не печатать вывод успешных проверок")
    args = parser.parse_args()

    selected = list(CHECKS)
    if args.fast:
        selected = [c for c in selected if not c.gui]
    if args.only:
        wanted = {name.removesuffix(".py") for name in args.only}
        known = {c.script.removesuffix(".py") for c in CHECKS}
        unknown = sorted(wanted - known)
        if unknown:
            print("Не знаю таких проверок: %s" % ", ".join(unknown), file=sys.stderr)
            print("Список: --list", file=sys.stderr)
            return 2
        selected = [c for c in selected if c.script.removesuffix(".py") in wanted]

    if args.list:
        for check in CHECKS:
            print("%-28s %-34s %s" % (check.script, check.title, "GUI" if check.gui else ""))
        return 0

    if not selected:
        print("Нечего запускать: фильтр не оставил ни одной проверки", file=sys.stderr)
        return 2

    env = make_env()
    print("Проверок в прогоне: %d" % len(selected))
    print(
        "Сервер на 8080: %s"
        % ("поднят" if server_up() else "не отвечает (часть блоков пропустится)")
    )
    print()

    outcomes: list[Outcome] = []
    for check in selected:
        outcomes.append(run_check(check, env, verbose=not args.quiet))
    outcomes.extend(run_ruff(env, verbose=not args.quiet))

    return print_summary(outcomes)


def server_up() -> bool:
    """Отвечает ли живой сервер — только для строки в шапке.

    Через `connect_ex` это делать нельзя: `settimeout()` переводит сокет в
    неблокирующий режим, и вызов возвращает 10035 (WSAEWOULDBLOCK) даже когда
    порт свободен. Поэтому спрашиваем сам сервер, как это делают проверки.
    """
    import requests

    try:
        answer = requests.get("http://127.0.0.1:8080/health", timeout=2)
    except Exception:  # noqa: BLE001 — сервер может быть просто не поднят
        return False
    # 503 — сервер поднят, но модель ещё грузится: это тоже «отвечает».
    return answer.status_code in (200, 503)


if __name__ == "__main__":
    sys.exit(main())
