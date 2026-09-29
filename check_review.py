"""Автоматическая часть чек-листа код-ревью (см. REVIEW_STANDARDS.md).

Ruff ловит синтаксис, мёртвые импорты и явные баги, но не ловит то, из-за чего
в проекте «плавало» качество: ошибку, которая исчезает без следа, безымянное
число в лимите и новую логику без проверки. Этот скрипт закрывает именно эти
пункты чек-листа:

    R1  глухой `except` — от ошибки не осталось следа          (O2, блок)
    R2  `except ... as e`, где `e` не читается                 (O3, блок)
    R3  магическое число в горячем коде                        (O4, совет)
    R4  новый модуль не упомянут ни в одном check_*.py         (O6, совет)
    R5  %-форматирование в новой строке                        (P1, совет)
    R6  TODO/FIXME без задачи                                  (совет)
    R7  отладка в коде: breakpoint(), print() в библиотеке     (блок/совет)

Проверяются ТОЛЬКО новые и изменённые строки — по умолчанию те, что лежат в
индексе. Это принципиально: в проекте 20 000 строк, и требовать «переписать
всё сразу» бессмысленно (см. §7 REVIEW_STANDARDS.md). Стандарт требует, чтобы
новый код не ухудшал существующий, — значит, и проверять надо новый код.

Режимы:
    --staged        строки в индексе (это режим pre-commit)
    --diff REV      строки, добавленные относительно REV
    --all           весь проект: аудит легаси, а не приговор
    --file PATH     один файл целиком

Запуск:
    .venv/Scripts/python.exe check_review.py --staged
    .venv/Scripts/python.exe check_review.py --all --no-fail

Отказ от правила на конкретной строке — комментарий `# noqa` на ней или в теле
обработчика. Отказ должен быть осознанным: он виден в диффе и объясняется там же.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".venv", "__pycache__", ".git", "build", "dist", ".cache", ".ruff_cache"}

# «Горячий» код: здесь магическое число и print считаются замечанием.
# Проверки (check_*.py) и черновики сюда не входят — там числа и печать по делу.
HOT_PREFIX = "llmtestbench/"

# Новые модули здесь обязаны быть покрыты хотя бы одним check_*.py (правило O6).
COVERAGE_PREFIX = "llmtestbench/"
COVERAGE_GLOB = "check_*.py"

# Число меньше этого порога магическим не считаем: 0, 1, 2 — это счёт, а не лимит.
MAGIC_MIN = 100

NOQA = "noqa"
BLOCK = "блок"
ADVICE = "совет"

# Где искать «глухой» except: только широкие типы. `except ValueError: return None`
# осознанно оставлен в стороне — это не «проглотил всё», а конкретное решение.
BROAD_EXCEPTIONS = {"Exception", "BaseException"}

DEBUG_CALLS = {"breakpoint", "set_trace", "pdb.set_trace"}


@dataclass
class Finding:
    rule: str
    level: str
    path: str
    line: int
    message: str


# ---------------------------------------------------------------- разбор AST


def _stmt_map(tree: ast.AST) -> dict[int, ast.stmt]:
    """Узел -> ближайший охватывающий оператор.

    Нужна, чтобы отличить `TIMEOUT = 900` (имя есть, всё хорошо) от `timeout=900`
    внутри вызова. Ближайший оператор ищем обходом сверху вниз: `ast.walk` тут
    не годится — он не говорит, какой оператор вложен в какой.
    """
    found: dict[int, ast.stmt] = {}

    def visit(node: ast.AST, current: ast.stmt | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.stmt) else current
            found[id(child)] = inner
            visit(child, inner)

    visit(tree, None)
    return found


def _walk_body(body: list[ast.stmt]):
    """Обход тела обработчика без захода во вложенные функции и классы.

    Вложенная функция — отдельная жизнь: она может быть вызвана или не вызвана,
    и её `raise` не говорит, что ошибка обработана здесь.
    """
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _has_action(body: list[ast.stmt]) -> bool:
    """Остался ли от ошибки след: запись в лог, пометка в статусе или повторный raise."""
    return any(isinstance(node, (ast.Call, ast.Raise)) for node in _walk_body(body))


def _name_used(body: list[ast.stmt], name: str) -> bool:
    return any(isinstance(node, ast.Name) and node.id == name for node in _walk_body(body))


def _is_broad(handler_type: ast.expr | None) -> bool:
    if handler_type is None:
        return True
    if isinstance(handler_type, ast.Name):
        return handler_type.id in BROAD_EXCEPTIONS
    if isinstance(handler_type, ast.Attribute):
        return handler_type.attr in BROAD_EXCEPTIONS
    return False


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_call_name(func.value)}.{func.attr}"
    return ""


def _named_constant(stmt: ast.stmt) -> bool:
    """`LIMIT = 900` и `LIMIT: int = 900` — имя есть, число объяснено."""
    targets: list[ast.expr] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    return any(isinstance(target, ast.Name) and target.id.isupper() for target in targets)


def _noqa(source: list[str], node: ast.AST) -> bool:
    """Отказ от правила — комментарий `# noqa` на строке находки или в её теле."""
    first = getattr(node, "lineno", 1)
    last = getattr(node, "end_lineno", first) or first
    return any(NOQA in line for line in source[first - 1 : last])


# ---------------------------------------------------------------- правила


def check_except(tree: ast.AST, lines: set[int], path: str, source: list[str]):
    """R1 — глухой except; R2 — имя исключения не читается."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.lineno not in lines:
            continue
        if _noqa(source, node):
            continue
        if node.name and not node.name.startswith("_") and not _name_used(node.body, node.name):
            yield Finding(
                "R2",
                BLOCK,
                path,
                node.lineno,
                f"`as {node.name}` не читается: либо используйте, либо уберите имя",
            )
        if _is_broad(node.type) and not _has_action(node.body):
            yield Finding(
                "R1",
                BLOCK,
                path,
                node.lineno,
                "глухой except: ошибка исчезает без следа — нужен лог, "
                "пометка в статусе или повторный raise",
            )


