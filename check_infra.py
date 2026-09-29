"""Проверки эксплуатационной обвязки: атомарная запись и логирование.

Два модуля, без которых «готово к продакшену» — только слова, и которые
глазами проверяются неверно (кажется, что запись файла проверять нечем):

    llmtestbench/atomic_io.py     — временный файл + os.replace
    llmtestbench/logging_setup.py — файл лога с ротацией

Проверяется не «функция вызвалась», а след на диске: файл читается обратно,
мусор не остаётся, при сбое старое содержимое цело, уровень логирования
действительно фильтрует записи, ротация действительно переключает файл.
Отдельно стережётся главное правило: **в библиотеке нет записи в обход
atomic_io** (кроме дозаписи в конец — так ведёт себя лог llama-server) —
иначе одна забытая точка снова открывает дыру, ради которой всё и делалось.
И отдельно проверяется, что этот сторож сам не ослеп: детектор обязан
находить подставные `write_text`, `open(..., "w")` и `path.open("w")`.

Запуск:  .venv/Scripts/python.exe check_infra.py
"""

from __future__ import annotations

import ast
import json
import logging
import logging.handlers
import sys
import tempfile
from pathlib import Path

from llmtestbench.atomic_io import (
    CSV_ENCODING,
    write_csv_atomic,
    write_json_atomic,
    write_text_atomic,
)
from llmtestbench.logging_setup import (
    LOG_FILE_NAME,
    LOG_MAX_BYTES,
    get_logger,
    setup_logging,
    shutdown_logging,
)

BOM = b"\xef\xbb\xbf"

#: Режимы, при которых файл создаётся заново или усекается. Дозапись (`a`) сюда
#: не входит: она не уничтожает то, что уже лежит, и нужна серверному логу —
#: llama-server дописывается в конец живым процессом, атомарной подмены тут
#: быть не может.
TRUNCATE_MODES = ("w", "x", "+")

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Записать результат одной проверки."""
    suffix = f" — {detail}" if detail else ""
    RESULTS.append((bool(ok), f"{name}{suffix}"))


# ---------------------------------------------------------------- atomic_io


def test_json_roundtrip(tmp: Path) -> None:
    path = write_json_atomic(tmp / "run.json", {"модель": "Ornith 1.5", "числа": [1, 2]})
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    check(
        "JSON читается обратно, кириллица не экранирована",
        data["модель"] == "Ornith 1.5" and "\\u" not in raw,
    )
    check("JSON заканчивается переводом строки", raw.endswith("\n"))


def test_no_temp_left(tmp: Path) -> None:
    folder = tmp / "clean"
    write_json_atomic(folder / "a.json", {"x": 1})
    leftovers = [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]
    check("временных файлов после записи не остаётся", not leftovers, ", ".join(leftovers))


def test_overwrite(tmp: Path) -> None:
    path = tmp / "over.json"
    write_text_atomic(path, "старое содержимое, длинное")
    write_text_atomic(path, "новое")
    text = path.read_text(encoding="utf-8")
    check("перезапись заменяет файл целиком, хвост не остаётся", text == "новое", repr(text))


def test_parent_dirs(tmp: Path) -> None:
    path = write_text_atomic(tmp / "a" / "b" / "c.txt", "глубоко")
    check("вложенные папки создаются на ходу", path.is_file())


def test_failure_keeps_neighbours(tmp: Path) -> None:
    """Сбой записи не должен портить соседние файлы и оставлять мусор."""
    folder = tmp / "fail"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "target.json"
    write_json_atomic(target, {"версия": 1})
    before = target.read_text(encoding="utf-8")

    # Путь занят папкой: os.replace не сможет подменить её файлом.
    blocked = folder / "занято"
    blocked.mkdir()

    raised = False
    try:
        write_text_atomic(blocked, "текст")
    except OSError:
        raised = True

    leftovers = [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]
    check("сбой записи поднимает ошибку, а не глотается", raised)
    check("после сбоя временный файл удалён", not leftovers, ", ".join(leftovers))
    check("после сбоя соседний файл цел", target.read_text(encoding="utf-8") == before)


def test_csv(tmp: Path) -> None:
    path = write_csv_atomic(tmp / "t.csv", [["id", "name"], ["1", "тест"]], delimiter=";")
    text = path.read_text(encoding=CSV_ENCODING)
    check("CSV пишется с BOM — Excel видит кириллицу", path.read_bytes().startswith(BOM))
    check("CSV использует заданный разделитель", "id;name" in text)
    check("CSV содержит строку данных", "1;тест" in text)


# ---------------------------------------------------------------- логирование


def test_logging(tmp: Path) -> None:
    shutdown_logging()
    logs = tmp / "logs"
    path = setup_logging(logs, "info", console=False)
    log = get_logger("check_infra.test")
    log.info("строка проверки %s", "номер один")
    log.debug("эту строку видеть не должны")

    text = path.read_text(encoding="utf-8")
    check("лог-файл создан в заданной папке", path.is_file() and path.name == LOG_FILE_NAME)
    check("запись логгера попала в файл", "строка проверки номер один" in text)
    check("уровень info отсекает debug", "видеть не должны" not in text)


def test_logging_idempotent(tmp: Path) -> None:
    shutdown_logging()
    first = setup_logging(tmp / "logs_idem", "info", console=False)
    second = setup_logging(tmp / "другая_папка", "info", console=False)
    get_logger("check_infra.test").warning("единственная строка")
    text = first.read_text(encoding="utf-8")
    check("повторная настройка не переключает путь к логу", second == first)
    check("повторная настройка не дублирует записи", text.count("единственная строка") == 1)


def test_console_flag(tmp: Path) -> None:
    shutdown_logging()
    setup_logging(tmp / "logs_console", "info", console=False)
    streams = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]
    check("console=False не подключает вывод в поток", not streams)


def test_rotation(tmp: Path) -> None:
    shutdown_logging()
    path = setup_logging(tmp / "logs_rot", "info", console=False)
    log = get_logger("check_infra.test.rotation")
    line = "х" * 200
    writes = LOG_MAX_BYTES // len(line.encode("utf-8")) + 50
    for _ in range(writes):
        log.info(line)

    rotated = path.with_name(f"{path.name}.1")
    check("ротация переключает файл при превышении размера", rotated.is_file())
    check(
        "текущий файл не растёт выше лимита",
        path.stat().st_size <= LOG_MAX_BYTES,
        f"{path.stat().st_size} байт",
    )


def test_log_levels(tmp: Path) -> None:
    shutdown_logging()
    path = setup_logging(tmp / "logs_level", "warning", console=False)
    log = get_logger("check_infra.test.level")
    log.info("информация не нужна")
    log.warning("предупреждение нужно")
    text = path.read_text(encoding="utf-8")
    check("уровень warning отсекает info", "информация не нужна" not in text)
    check("уровень warning пропускает warning", "предупреждение нужно" in text)
    shutdown_logging()


# ---------------------------------------------------------------- связка с кодом


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_call_name(func.value)}.{func.attr}"
    return ""


def _open_is_write(node: ast.Call) -> bool:
    """Усекает ли вызов `open()` файл: второй аргумент или `mode=`.

    Позиция режима зависит от формы вызова: у `open(path, "w")` это второй
    аргумент, у `path.open("w")` — первый. Проверка, которая смотрит только
    на второй, пропускает вызов методом — а именно он и появляется в коде
    чаще всего.
    """
    position = 0 if isinstance(node.func, ast.Attribute) else 1
    mode = ""
    if len(node.args) > position and isinstance(node.args[position], ast.Constant):
        mode = str(node.args[position].value)
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = str(keyword.value.value)
    if "a" in mode:
        return False
    return any(flag in mode for flag in TRUNCATE_MODES)


def _direct_write_offenders(source: str, label: str) -> list[str]:
    """Точки записи в обход atomic_io в одном файле.

    Смотрим и на `write_text`/`write_bytes`, и на `open(...)` в усекающем
    режиме — включая вызов методом (`path.open("w")`), который легко не
    заметить: имя вызова приходит как `path.open`, а не `open`.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{label}: не разобрать — {exc}"]

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func).split(".")[-1]
        if name in {"write_text", "write_bytes"} or (name == "open" and _open_is_write(node)):
            found.append(f"{label}:{node.lineno}")
    return found


