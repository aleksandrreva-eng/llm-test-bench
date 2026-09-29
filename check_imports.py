"""Проверка неиспользуемых импортов по всему проекту.

Нужна потому, что ruff и pyflakes в venv проекта не стоят (и ставить их
ради одной проверки не хочется): разбираем файлы модулем `ast` и смотрим,
встречается ли импортированное имя где-нибудь ещё в файле.

Проверка намеренно простая и потому может ошибаться в одну сторону —
объявить используемым то, что используется только в аннотации. Обратная
ошибка (сказать «не используется» про нужное) означала бы удаление
рабочего импорта, поэтому имена, встречающиеся хоть где-то, считаются
использованными.

Запуск:  .venv/Scripts/python.exe check_imports.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SKIP_DIRS = {".venv", "__pycache__", ".git", "build", "dist"}
# Реэкспорты: имя импортируется, чтобы его взяли снаружи пакета.
REEXPORT_FILES = {"__init__.py"}


def imported_names(tree: ast.Module) -> dict[str, int]:
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                found[alias.asname or alias.name] = node.lineno
    return found


def used_names(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
    return used


def check_file(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [(0, "не разобрать: %s" % exc)]
    used = used_names(tree)
    bad = []
    for name, line in sorted(imported_names(tree).items(), key=lambda kv: kv[1]):
        if name in used or name == "annotations":
            continue
        bad.append((line, name))
    return bad


def main() -> int:
    root = Path(__file__).resolve().parent
    files = [p for p in sorted(root.rglob("*.py")) if not (SKIP_DIRS & set(p.parts))]
    print("Проверка импортов: файлов %d" % len(files))
    print("-" * 72)
    total = 0
    for path in files:
        bad = check_file(path)
        if path.name in REEXPORT_FILES:
            # В __init__.py импорт — это реэкспорт, он «не используется» по определению.
            bad = [(ln, n) for ln, n in bad if False]
        if not bad:
            continue
        rel = path.relative_to(root)
        for line, name in bad:
            print("%-42s строка %4d: %s" % (rel, line, name))
            total += 1
    print("-" * 72)
    print("Неиспользуемых импортов: %d" % total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