def check_debug(tree: ast.AST, lines: set[int], path: str, source: list[str]):
    """R7 — отладка, забытая в коде."""
    hot = path.startswith(HOT_PREFIX)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno not in lines:
            continue
        if _noqa(source, node):
            continue
        name = _call_name(node.func)
        if name in DEBUG_CALLS:
            yield Finding(
                "R7",
                BLOCK,
                path,
                node.lineno,
                f"{name}() в коде — отладку убираем перед отправкой",
            )
        elif name == "print" and hot:
            yield Finding(
                "R7",
                ADVICE,
                path,
                node.lineno,
                "print() в модуле: печатать должны CLI и проверки, а не библиотека",
            )


def check_magic(tree: ast.AST, lines: set[int], path: str, source: list[str]):
    """R3 — число без имени в горячем коде."""
    if not path.startswith(HOT_PREFIX):
        return
    statements = _stmt_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        if not isinstance(node.value, (int, float)):
            continue
        if node.lineno not in lines or _noqa(source, node):
            continue
        if abs(node.value) < MAGIC_MIN:
            continue
        if _named_constant(statements.get(id(node))):  # type: ignore[arg-type]
            continue
        yield Finding(
            "R3",
            ADVICE,
            path,
            node.lineno,
            f"{node.value} без имени: лимит, таймаут или вес стоит назвать константой",
        )


def check_percent(tree: ast.AST, lines: set[int], path: str, source: list[str]):
    """R5 — %-форматирование в новой строке."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mod):
            continue
        if not isinstance(node.left, ast.Constant) or not isinstance(node.left.value, str):
            continue
        if node.lineno in lines and not _noqa(source, node):
            yield Finding(
                "R5",
                ADVICE,
                path,
                node.lineno,
                "%-форматирование: в новом коде пишем f-строки (P1)",
            )


MARKER = re.compile(r"\b(TODO|FIXME)\b(?!\s*[\(:]\s*\w)")


def check_markers(lines: set[int], path: str, source: list[str]):
    """R6 — маркер без задачи.

    Ищем только в комментарии: слово TODO встречается и в тексте документации,
    и в самих правилах ниже, и замечание на «упоминание правила» — ложное.
    """
    for number in sorted(lines):
        if number < 1 or number > len(source):
            continue
        text = source[number - 1]
        if NOQA in text:
            continue
        comment = text.find("#")
        if comment < 0:
            continue
        match = MARKER.search(text, comment)
        if match:
            yield Finding(
                "R6",
                ADVICE,
                path,
                number,
                f"{match.group(1)} без задачи: непонятно, когда и зачем это снимут",
            )


def check_coverage(root: Path, new_files: set[str]):
    """R4 — новый модуль без проверки."""
    if not new_files:
        return
    blob = "\n".join(
        script.read_text(encoding="utf-8", errors="replace")
        for script in sorted(root.glob(COVERAGE_GLOB))
    )
    for rel in sorted(new_files):
        stem = Path(rel).stem
        if stem and stem not in blob:
            yield Finding(
                "R4",
                ADVICE,
                rel,
                0,
                f"новый модуль: ни один {COVERAGE_GLOB} его не упоминает (O6)",
            )


RULES = (check_except, check_debug, check_magic, check_percent)


# ---------------------------------------------------------------- сбор строк


def run_git(root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(f"git {' '.join(args)} — код {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def parse_diff(text: str) -> dict[str, set[int]]:
    """Номера строк, добавленных в каждой правке.

    `--unified=0` — в выводе только сами добавления, без контекста, поэтому
    счётчик строк не сбивается на неизменённых строках.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    number = 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            current = (
                None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            )
            if current is not None:
                changed.setdefault(current, set())
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", raw)
            number = int(match.group(1)) if match else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if current is not None:
                changed[current].add(number)
            number += 1
        elif raw.startswith(" "):
            number += 1
    return changed


