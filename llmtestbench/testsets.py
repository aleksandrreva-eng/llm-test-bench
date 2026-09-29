from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import KNOWN_TYPES, describe_check

MANIFEST_NAME = "manifest.json"
CASES_DIR = "cases"


@dataclass
class TestCase:
    """Один тест-кейс."""

    id: str
    name: str = ""
    prompt: Any = ""
    expected: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    weight: float = 1.0
    # Для наборов, где промпт не хранится целиком, а генерируется по длине
    # (speed: 100/500/1000 токенов; context_long: иголка в стоге на 2K…32K).
    generator: str = ""
    target_tokens: int = 0
    # Всё остальное из JSON кейса — генераторы читают отсюда свои поля
    # (needle, question, depth и прочее), чтобы не плодить атрибуты на каждый
    # будущий вид кейса.
    extra: dict = field(default_factory=dict)
    source: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def is_multi_turn(self) -> bool:
        return isinstance(self.prompt, list)

    @property
    def check_summary(self) -> str:
        return describe_check(self.expected)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "expected": self.expected,
            "params": self.params,
            "tags": self.tags,
            "weight": self.weight,
            "generator": self.generator,
            "target_tokens": self.target_tokens,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> TestCase:
        errors = []
        if not isinstance(data, dict):
            return cls(id="unknown", errors=["данные не словарь"])

        case_id = str(data.get("id") or "".strip())
        if not case_id:
            errors.append("не указан id")

        prompt = data.get("prompt")
        generator = str(data.get("generator") or "".strip())
        target_tokens = int(data.get("target_tokens") or 0)

        if prompt is None and not generator:
            errors.append("не указан prompt")

        if isinstance(prompt, list):
            if not prompt:
                errors.append("пустой список сообщений")
        elif prompt is not None and not isinstance(prompt, str):
            errors.append("prompt должен быть строкой или списком сообщений")

        expected = data.get("expected") or {}
        if expected and not isinstance(expected, dict):
            errors.append("expected должен быть объектом")
            expected = {}
        elif expected:
            ctype = str(expected.get("type") or "".strip())
            if not ctype:
                errors.append("в expected не указан type")
            elif ctype not in KNOWN_TYPES:
                errors.append("неизвестный тип проверки «%s»" % ctype)

        weight = data.get("weight", 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            errors.append("weight не число")
            weight = 1.0

        known_keys = {
            "id",
            "name",
            "prompt",
            "expected",
            "params",
            "tags",
            "weight",
            "generator",
            "target_tokens",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}

        return cls(
            id=case_id,
            name=str(data.get("name") or case_id),
            prompt=prompt,
            expected=expected,
            params=dict(data.get("params") or {}),
            tags=list(data.get("tags", [])),
            weight=weight,
            generator=generator,
            target_tokens=target_tokens,
            extra=extra,
            source=source,
            errors=errors,
        )


@dataclass
class TestSet:
    """Набор тестовых кейсов."""

    id: str
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    metrics: list[str] = field(default_factory=list)
    scoring: Any = "none"
    default_params: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    generator: str = ""
    cases: list[TestCase] = field(default_factory=list)
    path: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def cases_count(self) -> int:
        return len(self.cases)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def bad_cases(self) -> list[TestCase]:
        return [c for c in self.cases if not c.ok]

    def filtered(
        self,
        tags: list[str] | None = None,
        limit: int = 0,
        *,
        only_valid: bool = True,
    ) -> list[TestCase]:
        """Кейсы набора с фильтром по тегам и лимитом (п. 3.2 ТЗ)."""
        wanted = {t.strip().lower() for t in (tags or []) if t.strip()}
        out = []
        for case in self.cases:
            if only_valid and not case.ok:
                continue
            if wanted:
                have = {t.lower() for t in case.tags}
                if not (wanted & have):
                    continue
            out.append(case)
        if limit and limit > 0:
            out = out[:limit]
        return out

    def all_tags(self) -> list[str]:
        seen: list[str] = []
        for case in self.cases:
            for tag in case.tags:
                if tag not in seen:
                    seen.append(tag)
        return sorted(seen)

    def as_dict(self, with_cases: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "metrics": self.metrics,
            "scoring": self.scoring,
            "default_params": self.default_params,
            "tags": self.tags,
            "generator": self.generator,
        }
        if with_cases:
            d["cases"] = [c.as_dict() for c in self.cases]
        return d

    @classmethod
    def from_dict(cls, data: dict, path: str = "") -> TestSet:
        """Создать набор из словаря."""
        cases = []
        if "cases" in data and isinstance(data["cases"], list):
            for c_data in data["cases"]:
                cases.append(TestCase.from_dict(c_data, path))

        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            metrics=list(data.get("metrics", [])),
            scoring=data.get("scoring", "none"),
            default_params=dict(data.get("default_params", {})),
            tags=list(data.get("tags", [])),
            generator=str(data.get("generator") or ""),
            cases=cases,
            path=path,
            errors=data.get("errors", []),
        )


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def parse_case(data: Any, source: str = "", fallback_id: str = "") -> TestCase:
    """Собрать кейс из словаря, отметив все проблемы валидации."""
    if not isinstance(data, dict):
        return TestCase(id=fallback_id, source=source, errors=["кейс не является JSON-объектом"])
    return TestCase.from_dict(data, source)


