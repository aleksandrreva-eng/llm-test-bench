from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from .atomic_io import write_csv_atomic, write_json_atomic
from .logging_setup import get_logger
from .testsets import CASES_DIR, TestCase, TestSet, parse_case

log = get_logger(__name__)


class TestSetImporter:
    """Загрузчик тестов из JSON или CSV."""

    def __init__(self, test_sets: List[TestSet]):
        self._sets = {s.id: s for s in test_sets}

    def import_json(self, file_path: Path, target_set_id: str) -> tuple[int, List[str]]:
        """Импортировать кейсы из JSON файла в указанный набор.

        На диск записываются только вновь добавленные кейсы — существующие
        файлы не перезаписываются, иначе при смене нумерации имён в папке
        cases/ возникали бы дубликаты.
        """
        if target_set_id not in self._sets:
            return 0, [f"Набор {target_set_id} не найден."]

        tset = self._sets[target_set_id]
        errors: List[str] = []
        imported: List[TestCase] = []

        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            cases_data = data if isinstance(data, list) else [data]

            for item in cases_data:
                if not isinstance(item, dict):
                    errors.append("Элемент в JSON не является объектом")
                    continue

                new_case = parse_case(item)
                if not new_case.id:
                    errors.append("кейс без id пропущен")
                    continue
                if any(c.id == new_case.id for c in tset.cases):
                    errors.append(f"ID «{new_case.id}» уже есть в наборе")
                    continue

                tset.cases.append(new_case)
                imported.append(new_case)

            self._write_new_cases(tset, imported)
        except Exception as e:
            errors.append(f"Ошибка при чтении JSON: {str(e)}")

        return len(imported), errors

    def import_csv(self, file_path: Path, target_set_id: str) -> tuple[int, List[str]]:
        """Импортировать кейсы из CSV файла в указанный набор.

        Поддерживаемые колонки (регистр и порядок не важны):
            id            — обязательно
            name          — название кейса
            prompt        — промпт (строка; если похож на JSON-массив — парсится как multi-turn)
            type          — тип проверки (contains_any, exact_match, regex, ...)
            expected      — критерий: для contains_* — значения через «|»; для exact_match — строка
            weight        — вес (по умолчанию 1.0)
            tags          — теги через запятую
        """
        if target_set_id not in self._sets:
            return 0, [f"Набор {target_set_id} не найден."]

        tset = self._sets[target_set_id]
        errors: List[str] = []
        imported: List[TestCase] = []

        import csv

        try:
            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return 0, ["Файл CSV пуст или без заголовка."]
                # Нормализуем имена колонок
                norm = {self._norm_key(h): h for h in reader.fieldnames}
                for row in reader:
                    case_id = (row.get(norm.get("id", ""), "") or "").strip()
                    if not case_id:
                        errors.append("пропущена строка без id")
                        continue
                    if any(c.id == case_id for c in tset.cases):
                        errors.append(f"ID «{case_id}» уже есть в наборе")
                        continue

                    prompt_raw = (row.get(norm.get("prompt", ""), "") or "").strip()
                    prompt: Any = prompt_raw
                    if prompt_raw.startswith("["):
                        try:
                            parsed = json.loads(prompt_raw)
                            if isinstance(parsed, list):
                                prompt = parsed
                        except ValueError:
                            pass

                    ctype = (row.get(norm.get("type", ""), "") or "").strip()
                    if not ctype:
                        ctype = "contains_any"

                    expected: dict = {"type": ctype}
                    exp_raw = (row.get(norm.get("expected", ""), "") or "").strip()
                    if ctype in ("contains_any", "contains_all", "not_contains"):
                        expected["values"] = [v.strip() for v in exp_raw.split("|") if v.strip()]
                    elif ctype == "exact_match":
                        expected["value"] = exp_raw
                    elif ctype == "regex":
                        expected["pattern"] = exp_raw

                    tags_raw = (row.get(norm.get("tags", ""), "") or "").strip()
                    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

                    weight_raw = (row.get(norm.get("weight", ""), "") or "").strip()
                    try:
                        weight = float(weight_raw) if weight_raw else 1.0
                    except ValueError:
                        weight = 1.0

                    data = {
                        "id": case_id,
                        "name": (row.get(norm.get("name", ""), "") or "").strip() or case_id,
                        "prompt": prompt,
                        "expected": expected,
                        "tags": tags,
                        "weight": weight,
                    }
                    new_case = parse_case(data)
                    tset.cases.append(new_case)
                    imported.append(new_case)

            self._write_new_cases(tset, imported)
        except Exception as e:
            errors.append(f"Ошибка при чтении CSV: {str(e)}")

        return len(imported), errors

    def _write_new_cases(self, tset: TestSet, imported: List[TestCase]) -> None:
        """Записать на диск только что импортированные кейсы."""
        if not imported:
            return
        cases_dir = Path(tset.path) / CASES_DIR
        cases_dir.mkdir(parents=True, exist_ok=True)
        existing = len(list(cases_dir.glob("*.json")))
        for idx, case in enumerate(imported, start=existing + 1):
            fpath = cases_dir / f"{idx:03d}_{case.id}.json"
            write_json_atomic(fpath, case.as_dict())

    @staticmethod
    def _norm_key(name: str) -> str:
        return name.strip().lower().replace("expected_type", "type")

    def export_json(self, test_set: TestSet, file_path: Path) -> bool:
        """Экспортировать набор в JSON.

        Возврат `False` вместо исключения оставлен ради вызывающего GUI, но
        причина теперь попадает в лог: молчаливый отказ невозможно отличить
        от «файл записан, но пустой».
        """
        try:
            write_json_atomic(file_path, test_set.as_dict(with_cases=True))
        except Exception as exc:
            log.warning("экспорт набора %s в JSON не удался: %s", test_set.id, exc)
            return False
        return True

    def export_csv(self, test_set: TestSet, file_path: Path) -> bool:
        """Экспортировать набор в CSV."""
        rows: List[list] = [["id", "name", "prompt", "expected_type", "weight"]]
        for case in test_set.cases:
            # Промпт может быть списком (multi-turn), в CSV он уходит строкой.
            rows.append(
                [
                    case.id,
                    case.name,
                    str(case.prompt),
                    str(case.expected.get("type", "")),
                    case.weight,
                ]
            )
        try:
            write_csv_atomic(file_path, rows)
        except Exception as exc:
            log.warning("экспорт набора %s в CSV не удался: %s", test_set.id, exc)
            return False
        return True


