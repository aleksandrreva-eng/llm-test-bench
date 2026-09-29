"""Проверка тестовых наборов и типов проверок.

Запуск:
    .venv\\Scripts\\python.exe check_testsets.py

Делает две вещи:
1. Загружает все наборы из tests/ и печатает сводку с ошибками валидации.
2. Прогоняет каждый тип проверки на заранее известных парах
   «ответ → ожидаемый вердикт». Негативные случаи важнее позитивных: проверка,
   которая всегда говорит «да», выглядит рабочей, но не проверяет ничего.

Код возврата 1, если есть ошибки в наборах или проваленный тест проверки.
"""

from __future__ import annotations

import sys
from pathlib import Path

from llmtestbench.checks import run_check
from llmtestbench.testsets import discover_test_sets, load_test_set

ROOT = Path(__file__).resolve().parent
TESTS_DIR = ROOT / "tests"

# ---------------------------------------------------------------------------
# тесты типов проверок: (описание, expected, ответ, ожидаемый passed)

CHECK_CASES: list[tuple[str, dict, str, bool | None]] = [
    # contains_any
    (
        "contains_any: значение есть",
        {"type": "contains_any", "values": ["Париж", "Paris"]},
        "Столица Франции — Париж.",
        True,
    ),
    (
        "contains_any: ничего нет",
        {"type": "contains_any", "values": ["Париж", "Paris"]},
        "Столица Франции — Лион.",
        False,
    ),
    (
        "contains_any: регистр и ё/е",
        {"type": "contains_any", "values": ["Королёв"]},
        "город КОРОЛЕВ",
        True,
    ),
    # contains_all
    (
        "contains_all: всё есть",
        {"type": "contains_all", "values": ["Париж", "Берлин"]},
        "Париж, Лондон, Берлин.",
        True,
    ),
    (
        "contains_all: одного нет",
        {"type": "contains_all", "values": ["Париж", "Берлин"]},
        "Париж и Лондон.",
        False,
    ),
    # not_contains
    (
        "not_contains: чисто",
        {"type": "not_contains", "values": ["не могу"]},
        "Вот название: НейроКофе.",
        True,
    ),
    (
        "not_contains: фраза есть",
        {"type": "not_contains", "values": ["не могу"]},
        "Извините, не могу придумать.",
        False,
    ),
    # regex
    (
        "regex: дата найдена",
        {"type": "regex", "pattern": r"\b2026-09-24\b"},
        "Дата: 2026-09-24",
        True,
    ),
    (
        "regex: даты нет",
        {"type": "regex", "pattern": r"\b2026-09-24\b"},
        "Дата: 24.09.2026",
        False,
    ),
    # exact_match
    ("exact_match: точное совпадение", {"type": "exact_match", "values": ["24"]}, "24", True),
    ("exact_match: с точкой в конце", {"type": "exact_match", "values": ["24"]}, "24.", True),
    ("exact_match: с префиксом", {"type": "exact_match", "values": ["24"]}, "Ответ: 24", False),
    # json_schema
    (
        "json_schema: валидный JSON",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["year"],
                "properties": {"year": {"type": "integer", "minimum": 1900}},
            },
        },
        '{"year": 1967}',
        True,
    ),
    (
        "json_schema: markdown-обёртка — нарушение формата",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["year"],
                "properties": {"year": {"type": "integer"}},
            },
        },
        '```json\n{"year": 1967}\n```',
        False,
    ),
    (
        "json_schema: нет обязательного поля",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["year"],
                "properties": {"year": {"type": "integer"}},
            },
        },
        '{"title": "книга"}',
        False,
    ),
    (
        "json_schema: неверный тип",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["year"],
                "properties": {"year": {"type": "integer"}},
            },
        },
        '{"year": "тысяча"}',
        False,
    ),
    (
        "json_schema: лишние поля запрещены",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["a"],
                "additionalProperties": False,
                "properties": {"a": {"type": "string"}},
            },
        },
        '{"a": "x", "b": 1}',
        False,
    ),
    (
        "json_schema: не JSON вовсе",
        {
            "type": "json_schema",
            "schema": {"type": "object", "properties": {}},
        },
        "Год издания — 1967.",
        False,
    ),
    # tool_call
    (
        "tool_call: верный вызов",
        {
            "type": "tool_call",
            "tool_call": {"name": "get_weather", "arguments": {"city": "Москва"}},
        },
        '{"name": "get_weather", "arguments": {"city": "Москва"}}',
        True,
    ),
    (
        "tool_call: другой инструмент",
        {
            "type": "tool_call",
            "tool_call": {"name": "get_weather", "arguments": {"city": "Москва"}},
        },
        '{"name": "get_time", "arguments": {"city": "Москва"}}',
        False,
    ),
    (
        "tool_call: неверные аргументы",
        {
            "type": "tool_call",
            "tool_call": {"name": "get_weather", "arguments": {"city": "Москва"}},
        },
        '{"name": "get_weather", "arguments": {"city": "Казань"}}',
        False,
    ),
    (
        "tool_call: вложенный формат OpenAI",
        {
            "type": "tool_call",
            "tool_call": {"name": "multiply", "arguments": {"a": 12, "b": 8}},
        },
        '{"name": "multiply", "arguments": {"a": 12, "b": 8}}',
        True,
    ),
    # no_tool_call — «инструмент вызывать не нужно»
    (
        "no_tool_call: обычный текст",
        {"type": "no_tool_call"},
        "Семнадцать умножить на двадцать три — триста девяносто один.",
        True,
    ),
    (
        "no_tool_call: вызов инструмента",
        {"type": "no_tool_call"},
        '{"name": "multiply", "arguments": {"a": 17, "b": 23}}',
        False,
    ),
    ("no_tool_call: JSON без вызова", {"type": "no_tool_call"}, '{"result": 391}', True),
    (
        "no_tool_call: список вызовов",
        {"type": "no_tool_call"},
        '[{"tool": "multiply", "args": {"a": 2, "b": 3}}]',
        False,
    ),
    # all_of — несколько условий сразу
    (
        "all_of: все условия выполнены",
        {
            "type": "all_of",
            "checks": [
                {"type": "contains_all", "values": ["Париж"]},
                {"type": "not_contains", "values": ["Лион"]},
            ],
        },
        "Столица Франции — Париж.",
        True,
    ),
    (
        "all_of: одно условие провалено",
        {
            "type": "all_of",
            "checks": [
                {"type": "contains_all", "values": ["Париж"]},
                {"type": "not_contains", "values": ["Лион"]},
            ],
        },
        "Париж, как и Лион, стоит на реке.",
        False,
    ),
    ("all_of: пустой список условий — пропуск", {"type": "all_of"}, "любой ответ", None),
    (
        "all_of: вложенный пропуск не зачитывается как успех",
        {
            "type": "all_of",
            "checks": [
                {"type": "contains_all", "values": ["Париж"]},
                {"type": "llm_judge", "golden": "эталон"},
            ],
        },
        "Париж.",
        None,
    ),
    # word_count — длина ответа в словах
    ("word_count: ровно три слова", {"type": "word_count", "exact": 3}, "Кот спит дома.", True),
    ("word_count: слов меньше нужного", {"type": "word_count", "exact": 3}, "Кот спит.", False),
    (
        "word_count: больше верхней границы",
        {"type": "word_count", "max": 5},
        "раз два три четыре пять шесть",
        False,
    ),
    (
        "word_count: markdown-звёздочки не слова",
        {"type": "word_count", "exact": 2},
        "**да** нет",
        True,
    ),
    ("word_count: нет границ — пропуск", {"type": "word_count"}, "любой ответ", None),
    # set_equal — ровно заданный набор, порядок не важен
    (
        "set_equal: тот же набор в другом порядке",
        {
            "type": "set_equal",
            "values": ["красный", "зелёный", "синий"],
        },
        "синий, красный, зеленый",
        True,
    ),
    (
        "set_equal: лишний элемент",
        {
            "type": "set_equal",
            "values": ["красный", "зелёный", "синий"],
        },
        "синий, красный, зеленый, белый",
        False,
    ),
    (
        "set_equal: не хватает элемента",
        {
            "type": "set_equal",
            "values": ["красный", "зелёный", "синий"],
        },
        "синий, красный",
        False,
    ),
    (
        "set_equal: JSON-массив",
        {"type": "set_equal", "values": ["альфа", "бета"]},
        '["бета", "альфа"]',
        True,
    ),
    (
        "set_equal: нумерованный список",
        {"type": "set_equal", "values": ["один", "два"]},
        "1. один\n2. два",
        True,
    ),
    # json_schema: новые ключи схемы
    (
        "json_schema: повторяющиеся элементы запрещены",
        {
            "type": "json_schema",
            "schema": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
        '["a", "b", "a"]',
        False,
    ),
    (
        "json_schema: минимум полей",
        {
            "type": "json_schema",
            "schema": {"type": "object", "minProperties": 2},
        },
        '{"a": 1}',
        False,
    ),
    (
        "json_schema: максимум полей",
        {
            "type": "json_schema",
            "schema": {"type": "object", "maxProperties": 1},
        },
        '{"a": 1, "b": 2}',
        False,
    ),
    (
        "json_schema: const не зависит от регистра",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["city"],
                "properties": {"city": {"const": "Канберра"}},
            },
        },
        '{"city": "канберра"}',
        True,
    ),
    (
        "json_schema: enum не зависит от регистра",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["p"],
                "properties": {"p": {"enum": ["low", "high"]}},
            },
        },
        '{"p": "HIGH"}',
        True,
    ),
    (
        "json_schema: const всё ещё ловит подмену",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["city"],
                "properties": {"city": {"const": "Канберра"}},
            },
        },
        '{"city": "Сидней"}',
        False,
    ),
    # отложенные типы
    (
        "llm_judge: пропуск, а не провал",
        {"type": "llm_judge", "golden": "эталон"},
        "любой ответ",
        None,
    ),
    ("embedding: пропуск", {"type": "embedding", "golden": "эталон"}, "любой ответ", None),
    ("неизвестный тип: пропуск", {"type": "telepathy"}, "любой ответ", None),
    ("нет критерия: пропуск", {}, "любой ответ", None),
]