def test_no_direct_writes() -> None:
    """Прямая запись в обход atomic_io снова открыла бы дыру, ради которой
    всё делалось. Проверяем по AST, а не по памяти: точка записи может
    появиться в любом модуле, и заметить её на ревью — не гарантия.

    Известные исключения, которые проверка не ловит и не должна:
    `server_manager.py` дописывает лог llama-server в режиме `a` (живой процесс
    пишет в конец, подменять файл нельзя), `config.py` копирует папку тестов
    из бандла через `shutil.copytree` (дерево, а не файл).
    """
    root = Path(__file__).resolve().parent / "llmtestbench"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "atomic_io.py":
            continue
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        offenders.extend(_direct_write_offenders(source, rel))
    check("запись файлов идёт только через atomic_io", not offenders, ", ".join(offenders))


def test_detector_sees_direct_writes() -> None:
    """Проверка выше обязана ловить то, ради чего написана. Иначе она тихо
    сломается — и никто не заметит, что защита больше не защищает."""
    sample = (
        "def f(p):\n"
        "    p.write_text('x')\n"
        "    open(p, 'w')\n"
        "    p.open('w')\n"
        "    p.write_bytes(b'x')\n"
    )
    caught = _direct_write_offenders(sample, "образец")
    check(
        "детектор видит write_text, write_bytes, open и Path.open", len(caught) == 4, str(caught)
    )

    allowed = "def f(p):\n    with p.open('a') as handle:\n        handle.write('строка')\n"
    missed = _direct_write_offenders(allowed, "образец")
    check("дозапись в конец нарушением не считается", not missed, str(missed))


def test_modules_have_logger() -> None:
    """Модули, которые пишут на диск, обязаны иметь логгер: без него ошибка
    записи снова исчезнет без следа."""
    from llmtestbench import gguf_scanner, importer, local_agent, runner

    modules = {
        "runner": runner,
        "importer": importer,
        "local_agent": local_agent,
        "gguf_scanner": gguf_scanner,
    }
    missing = [name for name, module in modules.items() if not hasattr(module, "log")]
    check("модули берут логгер из logging_setup", not missing, ", ".join(missing))


def main() -> int:
    print("Проверка обвязки: атомарная запись и логирование")
    print("-" * 72)
    tmp = Path(tempfile.mkdtemp(prefix="chk_infra_"))
    try:
        test_json_roundtrip(tmp)
        test_no_temp_left(tmp)
        test_overwrite(tmp)
        test_parent_dirs(tmp)
        test_failure_keeps_neighbours(tmp)
        test_csv(tmp)
        test_logging(tmp)
        test_logging_idempotent(tmp)
        test_console_flag(tmp)
        test_log_levels(tmp)
        test_rotation(tmp)
        test_no_direct_writes()
        test_detector_sees_direct_writes()
        test_modules_have_logger()
    finally:
        shutdown_logging()

    failed = 0
    for ok, name in RESULTS:
        print(f"  {'ok  ' if ok else 'ПРОВАЛ'} {name}")
        if not ok:
            failed += 1
    print("-" * 72)
    print(f"Пройдено: {len(RESULTS) - failed}   Провалено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