def migrate_json_to_db(cfg: Any) -> int:
    """Миграция старых JSON прогонов в SQLite."""
    from .database import DatabaseManager, DBRun, DBCaseResult

    db = DatabaseManager(cfg.db_path)
    added = 0

    # Проверяем все JSON в папке результатов
    results_dir = Path(cfg.results_path)
    if not results_dir.is_dir():
        return 0

    for json_file in results_dir.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Пропускаем, если это уже в базе (по run_id)
            if db.get_run_details(data["run_id"]) is not None:
                continue

            # Создаем объект прогона
            # Учитываем, что структура может слегка отличаться в зависимости от версии
            run_data = data.get("run", data)

            db_run = DBRun(
                run_id=data["run_id"],
                model=run_data.get("model", "unknown"),
                set_id=run_data.get("set_id", "unknown"),
                set_name=run_data.get("set_name", "unknown"),
                started=data.get("started"),
                finished=data.get("finished"),
                status=data.get("status", "finished"),
                total=data.get("summary", {}).get("total", 0),
                passed=data.get("summary", {}).get("passed", 0),
                failed=data.get("summary", {}).get("failed", 0),
                scored=data.get("summary", {}).get("score"),
                duration=data.get("seconds", 0),
                params=data.get("params", {}),
            )

            db_cases = []
            for c in data.get("cases", []):
                db_cases.append(
                    DBCaseResult(
                        run_id=data["run_id"],
                        case_id=c["id"],
                        name=c["name"],
                        prompt=str(c["prompt"]),
                        answer=c["answer"],
                        reasoning=c.get("reasoning", ""),
                        prompt_tokens=c.get("prompt_tokens", 0),
                        completion_tokens=c.get("completion_tokens", 0),
                        ttft_ms=c.get("ttft_ms", 0),
                        total_ms=c.get("total_ms", 0),
                        passed=(c.get("passed", False) if c.get("passed") is not None else False),
                        reason=c.get("reason", ""),
                        check_type=c.get("check_type", ""),
                        judge_score=c.get("judge_score"),
                        judge_response=c.get("judge_response"),
                        judge_error=c.get("judge_error"),
                        extra=c.get("extra", {}),
                    )
                )

            db.save_run(db_run, db_cases)
            added += 1
        except Exception as exc:
            log.warning("миграция %s не удалась: %s", json_file, exc)

    return added
