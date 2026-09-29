"""Типы проверок ответов (раздел 11.4 ТЗ).

    contains_any   ответ содержит любое из значений
    contains_all   ответ содержит все значения
    not_contains   ответ не содержит ни одного значения
    regex          ответ соответствует регулярному выражению
    exact_match    полное совпадение (нормализованное)
    json_schema    ответ — валидный JSON по схеме
    tool_call      правильный вызов инструмента
    no_tool_call   ответ НЕ является вызовом инструмента (инструмент не нужен)
    all_of         выполнены все вложенные проверки сразу
    word_count     в ответе нужное число слов (exact / min / max)
    set_equal      ответ перечисляет ровно заданный набор (порядок не важен)
    llm_judge      оценка отдельной моделью-судьёй (этап 8, реализовано на
                  уровне набора через TestSet.scoring, см. llmtestbench/judge.py)
    embedding      косинусная близость к эталону (этап 8, зарезервировано)

`all_of` — основной инструмент для сложных кейсов: одна задача почти всегда
требует нескольких условий разом («ответь JSON'ом, ровно пять элементов, без
латиницы»). Проверять такое по одному условию за кейс бессмысленно: модель,
выполнившая три условия из четырёх, ничем не отличается от выполнившей ноль.

Проверка возвращает `CheckResult`, где `passed=None` означает «не проверялось»
— это не то же самое, что провал. Такие кейсы не идут в знаменатель итогового
счёта, иначе метрика врёт.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Типы, которые нельзя оценить без модели-судьи. Держим их в списке известных,
# чтобы наборы с ними проходили валидацию, но честно помечаем как пропущенные.
DEFERRED_TYPES = {"llm_judge", "embedding"}

KNOWN_TYPES = {
    "contains_any",
    "contains_all",
    "not_contains",
    "regex",
    "exact_match",
    "json_schema",
    "tool_call",
    "no_tool_call",
    "all_of",
    "word_count",
    "set_equal",
} | DEFERRED_TYPES

# Ключи, под которыми значение проверки может лежать в expected
_VALUE_KEYS = ("values", "value", "expected", "answers")


@dataclass
class CheckResult:
    """Результат одной проверки."""

    check_type: str
    passed: bool | None  # None — не проверялось (пропуск)
    reason: str = ""
    matched: str = ""  # что именно совпало (для лога и отчёта)
    details: dict = field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        return self.passed is None

    def as_dict(self) -> dict:
        return {
            "type": self.check_type,
            "passed": self.passed,
            "reason": self.reason,
            "matched": self.matched,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# нормализация


def normalize(text: str) -> str:
    """Привести текст к сравнимому виду.

    Приводим регистр, схлопываем пробелы и приравниваем «ё» к «е»: модели
    пишут «Королёв» и «Королев» вперемешку, и разница не должна стоить балла.
    Пунктуация по краям не трогается — она бывает значимой.
    """
    if text is None:
        return ""
    s = str(text).replace("ё", "е").replace("Ё", "Е")
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _values_from(expected: dict) -> list[str]:
    """Достать список значений из expected, под любым из принятых ключей."""
    for key in _VALUE_KEYS:
        if key in expected:
            return _as_list(expected[key])
    return []


# ---------------------------------------------------------------------------
# отдельные проверки


def _check_contains_any(response: str, expected: dict) -> CheckResult:
    values = _values_from(expected)
    if not values:
        return CheckResult("contains_any", None, "в expected не указаны значения")
    hay = normalize(response)
    for v in values:
        if normalize(v) in hay:
            return CheckResult("contains_any", True, matched=v, details={"values": values})
    return CheckResult(
        "contains_any",
        False,
        "нет ни одного из: %s" % ", ".join(values),
        details={"values": values},
    )


def _check_contains_all(response: str, expected: dict) -> CheckResult:
    values = _values_from(expected)
    if not values:
        return CheckResult("contains_all", None, "в expected не указаны значения")
    hay = normalize(response)
    missing = [v for v in values if normalize(v) not in hay]
    if missing:
        return CheckResult(
            "contains_all",
            False,
            "не найдено: %s" % ", ".join(missing),
            details={"values": values, "missing": missing},
        )
    return CheckResult("contains_all", True, matched=", ".join(values), details={"values": values})


def _check_not_contains(response: str, expected: dict) -> CheckResult:
    values = _values_from(expected)
    if not values:
        return CheckResult("not_contains", None, "в expected не указаны значения")
    hay = normalize(response)
    found = [v for v in values if normalize(v) in hay]
    if found:
        return CheckResult(
            "not_contains",
            False,
            "не должно быть, но найдено: %s" % ", ".join(found),
            details={"values": values, "found": found},
        )
    return CheckResult("not_contains", True, details={"values": values})


def _check_regex(response: str, expected: dict) -> CheckResult:
    pattern = expected.get("pattern") or expected.get("regex") or ""
    if not pattern:
        return CheckResult("regex", None, "в expected нет шаблона")
    flags = 0
    for name in _as_list(expected.get("flags")):
        flags |= getattr(re, name.upper(), 0)
    try:
        m = re.search(pattern, response or "", flags)
    except re.error as exc:
        return CheckResult(
            "regex", False, "битое выражение: %s" % exc, details={"pattern": pattern}
        )
    if m:
        return CheckResult("regex", True, matched=m.group(0)[:120], details={"pattern": pattern})
    return CheckResult(
        "regex", False, "не найдено по шаблону %s" % pattern, details={"pattern": pattern}
    )


def _check_exact_match(response: str, expected: dict) -> CheckResult:
    values = _values_from(expected)
    if not values:
        return CheckResult("exact_match", None, "в expected не указано значение")
    target = normalize(values[0])
    got = normalize(response)
    # Сравниваем и «как есть», и без завершающей пунктуации: модель часто
    # ставит точку там, где в эталоне её нет.
    if got == target or got.rstrip(".!?;:") == target.rstrip(".!?;:"):
        return CheckResult(
            "exact_match", True, matched=response.strip()[:120], details={"expected": values[0]}
        )
    return CheckResult(
        "exact_match",
        False,
        "ожидалось «%s», получено «%s»" % (values[0], (response or "").strip()[:80]),
        details={"expected": values[0]},
    )


_MD_FENCE_RE = re.compile(r"```")


def extract_json(response: str) -> tuple[Any, str]:
    """Разобрать ответ как JSON. Возвращает (объект, ошибка).

    Промпт требует «ТОЛЬКО валидный JSON без обёртки», поэтому markdown-забор
    считается нарушением формата даже при валидном содержимом: в реальном
    пайплайне такой ответ не распарсится без дополнительной чистки.
    """
    text = (response or "").strip()
    if not text:
        return None, "пустой ответ"
    if _MD_FENCE_RE.search(text):
        return None, "ответ обёрнут в markdown-блок (```), это нарушение формата"
    try:
        return json.loads(text), ""
    except ValueError as exc:
        return None, "не валидный JSON: %s" % exc


def _check_json_schema(response: str, expected: dict) -> CheckResult:
    schema = expected.get("schema") or expected.get("json_schema") or {}
    if not isinstance(schema, dict) or not schema:
        return CheckResult("json_schema", None, "в expected нет схемы")

    data, err = extract_json(response)
    if err:
        return CheckResult("json_schema", False, err, details={"schema": schema})

    errors = validate_json(data, schema)
    if errors:
        return CheckResult(
            "json_schema",
            False,
            "не соответствует схеме: %s" % "; ".join(errors[:3]),
            details={"schema": schema, "errors": errors},
        )
    return CheckResult("json_schema", True, matched="JSON валиден", details={"schema": schema})


def _check_tool_call(response: str, expected: dict) -> CheckResult:
    want = expected.get("tool_call") or expected.get("call") or {}
    if not isinstance(want, dict) or not want:
        return CheckResult("tool_call", None, "в expected нет описания вызова")

    data, err = extract_json(response)
    if err:
        return CheckResult("tool_call", False, err, details={"expected": want})

    # Модели возвращают и одиночный объект, и массив вызовов — принимаем оба.
    calls = data if isinstance(data, list) else [data]
    want_name = str(want.get("name") or "")
    want_args = want.get("arguments") or want.get("args") or {}

    problems: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            problems.append("элемент вызова не объект")
            continue
        name = str(call.get("name") or call.get("tool") or call.get("function") or "")
        args = call.get("arguments")
        if args is None:
            args = call.get("args")
        if isinstance(args, dict) and "arguments" in args and isinstance(args["arguments"], dict):
            args = args["arguments"]
        if name != want_name:
            problems.append("инструмент «%s» вместо «%s»" % (name or "—", want_name))
            continue
        if not isinstance(args, dict):
            problems.append("аргументы не объект")
            continue
        bad = [
            "%s=%r вместо %r" % (k, args.get(k), v)
            for k, v in want_args.items()
            if normalize(str(args.get(k))) != normalize(str(v))
        ]
        if bad:
            problems.append("; ".join(bad))
            continue
        extra = sorted(set(args) - set(want_args))
        return CheckResult(
            "tool_call",
            True,
            matched="%s(%s)" % (name, json.dumps(args, ensure_ascii=False)),
            details={"expected": want, "extra_args": extra},
        )

    return CheckResult(
        "tool_call",
        False,
        "; ".join(problems) or "подходящего вызова нет",
        details={"expected": want},
    )


#: Ключи, по которым видно, что JSON — это вызов инструмента, а не данные.
_TOOL_CALL_KEYS = ("arguments", "args", "tool", "function")


def _looks_like_tool_call(data: Any) -> bool:
    if isinstance(data, list):
        return any(_looks_like_tool_call(x) for x in data)
    if not isinstance(data, dict):
        return False
    return any(key in data for key in _TOOL_CALL_KEYS)


def _check_no_tool_call(response: str, expected: dict) -> CheckResult:
    """Ответ не должен быть вызовом инструмента.

    Кейсы «инструмент не нужен» ловят модель, которая на любой вопрос отвечает
    вызовом функции. Обычным `not_contains` такое не поймать: ключи вызова
    бывают разными (`name`/`tool`/`function`, `arguments`/`args`), и запрещать
    их по именам — значит запрещать и нормальный ответ про них.
    """
    data, err = extract_json(response)
    if err:
        # Не JSON — значит и не вызов инструмента. Формат ответа здесь
        # проверяет не эта проверка, а соседняя (обычно через all_of).
        return CheckResult(
            "no_tool_call",
            True,
            "ответ не JSON, вызова инструмента нет",
            matched=(response or "").strip()[:80],
        )
    if _looks_like_tool_call(data):
        return CheckResult(
            "no_tool_call",
            False,
            "вернулся вызов инструмента, хотя вызывать его не нужно: %s"
            % json.dumps(data, ensure_ascii=False)[:120],
            details={"parsed": data},
        )
    return CheckResult("no_tool_call", True, "вызова инструмента нет")


# ---------------------------------------------------------------------------
# составные и количественные проверки


def _check_all_of(response: str, expected: dict) -> CheckResult:
    """Все вложенные проверки должны пройти.

    `passed=None` у вложенной проверки означает «эту часть проверить нечем»;
    весь кейс тогда тоже пропуск, а не успех — иначе составной критерий
    зачитывался бы по одной выполненной части из трёх.
    """
    subs = expected.get("checks") or expected.get("all_of") or []
    if not isinstance(subs, list) or not subs:
        return CheckResult("all_of", None, "в expected нет списка проверок checks")
    results = [run_check(sub, response) for sub in subs]
    details = {"checks": [r.as_dict() for r in results]}

    failed = [r for r in results if r.passed is False]
    if failed:
        return CheckResult(
            "all_of",
            False,
            "; ".join("%s: %s" % (r.check_type, r.reason) for r in failed[:3]),
            details=details,
        )
    skipped = [r for r in results if r.passed is None]
    if skipped:
        return CheckResult(
            "all_of",
            None,
            "не проверено: %s" % ", ".join(r.check_type for r in skipped),
            details=details,
        )
    return CheckResult(
        "all_of", True, matched="выполнено условий: %d" % len(results), details=details
    )


#: Служебные символы markdown словом не считаются: «**пять**» — одно слово.
_MD_CHARS_RE = re.compile(r"[#*_`>\[\]()|]+")
_WORD_RE = re.compile(r"\S+")
_LETTER_OR_DIGIT_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")


def count_words(text: str) -> int:
    """Число слов в ответе — без markdown-разметки и висящей пунктуации."""
    cleaned = _MD_CHARS_RE.sub(" ", text or "")
    return sum(1 for w in _WORD_RE.findall(cleaned) if _LETTER_OR_DIGIT_RE.search(w))


def _check_word_count(response: str, expected: dict) -> CheckResult:
    if not any(k in expected for k in ("exact", "min", "max")):
        return CheckResult("word_count", None, "не задан ни exact, ни min, ни max")
    got = count_words(response)
    limits = {k: int(expected[k]) for k in ("exact", "min", "max") if k in expected}
    details = {"words": got, **limits}

    exact = limits.get("exact")
    if exact is not None and got != exact:
        return CheckResult(
            "word_count", False, "слов %d, нужно ровно %d" % (got, exact), details=details
        )
    low, high = limits.get("min"), limits.get("max")
    if low is not None and got < low:
        return CheckResult(
            "word_count", False, "слов %d, нужно не меньше %d" % (got, low), details=details
        )
    if high is not None and got > high:
        return CheckResult(
            "word_count", False, "слов %d, нужно не больше %d" % (got, high), details=details
        )
    return CheckResult("word_count", True, matched="слов %d" % got, details=details)


#: Маркеры списка, которые надо снять перед сравнением: «1. », «- », «• ».
_BULLET_RE = re.compile(r"^\s*(?:[-*•·–—]|\d+[.)])\s*")


def extract_items(response: str) -> list[str]:
    """Разобрать ответ как перечисление: JSON-массив или список строками."""
    text = (response or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    parts = re.split(r"[;\n]|,(?=\s)", text)
    return [_BULLET_RE.sub("", p).strip(" .;") for p in parts if p.strip(" .;\n\t")]


def _check_set_equal(response: str, expected: dict) -> CheckResult:
    """Ответ перечисляет ровно заданный набор — порядок не важен.

    Лишний элемент здесь такой же провал, как пропущенный: модель, добавившая
    к пяти цветам радуги шестой, задание не выполнила.
    """
    values = _values_from(expected)
    if not values:
        return CheckResult("set_equal", None, "в expected не указаны значения")
    got = {normalize(x) for x in extract_items(response)}
    got.discard("")
    want = {normalize(v) for v in values}
    missing = sorted(want - got)
    extra = sorted(got - want)
    details = {"expected": values, "got": sorted(got), "missing": missing, "extra": extra}
    if missing or extra:
        problems = []
        if missing:
            problems.append("не хватает: %s" % ", ".join(missing))
        if extra:
            problems.append("лишнее: %s" % ", ".join(extra))
        return CheckResult("set_equal", False, "; ".join(problems), details=details)
    return CheckResult("set_equal", True, matched=", ".join(values), details=details)


def _check_deferred(response: str, expected: dict, check_type: str) -> CheckResult:
    """llm_judge и embedding как per-case тип проверки.

    Судья реализован на уровне набора (TestSet.scoring → JudgeEvaluator), и
    именно оттуда приходит вердикт по качеству. Если же тип «llm_judge»
    указан прямо в expected кейса без блока scoring, честно помечаем кейс как
    пропущенный — выдумывать вердикт нельзя, а судью в этом контексте не с
    чем звать.
    """
    return CheckResult(
        check_type,
        None,
        "тип «%s» оценивается через scoring набора; в expected его нельзя "
        "звать напрямую" % check_type,
        details={"golden": expected.get("golden")},
    )


_DISPATCH = {
    "contains_any": _check_contains_any,
    "contains_all": _check_contains_all,
    "not_contains": _check_not_contains,
    "regex": _check_regex,
    "exact_match": _check_exact_match,
    "json_schema": _check_json_schema,
    "tool_call": _check_tool_call,
    "no_tool_call": _check_no_tool_call,
    "all_of": _check_all_of,
    "word_count": _check_word_count,
    "set_equal": _check_set_equal,
}


def run_check(expected: dict | None, response: str) -> CheckResult:
    """Выполнить проверку ответа по описанию `expected`."""
    if not isinstance(expected, dict) or not expected:
        return CheckResult("none", None, "у кейса нет критерия проверки")
    check_type = str(expected.get("type") or "").strip()
    if not check_type:
        return CheckResult("none", None, "в expected не указан тип проверки")
    # Пустой ответ не зачитывается ничем. `not_contains` на пустой строке
    # проходит формально верно — запрещённых слов там действительно нет, — но
    # модель не ответила, и это провал, а не успех. Правило стоит до ветки
    # отложенных проверок: отсутствие судьи не мешает увидеть, что ответа нет.
    if not str(response or "").strip():
        return CheckResult(check_type, False, "пустой ответ")
    if check_type in DEFERRED_TYPES:
        return _check_deferred(response, expected, check_type)
    handler = _DISPATCH.get(check_type)
    if handler is None:
        return CheckResult(check_type, None, "неизвестный тип проверки «%s»" % check_type)
    try:
        return handler(response, expected)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять прогон
        return CheckResult(check_type, False, "ошибка проверки: %s" % exc)


def describe_check(expected: dict | None) -> str:
    """Короткое описание критерия — для логов и таблиц."""
    if not isinstance(expected, dict) or not expected:
        return "—"
    t = str(expected.get("type") or "")
    if t in ("contains_any", "contains_all", "not_contains"):
        return "%s(%s)" % (t, ", ".join(_values_from(expected)))
    if t == "regex":
        return "regex(%s)" % (expected.get("pattern") or "")
    if t == "exact_match":
        vals = _values_from(expected)
        return "exact_match(%s)" % (vals[0] if vals else "")
    if t == "json_schema":
        return "json_schema"
    if t == "tool_call":
        want = expected.get("tool_call") or {}
        return "tool_call(%s)" % (want.get("name") or "")
    if t == "no_tool_call":
        return "no_tool_call"
    if t == "all_of":
        subs = expected.get("checks") or expected.get("all_of") or []
        return "all_of(%s)" % " + ".join(describe_check(s) for s in subs if isinstance(s, dict))
    if t == "word_count":
        if "exact" in expected:
            return "word_count(=%s)" % expected["exact"]
        return "word_count(%s..%s)" % (expected.get("min", "—"), expected.get("max", "—"))
    if t == "set_equal":
        return "set_equal(%s)" % ", ".join(_values_from(expected))
    return t or "—"


# ---------------------------------------------------------------------------
# минимальный валидатор JSON Schema


def validate_json(data: Any, schema: dict, path: str = "$") -> list[str]:
    """Проверить данные по схеме. Возвращает список расхождений.

    Поддержано подмножество JSON Schema, которого хватает для тестовых кейсов:
    type, required, properties, items, enum, const, minimum, maximum,
    minLength, maxLength, minItems, maxItems, uniqueItems, minProperties,
    maxProperties, pattern, additionalProperties. Полноценная схема тут
    не нужна, а тащить ради неё зависимость — лишнее.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    stype = schema.get("type")
    if stype:
        types = stype if isinstance(stype, list) else [stype]
        if not any(_type_ok(data, t) for t in types):
            errors.append(
                "%s: ожидался тип %s, получено %s" % (path, "/".join(types), _type_name(data))
            )
            return errors  # дальше проверять нечего

    if "enum" in schema and not _in_values(data, schema["enum"]):
        errors.append("%s: значение %r не из списка %r" % (path, data, schema["enum"]))
    if "const" in schema and not _same_value(data, schema["const"]):
        errors.append("%s: ожидалось %r" % (path, schema["const"]))

    if isinstance(data, dict):
        for key in schema.get("required", []) or []:
            if key not in data:
                errors.append("%s: нет обязательного поля «%s»" % (path, key))
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in data:
                errors.extend(validate_json(data[key], sub, "%s.%s" % (path, key)))
        if "minProperties" in schema and len(data) < schema["minProperties"]:
            errors.append(
                "%s: полей %d, нужно минимум %d" % (path, len(data), schema["minProperties"])
            )
        if "maxProperties" in schema and len(data) > schema["maxProperties"]:
            errors.append("%s: полей %d, максимум %d" % (path, len(data), schema["maxProperties"]))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(data) - set(props))
            if extra:
                errors.append("%s: лишние поля %s" % (path, ", ".join(extra)))

    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errors.append(
                "%s: элементов %d, нужно минимум %d" % (path, len(data), schema["minItems"])
            )
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append("%s: элементов %d, максимум %d" % (path, len(data), schema["maxItems"]))
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in data:
                if any(_same_value(item, s) for s in seen):
                    errors.append("%s: повторяющийся элемент %r" % (path, item))
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(data):
                errors.extend(validate_json(item, item_schema, "%s[%d]" % (path, i)))

    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append("%s: длина %d, минимум %d" % (path, len(data), schema["minLength"]))
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append("%s: длина %d, максимум %d" % (path, len(data), schema["maxLength"]))
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], data):
                    errors.append("%s: не подходит под шаблон %s" % (path, schema["pattern"]))
            except re.error:
                pass

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append("%s: %r меньше минимума %r" % (path, data, schema["minimum"]))
        if "maximum" in schema and data > schema["maximum"]:
            errors.append("%s: %r больше максимума %r" % (path, data, schema["maximum"]))

    return errors