def _load_case_file(path: Path) -> tuple[list[TestCase], list[str]]:
    """Прочитать файл кейсов. Внутри может быть объект или массив."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], ["%s: %s" % (path.name, exc)]

    if isinstance(data, list):
        cases = [parse_case(item, str(path)) for item in data]
        return cases, []
    return [parse_case(data, str(path), fallback_id=path.stem)], []


def load_test_set(directory: str | Path) -> TestSet:
    """Загрузить набор из директории с manifest.json."""
    d = Path(directory)
    errors: list[str] = []
    manifest: dict = {}

    manifest_path = d / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
            else:
                errors.append("manifest.json должен быть объектом")
        except (OSError, ValueError) as exc:
            errors.append("manifest.json: %s" % exc)
    else:
        errors.append("нет manifest.json")

    set_id = str(manifest.get("id") or d.name).strip()
    tset = TestSet(
        id=set_id,
        name=str(manifest.get("name") or set_id),
        version=str(manifest.get("version") or "1.0.0"),
        description=str(manifest.get("description") or ""),
        metrics=_as_str_list(manifest.get("metrics")),
        scoring=manifest.get("scoring", "none"),
        default_params=dict(manifest.get("default_params", {})),
        tags=_as_str_list(manifest.get("tags")),
        generator=str(manifest.get("generator") or ""),
        path=str(d),
        errors=errors,
    )

    cases_dir = d / CASES_DIR
    if cases_dir.is_dir():
        try:
            files = sorted(cases_dir.glob("*.json"))
        except OSError:
            files = []
        for f in files:
            cases, file_errors = _load_case_file(f)
            tset.cases.extend(cases)
            tset.errors.extend(file_errors)
    elif tset.generator or manifest.get("cases_generated"):
        pass
    elif manifest:
        tset.errors.append("нет папки cases/ и не указан генератор кейсов")

    seen: dict[str, int] = {}
    for case in tset.cases:
        seen[case.id] = seen.get(case.id, 0) + 1
    for case_id, count in seen.items():
        if count > 1:
            tset.errors.append("id «%s» встречается %d раза" % (case_id, count))

    declared = manifest.get("cases_count")
    if isinstance(declared, int) and declared and declared != len(tset.cases):
        tset.errors.append(
            "в manifest указано cases_count=%d, а кейсов %d" % (declared, len(tset.cases))
        )
    return tset


def discover_test_sets(root: str | Path) -> list[TestSet]:
    """Найти все наборы в директории tests/."""
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[TestSet] = []
    try:
        entries = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        return []
    for entry in entries:
        if (entry / MANIFEST_NAME).is_file():
            out.append(load_test_set(entry))
    return out


def set_to_dict(tset: TestSet, *, with_cases: bool = False) -> dict:
    """Для сохранения в результаты прогона (п. 11.7: версия набора)."""
    return tset.as_dict(with_cases=with_cases)


def case_prompt_text(case: TestCase) -> str:
    """Промпт кейса одной строкой — для логов и отчётов."""
    if isinstance(case.prompt, list):
        parts = []
        for msg in case.prompt:
            if isinstance(msg, dict):
                parts.append("%s: %s" % (msg.get("role", "?"), msg.get("content", "")))
        return "\n".join(parts)
    return str(case.prompt or "")