def diff_lines(root: Path, rev: str | None, staged: bool) -> dict[str, set[int]]:
    where = ["--cached"] if staged else [rev or "HEAD"]
    return parse_diff(run_git(root, ["diff", *where, "--unified=0", "--no-color", "--", "*.py"]))


def diff_new_files(root: Path, rev: str | None, staged: bool) -> set[str]:
    where = ["--cached"] if staged else [rev or "HEAD"]
    out = run_git(root, ["diff", *where, "--name-only", "--diff-filter=A", "--", "*.py"])
    return {name.strip() for name in out.splitlines() if name.strip().startswith(COVERAGE_PREFIX)}


def iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS & set(path.parts):
            continue
        yield path


def all_lines(root: Path, only: str | None) -> dict[str, set[int]]:
    target = (root / only).resolve() if only else None
    if target is not None and not target.is_file():
        print(f"файл не найден: {only}")
        return {}
    found: dict[str, set[int]] = {}
    for path in iter_py_files(root):
        if target is not None and path.resolve() != target:
            continue
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        found[path.relative_to(root).as_posix()] = set(range(1, count + 1))
    return found


# ---------------------------------------------------------------- вывод


def inspect(root: Path, changed: dict[str, set[int]], new_files: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel, lines in sorted(changed.items()):
        if not lines:
            continue
        path = root / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            tree = ast.parse("\n".join(source))
        except SyntaxError as exc:
            print(f"{rel}: не разобрать — {exc}")
            continue
        for rule in RULES:
            findings.extend(rule(tree, lines, rel, source))
        findings.extend(check_markers(lines, rel, source))
    findings.extend(check_coverage(root, new_files))
    return findings


def report(findings: list[Finding], mode: str, checked: int) -> int:
    blocking = [item for item in findings if item.level == BLOCK]
    print(f"Проверка ревью — {mode}")
    print(f"Файлов с правками: {checked}")
    print("-" * 72)
    if not findings:
        print("Замечаний нет.")
    current = None
    for item in sorted(findings, key=lambda f: (f.path, f.line, f.rule)):
        if item.path != current:
            current = item.path
            print(f"\n{current}")
        place = f"строка {item.line:>5}" if item.line else "файл       "
        print(f"  {place}  [{item.level:<5}] {item.rule}  {item.message}")
    print("-" * 72)
    print(f"Блокирующих: {len(blocking)}, советов: {len(findings) - len(blocking)}")
    if blocking:
        print("ИТОГ: есть блокирующие замечания — правка не проходит ревью")
        return 1
    print("ИТОГ: блокирующих замечаний нет")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверки ревью по новым строкам")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="строки в индексе (pre-commit)")
    group.add_argument("--diff", metavar="REV", help="строки, добавленные относительно REV")
    group.add_argument("--all", action="store_true", help="весь проект: аудит легаси")
    group.add_argument("--file", metavar="PATH", help="один файл целиком")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="всегда возвращать 0: для аудита, где замечания к легаси ожидаемы",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent

    if args.all or args.file:
        changed = all_lines(root, args.file)
        new_files: set[str] = set()
        mode = "весь проект" if args.all else f"файл {args.file}"
    elif args.staged:
        changed = diff_lines(root, None, staged=True)
        new_files = diff_new_files(root, None, staged=True)
        mode = "строки в индексе"
    else:
        changed = diff_lines(root, args.diff, staged=False)
        new_files = diff_new_files(root, args.diff, staged=False)
        mode = f"строки, добавленные относительно {args.diff}"

    if not changed:
        print(f"Проверка ревью — {mode}")
        print("Изменённых .py-файлов нет — проверять нечего.")
        return 0

    findings = inspect(root, changed, new_files)
    code = report(findings, mode, len(changed))
    return 0 if args.no_fail else code


if __name__ == "__main__":
    sys.exit(main())