def _same_value(data: Any, target: Any) -> bool:
    """Совпадает ли значение с эталоном.

    Строки сравниваются нормализованно: «Вика» и «вика» — это один и тот же
    ответ, и терять на регистре балл незачем. Числа и булевы значения
    сравниваются строго, но `1` и `1.0` считаются одним и тем же — JSON не
    различает целое и дробное в такой степени, чтобы это стоило балла.
    """
    if isinstance(data, str) and isinstance(target, str):
        return normalize(data) == normalize(target)
    if isinstance(data, bool) or isinstance(target, bool):
        return data is target
    if isinstance(data, (int, float)) and isinstance(target, (int, float)):
        return data == target
    return data == target


def _in_values(data: Any, values: list) -> bool:
    return any(_same_value(data, v) for v in values)


def _type_ok(data: Any, t: str) -> bool:
    if t == "object":
        return isinstance(data, dict)
    if t == "array":
        return isinstance(data, list)
    if t == "string":
        return isinstance(data, str)
    if t == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if t == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if t == "boolean":
        return isinstance(data, bool)
    if t == "null":
        return data is None
    return True


def _type_name(data: Any) -> str:
    if data is None:
        return "null"
    if isinstance(data, bool):
        return "boolean"
    if isinstance(data, dict):
        return "object"
    if isinstance(data, list):
        return "array"
    if isinstance(data, int):
        return "integer"
    if isinstance(data, float):
        return "number"
    if isinstance(data, str):
        return "string"
    return type(data).__name__