def check_checks() -> int:
    """Прогнать тесты типов проверок. Возвращает число провалов."""
    print("=== Проверка типов проверок ===")
    failed = 0
    for name, expected, response, want in CHECK_CASES:
        result = run_check(expected, response)
        ok = result.passed is want
        mark = "OK  " if ok else "ПРОВАЛ"
        if not ok:
            failed += 1
        verdict = {True: "прошло", False: "провал", None: "пропуск"}[result.passed]
        print(
            "  [%s] %-46s → %s%s"
            % (
                mark,
                name,
                verdict,
                ""
                if ok
                else "  (ожидалось %s)" % {True: "прошло", False: "провал", None: "пропуск"}[want],
            )
        )
    print("  всего %d, провалов %d" % (len(CHECK_CASES), failed))
    print()
    return failed


def check_sets() -> int:
    """Загрузить и проверить все наборы. Возвращает число проблем."""
    print("=== Тестовые наборы ===")
    if not TESTS_DIR.is_dir():
        print("  папка tests/ не найдена")
        return 1

    sets = discover_test_sets(TESTS_DIR)
    if not sets:
        print("  наборов не найдено")
        return 1

    problems = 0
    total_cases = 0
    for tset in sets:
        total_cases += tset.cases_count
        bad = tset.bad_cases
        state = "OK" if tset.ok and not bad else "ЕСТЬ ЗАМЕЧАНИЯ"
        print(
            "  %-18s v%-7s кейсов %2d  теги: %s  [%s]"
            % (tset.id, tset.version, tset.cases_count, ", ".join(tset.all_tags()) or "—", state)
        )
        for err in tset.errors:
            print("      набор: %s" % err)
            problems += 1
        for case in bad:
            print("      кейс %s: %s" % (case.id, "; ".join(case.errors)))
            problems += 1
    print("  наборов %d, кейсов всего %d, замечаний %d" % (len(sets), total_cases, problems))
    print()
    return problems


