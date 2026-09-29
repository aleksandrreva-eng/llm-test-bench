"""Диалог редактора тест-кейсов (раздел 10.4 / 11.5.2 ТЗ).

Слева — дерево наборов и кейсов. Справа — форма редактирования.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..atomic_io import write_json_atomic
from ..checks import KNOWN_TYPES
from ..testsets import CASES_DIR, TestCase, TestSet
from ..importer import TestSetImporter


# Тип проверки → человекочитаемая подпись
_TYPE_LABELS: dict[str, str] = {
    "contains_any": "Содержит любое из (contains_any)",
    "contains_all": "Содержит все из (contains_all)",
    "not_contains": "Не содержит (not_contains)",
    "regex": "Регулярное выражение (regex)",
    "exact_match": "Точное совпадение (exact_match)",
    "json_schema": "JSON Schema",
    "tool_call": "Вызов инструмента (tool_call)",
    "llm_judge": "LLM-as-judge (этап 8)",
    "embedding": "Embedding similarity (этап 8)",
}


class TestEditorDialog(QDialog):
    """Окно редактирования тест-кейсов."""

    def __init__(self, test_sets: list[TestSet], parent: QWidget | None = None):
        super().__init__(parent)
        self._sets = {s.id: s for s in test_sets}
        self._importer = TestSetImporter(test_sets)
        self._current_case: TestCase | None = None
        self._current_set: TestSet | None = None
        self._dirty = False

        self.setWindowTitle("Редактор тестов")
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._populate_tree()
        self._set_form_enabled(False)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # --- левая панель: дерево -------------------------------------
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(260)
        self.tree.currentItemChanged.connect(self._on_tree_changed)
        left_lay.addWidget(self.tree)

        btn_row = QHBoxLayout()
        self.new_case_btn = QPushButton("+ Кейс")
        self.new_case_btn.setToolTip("Добавить новый кейс в текущий набор")
        self.new_case_btn.clicked.connect(self._new_case)
        btn_row.addWidget(self.new_case_btn)

        self.del_case_btn = QPushButton("− Удалить")
        self.del_case_btn.setToolTip("Удалить выбранный кейс")
        self.del_case_btn.clicked.connect(self._delete_case)
        btn_row.addWidget(self.del_case_btn)
        btn_row.addStretch(1)
        left_lay.addLayout(btn_row)

        splitter.addWidget(left)

        # --- правая панель: форма --------------------------------------
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)

        # Заголовок набора
        self.set_label = QLabel("Выберите кейс слева")
        self.set_label.setProperty("role", "label")
        right_lay.addWidget(self.set_label)

        # Основные поля
        form = QFormLayout()
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(10)

        self.id_edit = QLineEdit()
        self.id_edit.setReadOnly(True)
        self.id_edit.setPlaceholderText("авто")
        form.addRow("ID:", self.id_edit)

        self.name_edit = QLineEdit()
        form.addRow("Название:", self.name_edit)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("Промпт (строка или JSON-массив сообщений)")
        self.prompt_edit.setMaximumHeight(140)
        form.addRow("Промпт:", self.prompt_edit)

        self.type_combo = QComboBox()
        for tid in sorted(KNOWN_TYPES, key=lambda x: _TYPE_LABELS.get(x, x)):
            self.type_combo.addItem(_TYPE_LABELS.get(tid, tid), tid)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Тип проверки:", self.type_combo)

        # Поля expected — динамические
        self.expected_box = QGroupBox("Критерий проверки")
        exp_lay = QFormLayout(self.expected_box)
        exp_lay.setVerticalSpacing(6)

        self.exp_values_edit = QTextEdit()
        self.exp_values_edit.setPlaceholderText("Одно значение на строку")
        self.exp_values_edit.setMaximumHeight(80)
        exp_lay.addRow("Значения:", self.exp_values_edit)

        self.exp_pattern_edit = QLineEdit()
        self.exp_pattern_edit.setPlaceholderText("Шаблон регулярного выражения")
        exp_lay.addRow("Шаблон:", self.exp_pattern_edit)

        self.exp_flags_edit = QLineEdit()
        self.exp_flags_edit.setPlaceholderText("IGNORECASE, MULTILINE, ...")
        exp_lay.addRow("Флаги:", self.exp_flags_edit)

        self.exp_exact_edit = QLineEdit()
        self.exp_exact_edit.setPlaceholderText("Ожидаемое значение")
        exp_lay.addRow("Ожидается:", self.exp_exact_edit)

        self.exp_schema_edit = QTextEdit()
        self.exp_schema_edit.setPlaceholderText('{"type": "object", ...}')
        self.exp_schema_edit.setMaximumHeight(100)
        exp_lay.addRow("JSON Schema:", self.exp_schema_edit)

        self.exp_tool_edit = QTextEdit()
        self.exp_tool_edit.setPlaceholderText('{"name": "func", "arguments": {"key": "val"}}')
        self.exp_tool_edit.setMaximumHeight(100)
        exp_lay.addRow("Описание вызова:", self.exp_tool_edit)

        self.exp_disabled_label = QLabel("Этот тип проверки требует модель-судью (этап 8).")
        self.exp_disabled_label.setEnabled(False)
        exp_lay.addRow(self.exp_disabled_label)

        form.addRow(self.expected_box)
        right_lay.addLayout(form)

        # Параметры сэмплирования
        params_box = QGroupBox("Параметры сэмплирования")
        params_lay = QFormLayout(params_box)
        params_lay.setVerticalSpacing(6)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(2)
        self.temp_spin.setSpecialValueText("по умолчанию")
        params_lay.addRow("Temperature:", self.temp_spin)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(-1, 2147483647)
        self.seed_spin.setSpecialValueText("по умолчанию")
        params_lay.addRow("Seed:", self.seed_spin)

        self.maxtok_spin = QSpinBox()
        self.maxtok_spin.setRange(0, 131072)
        self.maxtok_spin.setSpecialValueText("по умолчанию")
        params_lay.addRow("Max tokens:", self.maxtok_spin)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 10.0)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setValue(1.0)
        params_lay.addRow("Вес:", self.weight_spin)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("tag1, tag2")
        params_lay.addRow("Теги:", self.tags_edit)

        self.generator_edit = QLineEdit()
        self.generator_edit.setReadOnly(True)
        self.generator_edit.setPlaceholderText("—")
        params_lay.addRow("Генератор:", self.generator_edit)

        right_lay.addWidget(params_box)

        # Кнопки формы
        act_row = QHBoxLayout()
        self.save_btn = QPushButton("💾 Сохранить")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save_case)
        act_row.addWidget(self.save_btn)

        self.dup_btn = QPushButton("📋 Дублировать")
        self.dup_btn.clicked.connect(self._duplicate_case)
        act_row.addWidget(self.dup_btn)

        self.revert_btn = QPushButton("↩ Отменить изменения")
        self.revert_btn.clicked.connect(self._revert_case)
        act_row.addWidget(self.revert_btn)
        act_row.addStretch(1)
        right_lay.addLayout(act_row)

        right_lay.addStretch(1)

        # Нижние кнопки диалога
        bottom = QHBoxLayout()
        bottom.addStretch(1)

        self.import_btn = QPushButton("📥 Импорт JSON")
        self.import_btn.clicked.connect(self._import_json)
        bottom.addWidget(self.import_btn)

        self.import_csv_btn = QPushButton("📥 Импорт CSV")
        self.import_csv_btn.clicked.connect(self._import_csv)
        bottom.addWidget(self.import_csv_btn)

        self.export_btn = QPushButton("📤 Экспорт JSON")
        self.export_btn.clicked.connect(self._export_json)
        bottom.addWidget(self.export_btn)

        self.export_csv_btn = QPushButton("📤 Экспорт CSV")
        self.export_csv_btn.clicked.connect(self._export_csv)
        bottom.addWidget(self.export_csv_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.close_btn)
        right_lay.addLayout(bottom)

        splitter.addWidget(right)
        splitter.setSizes([280, 620])

    # ------------------------------------------------------------------
    # Дерево
    # ------------------------------------------------------------------

    def _populate_tree(self) -> None:
        self.tree.clear()
        for sid in sorted(self._sets):
            tset = self._sets[sid]
            set_item = QTreeWidgetItem([tset.name or sid])
            set_item.setData(0, Qt.ItemDataRole.UserRole, ("set", sid))
            set_item.setToolTip(0, "%s · %d кейсов · v%s" % (sid, tset.cases_count, tset.version))
            for case in tset.cases:
                case_item = QTreeWidgetItem(["%s — %s" % (case.id, case.name or case.id)])
                case_item.setData(0, Qt.ItemDataRole.UserRole, ("case", sid, case.id))
                case_item.setToolTip(0, case.check_summary)
                if not case.ok:
                    case_item.setForeground(0, Qt.GlobalColor.red)
                set_item.addChild(case_item)
            self.tree.addTopLevelItem(set_item)
            if tset.cases:
                set_item.setExpanded(True)

    def _on_tree_changed(self, current: QTreeWidgetItem | None, _prev=None) -> None:
        if current is None:
            self._set_form_enabled(False)
            return
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        if kind == "set":
            self._set_form_enabled(False)
            sid = rest[0]
            tset = self._sets.get(sid)
            self.set_label.setText(
                "Набор: %s · %d кейсов · v%s" % (tset.name or sid, tset.cases_count, tset.version)
            )
            return
        if kind == "case":
            sid, cid = rest[0], rest[1]
            tset = self._sets.get(sid)
            case = next((c for c in (tset.cases if tset else []) if c.id == cid), None)
            if case and tset:
                self._current_set = tset
                self._current_case = case
                self._load_case(case)
                self._set_form_enabled(True)
                self.set_label.setText("Набор: %s · кейс %s" % (tset.name or sid, case.id))
                return
        self._set_form_enabled(False)

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (
            self.name_edit,
            self.prompt_edit,
            self.type_combo,
            self.exp_values_edit,
            self.exp_pattern_edit,
            self.exp_flags_edit,
            self.exp_exact_edit,
            self.exp_schema_edit,
            self.exp_tool_edit,
            self.temp_spin,
            self.seed_spin,
            self.maxtok_spin,
            self.weight_spin,
            self.tags_edit,
            self.save_btn,
            self.dup_btn,
            self.revert_btn,
        ):
            w.setEnabled(enabled)
        self._show_expected_fields()

    # ------------------------------------------------------------------
    # Загрузка / отображение кейса
    # ------------------------------------------------------------------

    def _load_case(self, case: TestCase) -> None:
        self.id_edit.setText(case.id)
        self.name_edit.setText(case.name)

        if isinstance(case.prompt, list):
            self.prompt_edit.setPlainText(json.dumps(case.prompt, ensure_ascii=False, indent=2))
        else:
            self.prompt_edit.setPlainText(str(case.prompt or ""))

        ctype = str(case.expected.get("type") or "")
        idx = self.type_combo.findData(ctype)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)

        self._load_expected(case.expected)

        params = case.params or {}
        self.temp_spin.setValue(
            params.get("temperature", -1.0) if "temperature" in params else -1.0
        )
        self.seed_spin.setValue(params.get("seed", -1) if "seed" in params else -1)
        self.maxtok_spin.setValue(params.get("max_tokens", 0) if "max_tokens" in params else 0)
        self.weight_spin.setValue(case.weight)
        self.tags_edit.setText(", ".join(case.tags))
        self.generator_edit.setText(case.generator or "—")
        self._dirty = False

    def _load_expected(self, expected: dict) -> None:
        vals = expected.get("values") or expected.get("value") or expected.get("expected") or []
        if isinstance(vals, str):
            vals = [vals]
        self.exp_values_edit.setPlainText("\n".join(str(v) for v in vals))
        self.exp_pattern_edit.setText(expected.get("pattern") or expected.get("regex") or "")
        flags = expected.get("flags")
        self.exp_flags_edit.setText(
            ", ".join(str(f) for f in flags) if isinstance(flags, list) else str(flags or "")
        )
        exact = expected.get("value") or expected.get("expected") or expected.get("values")
        if isinstance(exact, list) and exact:
            exact = exact[0]
        self.exp_exact_edit.setText(str(exact or ""))
        schema = expected.get("schema") or expected.get("json_schema") or {}
        self.exp_schema_edit.setPlainText(
            json.dumps(schema, ensure_ascii=False, indent=2) if schema else ""
        )
        tool = expected.get("tool_call") or expected.get("call") or {}
        self.exp_tool_edit.setPlainText(
            json.dumps(tool, ensure_ascii=False, indent=2) if tool else ""
        )
        self._show_expected_fields()

    def _show_expected_fields(self) -> None:
        ctype = self.type_combo.currentData() or ""
        enabled = self.save_btn.isEnabled()

        # Сначала всё скрываем
        for w in (
            self.exp_values_edit,
            self.exp_pattern_edit,
            self.exp_flags_edit,
            self.exp_exact_edit,
            self.exp_schema_edit,
            self.exp_tool_edit,
            self.exp_disabled_label,
        ):
            w.setVisible(False)
            if hasattr(w, "setEnabled"):
                w.setEnabled(enabled)

        # Показываем нужное
        if ctype in ("contains_any", "contains_all", "not_contains"):
            self.exp_values_edit.setVisible(True)
        elif ctype == "regex":
            self.exp_pattern_edit.setVisible(True)
            self.exp_flags_edit.setVisible(True)
        elif ctype == "exact_match":
            self.exp_exact_edit.setVisible(True)
        elif ctype == "json_schema":
            self.exp_schema_edit.setVisible(True)
        elif ctype == "tool_call":
            self.exp_tool_edit.setVisible(True)
        elif ctype in ("llm_judge", "embedding"):
            self.exp_disabled_label.setVisible(True)

    def _on_type_changed(self, _idx: int) -> None:
        self._show_expected_fields()

    # ------------------------------------------------------------------
    # Сохранение
    # ------------------------------------------------------------------

    def _gather_case(self) -> dict:
        """Собрать кейс из полей формы в словарь."""
        prompt_text = self.prompt_edit.toPlainText().strip()
        # Попробуем распарсить как JSON (multi-turn)
        try:
            prompt = json.loads(prompt_text)
            if not isinstance(prompt, list):
                prompt = prompt_text
        except ValueError:
            prompt = prompt_text

        ctype = self.type_combo.currentData() or ""
        expected: dict[str, Any] = {"type": ctype}

        if ctype in ("contains_any", "contains_all", "not_contains"):
            lines = [
                ln.strip() for ln in self.exp_values_edit.toPlainText().splitlines() if ln.strip()
            ]
            expected["values"] = lines
        elif ctype == "regex":
            expected["pattern"] = self.exp_pattern_edit.text().strip()
            flags_text = self.exp_flags_edit.text().strip()
            if flags_text:
                expected["flags"] = [f.strip() for f in flags_text.split(",") if f.strip()]
        elif ctype == "exact_match":
            expected["value"] = self.exp_exact_edit.text().strip()
        elif ctype == "json_schema":
            schema_text = self.exp_schema_edit.toPlainText().strip()
            if schema_text:
                try:
                    expected["schema"] = json.loads(schema_text)
                except ValueError:
                    expected["schema"] = {}
            else:
                expected["schema"] = {}
        elif ctype == "tool_call":
            tool_text = self.exp_tool_edit.toPlainText().strip()
            if tool_text:
                try:
                    expected["tool_call"] = json.loads(tool_text)
                except ValueError:
                    expected["tool_call"] = {}
            else:
                expected["tool_call"] = {}

        params: dict[str, Any] = {}
        if self.temp_spin.value() >= 0:
            params["temperature"] = self.temp_spin.value()
        if self.seed_spin.value() >= 0:
            params["seed"] = self.seed_spin.value()
        if self.maxtok_spin.value() > 0:
            params["max_tokens"] = self.maxtok_spin.value()

        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]

        data = {
            "id": self.id_edit.text().strip() or "",
            "name": self.name_edit.text().strip(),
            "prompt": prompt,
            "expected": expected,
            "params": params,
            "tags": tags,
            "weight": self.weight_spin.value(),
        }
        # Сохраняем generator/target_tokens только если они были у оригинала
        if self._current_case and self._current_case.generator:
            data["generator"] = self._current_case.generator
            if self._current_case.target_tokens:
                data["target_tokens"] = self._current_case.target_tokens
        return data

    def _save_case(self) -> None:
        if self._current_set is None or self._current_case is None:
            return
        data = self._gather_case()
        case_id = data["id"]
        if not case_id:
            QMessageBox.warning(self, "Сохранение", "ID кейса не может быть пустым.")
            return

        # Определяем путь: один кейс = один файл
        cases_dir = Path(self._current_set.path) / CASES_DIR
        # Ищем текущий файл по id
        src_path: Path | None = None
        for f in sorted(cases_dir.glob("*.json")):
            try:
                contents = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(contents, dict) and contents.get("id") == self._current_case.id:
                    src_path = f
                    break
                if isinstance(contents, list):
                    for item in contents:
                        if isinstance(item, dict) and item.get("id") == self._current_case.id:
                            src_path = f
                            break
                    if src_path:
                        break
            except (OSError, ValueError):
                continue

        if src_path is None:
            # Создаём новый файл
            num = 1 + len(list(cases_dir.glob("*.json")))
            src_path = cases_dir / ("%03d_%s.json" % (num, case_id))

        # Перезаписываем файл
        try:
            write_json_atomic(src_path, data)
        except OSError as exc:
            QMessageBox.critical(self, "Ошибка", "Не удалось записать файл:\n%s" % exc)
            return

        # Обновляем объект в памяти
        idx = next(
            (i for i, c in enumerate(self._current_set.cases) if c.id == self._current_case.id),
            -1,
        )
        if idx >= 0:
            from ..testsets import parse_case

            new_case = parse_case(data, str(src_path))
            self._current_set.cases[idx] = new_case
            self._current_case = new_case
            self._reload_tree_item(case_id)
        self._dirty = False
        QMessageBox.information(self, "Сохранено", "Кейс %s сохранён." % case_id)

    def _reload_tree_item(self, case_id: str) -> None:
        """Обновить подпись в дереве после сохранения."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            set_item = root.child(i)
            for j in range(set_item.childCount()):
                case_item = set_item.child(j)
                data = case_item.data(0, Qt.ItemDataRole.UserRole)
                if data and len(data) >= 3 and data[2] == case_id:
                    case = next((c for c in self._current_set.cases if c.id == case_id), None)
                    if case:
                        case_item.setText(0, "%s — %s" % (case.id, case.name or case.id))
                        case_item.setToolTip(0, case.check_summary)
                        if case.ok:
                            case_item.setForeground(0, Qt.GlobalColor.black)
                    break

    def _revert_case(self) -> None:
        if self._current_case:
            self._load_case(self._current_case)

    # ------------------------------------------------------------------
    # Новый / дублировать / удалить
    # ------------------------------------------------------------------

    def _new_case(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            return
        # Определяем набор
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        if kind == "case":
            sid = rest[0]
        elif kind == "set":
            sid = rest[0]
        else:
            return
        tset = self._sets.get(sid)
        if tset is None:
            return

        # Новый id
        nums = []
        for c in tset.cases:
            try:
                nums.append(int("".join(ch for ch in c.id if ch.isdigit())))
            except ValueError:
                pass
        next_num = max(nums, default=0) + 1
        new_id = "%s_%03d" % (tset.id, next_num)
        new_case = TestCase(
            id=new_id,
            name="Новый кейс",
            prompt="",
            expected={"type": "contains_any", "values": []},
            tags=[],
        )
        tset.cases.append(new_case)
        self._current_set = tset
        self._current_case = new_case

        # Добавляем в дерево
        set_item = None
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d and d[1] == sid:
                set_item = item
                break
        if set_item:
            case_item = QTreeWidgetItem(["%s — %s" % (new_case.id, new_case.name)])
            case_item.setData(0, Qt.ItemDataRole.UserRole, ("case", sid, new_id))
            set_item.addChild(case_item)
            self.tree.setCurrentItem(case_item)
            self._load_case(new_case)
            self._set_form_enabled(True)

    def _duplicate_case(self) -> None:
        if self._current_set is None or self._current_case is None:
            return
        orig = self._current_case
        nums = []
        for c in self._current_set.cases:
            try:
                nums.append(int("".join(ch for ch in c.id if ch.isdigit())))
            except ValueError:
                pass
        next_num = max(nums, default=0) + 1
        new_id = "%s_%03d" % (self._current_set.id, next_num)
        data = orig.as_dict()
        data["id"] = new_id
        data["name"] = (orig.name or "") + " (копия)"
        from ..testsets import parse_case

        new_case = parse_case(data)
        self._current_set.cases.append(new_case)
        self._current_case = new_case

        set_item = None
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d and d[1] == self._current_set.id:
                set_item = item
                break
        if set_item:
            case_item = QTreeWidgetItem(["%s — %s" % (new_case.id, new_case.name)])
            case_item.setData(0, Qt.ItemDataRole.UserRole, ("case", self._current_set.id, new_id))
            set_item.addChild(case_item)
            self.tree.setCurrentItem(case_item)
            self._load_case(new_case)
            self._set_form_enabled(True)

    def _delete_case(self) -> None:
        if self._current_set is None or self._current_case is None:
            return
        case_id = self._current_case.id
        reply = QMessageBox.question(
            self,
            "Удаление",
            "Удалить кейс «%s»?\nФайл на диске тоже будет удалён." % case_id,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Удалить файл
        cases_dir = Path(self._current_set.path) / CASES_DIR
        removed = False
        for f in sorted(cases_dir.glob("*.json")):
            try:
                contents = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(contents, dict) and contents.get("id") == case_id:
                    f.unlink()
                    removed = True
                    break
                if isinstance(contents, list):
                    new_list = [
                        item
                        for item in contents
                        if not (isinstance(item, dict) and item.get("id") == case_id)
                    ]
                    if len(new_list) < len(contents):
                        write_json_atomic(f, new_list)
                        removed = True
                        break
            except (OSError, ValueError):
                continue

        # Удалить из памяти и дерева
        self._current_set.cases = [c for c in self._current_set.cases if c.id != case_id]
        self._current_case = None
        self._populate_tree()
        self._set_form_enabled(False)
        self.set_label.setText(
            "Кейс удалён" if removed else "Кейс удалён из памяти (файл не найден)"
        )

    # ------------------------------------------------------------------
    # Импорт / экспорт
    # ------------------------------------------------------------------

    def _import_json(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            QMessageBox.warning(self, "Импорт", "Выберите набор слева.")
            return
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        sid = rest[0] if kind in ("case", "set") else None
        tset = self._sets.get(sid) if sid else None
        if tset is None:
            return

        path, _ = QFileDialog.getOpenFileName(self, "Импорт кейсов (JSON)", "", "JSON (*.json)")
        if not path:
            return

        imported, errors = self._importer.import_json(Path(path), tset.id)
        if errors:
            QMessageBox.warning(
                self,
                "Импорт JSON",
                "Импортировано: %d\nОшибки:\n• %s" % (imported, "\n• ".join(errors[:10])),
            )
        if imported:
            self._populate_tree()
            QMessageBox.information(self, "Импорт JSON", "Импортировано кейсов: %d" % imported)

    def _export_json(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            QMessageBox.warning(self, "Экспорт", "Выберите набор слева.")
            return
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        sid = rest[0] if kind in ("case", "set") else None
        tset = self._sets.get(sid) if sid else None
        if tset is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт набора (JSON)", "%s_cases.json" % sid, "JSON (*.json)"
        )
        if not path:
            return
        if self._importer.export_json(tset, Path(path)):
            QMessageBox.information(self, "Экспорт JSON", "Сохранено кейсов: %d" % len(tset.cases))
        else:
            QMessageBox.critical(self, "Ошибка экспорта", "Не удалось записать файл.")

    def _import_csv(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            QMessageBox.warning(self, "Импорт", "Выберите набор слева.")
            return
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        sid = rest[0] if kind in ("case", "set") else None
        tset = self._sets.get(sid) if sid else None
        if tset is None:
            return

        path, _ = QFileDialog.getOpenFileName(self, "Импорт кейсов (CSV)", "", "CSV (*.csv)")
        if not path:
            return

        imported, errors = self._importer.import_csv(Path(path), tset.id)
        if errors:
            QMessageBox.warning(
                self,
                "Импорт CSV",
                "Импортировано: %d\nОшибки:\n• %s" % (imported, "\n• ".join(errors[:10])),
            )
        if imported:
            self._populate_tree()
            QMessageBox.information(self, "Импорт CSV", "Импортировано кейсов: %d" % imported)

    def _export_csv(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            QMessageBox.warning(self, "Экспорт", "Выберите набор слева.")
            return
        kind, *rest = current.data(0, Qt.ItemDataRole.UserRole) or (None,)
        sid = rest[0] if kind in ("case", "set") else None
        tset = self._sets.get(sid) if sid else None
        if tset is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт набора (CSV)", "%s_cases.csv" % sid, "CSV (*.csv)"
        )
        if not path:
            return
        if self._importer.export_csv(tset, Path(path)):
            QMessageBox.information(self, "Экспорт CSV", "Сохранено кейсов: %d" % len(tset.cases))
        else:
            QMessageBox.critical(self, "Ошибка экспорта", "Не удалось записать файл.")