def show_filter_demo() -> None:
    """Показать работу фильтра по тегам и лимита кейсов (п. 3.2 ТЗ)."""
    print("=== Фильтр по тегам и лимит ===")
    path = TESTS_DIR / "chat_single"
    if not (path / "manifest.json").is_file():
        print("  набор chat_single не найден")
        return
    tset = load_test_set(path)
    print("  chat_single: всего %d" % tset.cases_count)
    for tags in (["basic"], ["format"], ["reasoning"], ["basic", "format"]):
        picked = tset.filtered(tags=tags)
        print(
            "    теги %-22s → %d кейсов: %s"
            % (", ".join(tags), len(picked), ", ".join(c.id for c in picked) or "—")
        )
    limited = tset.filtered(limit=3)
    print("    лимит 3 → %s" % ", ".join(c.id for c in limited))
    print()


def main() -> int:
    print("LLM Test Bench — проверка наборов и проверок\n")
    failures = check_checks()
    problems = check_sets()
    show_filter_demo()
    if failures or problems:
        print(
            "ИТОГ: есть проблемы — провалов проверок %d, замечаний к наборам %d"
            % (failures, problems)
        )
        return 1
    print("ИТОГ: всё чисто")
    return 0


if __name__ == "__main__":
    sys.exit(main())
