"""Панели «Наборы тестов» и «Параметры прогона» (раздел 3.2 и 11.5.1 ТЗ).

Главное отличие от прежней панели: у каждого набора видно **счёт последнего
прогона выбранной модели**. Раньше, чтобы понять, проходила ли эта модель
`chat_single` и с каким результатом, приходилось идти на вкладку «История» и
сопоставлять там строки вручную. Теперь счёт стоит в строке набора — и решение
«что гонять дальше» принимается на одном экране.

Источник счёта — JSON-прогоны из `results/` (`results_index`), а не база:
у перенесённых в базу прогонов счёт пустой, и метка врала бы «не гонялся» про
уже пройденный набор.

Флажок набора — настоящий `QCheckBox`: он лежит в `_checks[set_id]`, и по нему
работают и прогон, и проверки. Строка целиком кликабельна, но состояние
хранит именно флажок.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import results_index
from ..testsets import TestSet
from . import icons, theme
from .card import Badge, Card, field, score_badge
from .test_editor_dialog import TestEditorDialog

# id типа → подпись (п. 3.2 ТЗ). Список задаёт порядок знакомых наборов;
# наборы, которых тут нет, добавляются в конец по факту находки на диске —
# иначе новый набор молча не попадает в интерфейс.
TEST_TYPES: list[tuple[str, str]] = [
    ("chat_single", "Чат (single-turn)"),
    ("chat_multi", "Чат (multi-turn)"),
    ("agent_tools", "Агент (tools)"),
    ("speed", "Скорость (tokens/sec)"),
    ("quality", "Качество (golden set)"),
    ("context_long", "Контекст (длинный)"),
    ("function_calling", "Function calling"),
    ("reasoning", "Рассуждения (многошаговые)"),
    ("instruction_following", "Инструкции (жёсткие)"),
    ("robustness", "Устойчивость"),
    ("code", "Код"),
    ("russian", "Русский язык"),
]


class _SetRow(QFrame):
    """Строка набора: флажок, идентификатор, описание, счёт прошлого прогона."""

    def __init__(self, tset: TestSet, checked: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("setRow")
        self.setCursor(Qt.PointingHandCursor)
        self._on = False
        # Фон и рамку строки ставит `_apply_state` — там они зависят от того,
        # отмечен набор или нет. Цвета при этом берутся из темы в момент
        # вызова, поэтому после смены темы строку достаточно переспросить.

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(10)

        self.check = QCheckBox()
        self.check.setChecked(checked)
        self.check.setToolTip("Гонять этот набор")
        row.addWidget(self.check, 0, Qt.AlignTop)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(1)

        self.name = QLabel(tset.id)
        self.name.setProperty("role", "setName")
        main.addWidget(self.name)

        self.meta = QLabel(self._meta(tset))
        self.meta.setProperty("role", "setMeta")
        main.addWidget(self.meta)
        row.addLayout(main, 1)

        self.badge = Badge("не гонялся", "mute")
        row.addWidget(self.badge, 0, Qt.AlignTop)

        self._apply_state(checked)

    @staticmethod
    def _meta(tset: TestSet) -> str:
        parts = [
            "%d %s" % (tset.cases_count, theme.plural(tset.cases_count, "кейс", "кейса", "кейсов"))
        ]
        if tset.version:
            parts.append("v%s" % tset.version)
        if tset.errors:
            parts.append("замечания")
        return "  ·  ".join(parts)

    def _apply_state(self, on: bool) -> None:
        self._on = bool(on)
        bg = theme.ACCENT_DIM if self._on else "transparent"
        border = theme.ACCENT if self._on else "transparent"
        self.setStyleSheet(
            "#setRow { background: %s; border: 1px solid %s; border-radius: 4px; }" % (bg, border)
        )

    def restyle_theme(self) -> None:
        """Перечитать цвета строки: они стоят в её собственном стиле.

        Общий стиль сюда не достаёт — свой `setStyleSheet` сильнее стиля
        приложения, даже когда селектор по имени.
        """
        self._apply_state(self._on)

    def set_on(self, on: bool) -> None:
        self._apply_state(on)

    def set_score(self, rec) -> None:
        """Счёт последнего прогона. `None` — модель этот набор не проходила."""
        old = self.badge
        if rec is None:
            new = Badge("не гонялся", "mute")
        elif not rec.scored:
            new = Badge("метрики", "accent")
        else:
            new = score_badge(rec.passed, rec.counted)
            new.setToolTip(
                "Последний прогон: %d из %d пройдено · %s"
                % (rec.passed, rec.counted, rec.date_text)
            )
        layout = old.parentWidget().layout() if old.parentWidget() else None
        if layout is None:
            self.badge = new
            return
        layout.replaceWidget(old, new)
        old.deleteLater()
        self.badge = new

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt-имя
        # Клик по строке переключает флажок. По самому флажку клик сюда не
        # доходит — QCheckBox обрабатывает его сам, — поэтому дублирования нет.
        if event.button() == Qt.LeftButton:
            self.check.setChecked(not self.check.isChecked())
        super().mousePressEvent(event)


class TestSettingsPanel(QWidget):
    """Наборы слева, параметры справа — две карточки в одной панели."""

    settings_changed = Signal(dict)

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self._checks: dict[str, QCheckBox] = {}
        self._rows: dict[str, _SetRow] = {}
        self._sets: dict[str, TestSet] = {}
        self._tag_chips: dict[str, QPushButton] = {}
        self._runs: list = []
        self._model = ""
        self._only_failed = False
        self._order: list[str] = []

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(split)

        split.addWidget(self._build_sets_card())
        split.addWidget(self._build_params_card())
        # Наборы забирают всю лишнюю ширину, параметры держат свою: их поля
        # и так узкие, а строке набора нужен запас под имя и метку счёта.
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.params_card.setMinimumWidth(290)
        split.setSizes([560, 300])

    # ------------------------------------------------------------------
    # сборка

    def _build_sets_card(self) -> Card:
        card = Card("Наборы тестов")
        self.sets_card = card

        self.editor_btn = QPushButton("Редактор")
        self.editor_btn.setProperty("flat", True)
        self.editor_btn.setProperty("size", "sm")
        self.editor_btn.setIcon(icons.icon("grid", "TEXT_3", 14))
        self.editor_btn.setToolTip("Открыть редактор кейсов набора")
        self.editor_btn.clicked.connect(self._open_editor)

        self.failed_btn = QPushButton("Только упавшие")
        self.failed_btn.setProperty("chip", True)
        self.failed_btn.setCheckable(True)
        self.failed_btn.setToolTip(
            "Показать наборы, где прошлый прогон этой модели провалил кейсы"
        )
        self.failed_btn.clicked.connect(self._toggle_only_failed)

        card.add_action(self.failed_btn)
        card.add_action(self.editor_btn)

        # --- подзаголовок: сколько отмечено ---
        sub = QWidget()
        sub_lay = QHBoxLayout(sub)
        sub_lay.setContentsMargins(12, 6, 12, 6)
        sub_lay.setSpacing(8)

        self.cases_label = QLabel("—")
        self.cases_label.setObjectName("setCasesLabel")
        sub_lay.addWidget(self.cases_label)
        sub_lay.addStretch(1)
        card.add_body(sub)

        # --- список наборов ---
        self._rows_host = QWidget()
        self._rows_lay = QVBoxLayout(self._rows_host)
        self._rows_lay.setContentsMargins(6, 6, 6, 6)
        self._rows_lay.setSpacing(1)
        self._rows_lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._rows_host)
        card.add_body(scroll, 1)

        self.empty_label = QLabel("Наборы не найдены в папке tests/")
        self.empty_label.setProperty("role", "emptyText")
        self.empty_label.setAlignment(Qt.AlignCenter)
        card.add_body(self.empty_label)
        self.empty_label.setVisible(False)

        # Подвал: набор для редактора и кнопки отметки. Выпадашка живёт здесь,
        # а не в шапке: в шапке для неё не хватало ширины, и кнопки обрезались.
        editor_label = QLabel("Набор для редактора")
        editor_label.setProperty("role", "hint")
        card.add_footer(editor_label)

        self.set_combo = QComboBox()
        self.set_combo.setMinimumWidth(170)
        self.set_combo.setToolTip(
            "Какой набор открывает кнопка «Редактор».\n"
            "Прогон идёт строго по отмеченным наборам — выпадашка на него "
            "не влияет."
        )
        self.set_combo.currentIndexChanged.connect(self._on_set_changed)
        card.add_footer(self.set_combo)

        card.add_footer_stretch()
        self.all_btn = QPushButton("Все")
        self.all_btn.setProperty("flat", True)
        self.all_btn.setProperty("size", "sm")
        self.all_btn.clicked.connect(self._select_all_types)
        card.add_footer(self.all_btn)

        self.no_btn = QPushButton("Снять")
        self.no_btn.setProperty("flat", True)
        self.no_btn.setProperty("size", "sm")
        self.no_btn.clicked.connect(self._clear_all_types)
        card.add_footer(self.no_btn)
        return card

    def _build_params_card(self) -> Card:
        card = Card("Параметры прогона")
        self.params_card = card

        # --- прогонов на кейс ---
        self.runs_spin = QSpinBox()
        self.runs_spin.setRange(1, 10)
        self.runs_spin.setValue(max(1, int(self.cfg.default_runs)))
        card.add_body_layout(
            self._field("Прогонов на кейс", self.runs_spin, "усреднение по повторам")
        )

        # --- лимит кейсов ---
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 9999)
        self.limit_spin.setValue(int(self.cfg.last_case_limit))
        self.limit_spin.setSpecialValueText("все кейсы")
        card.add_body_layout(
            self._field(
                "Лимит кейсов на набор", self.limit_spin, "применяется к каждому набору отдельно"
            )
        )

        # --- фильтр по тегам ---
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("basic, format")
        self.tags_edit.setClearButtonEnabled(True)
        card.add_body_layout(
            self._field("Фильтр по тегам", self.tags_edit, "пусто — берутся все кейсы")
        )

        self.tags_host = QWidget()
        self.tags_lay = QHBoxLayout(self.tags_host)
        self.tags_lay.setContentsMargins(0, 0, 0, 0)
        self.tags_lay.setSpacing(6)
        card.add_body(self.tags_host)

        line = QFrame()
        line.setObjectName("hrLine")
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        card.add_body(line)

        # --- запас на рассуждение ---
        self.reasoning_spin = QSpinBox()
        self.reasoning_spin.setRange(0, 65536)
        self.reasoning_spin.setSingleStep(256)
        self.reasoning_spin.setValue(int(getattr(self.cfg, "reasoning_allowance", 2048)))
        card.add_body_layout(
            self._field(
                "Запас токенов на рассуждение",
                self.reasoning_spin,
                "у думающих моделей рассуждение идёт в тот же счётчик — "
                "тесный запас даёт пустой ответ",
            )
        )

        # --- таймаут ---
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 7200)
        self.timeout_spin.setSingleStep(30)
        self.timeout_spin.setSuffix(" с")
        self.timeout_spin.setValue(int(getattr(self.cfg, "request_timeout_sec", 900)))
        card.add_body_layout(
            self._field("Таймаут запроса", self.timeout_spin, "сколько ждать ответа на один кейс")
        )

        card.body_layout().addStretch(1)

        for spin in (self.runs_spin, self.limit_spin, self.reasoning_spin, self.timeout_spin):
            spin.valueChanged.connect(lambda _: self._emit())
        self.tags_edit.textChanged.connect(self._on_tags_typed)
        return card

    @staticmethod
    def _field(label: str, widget: QWidget, hint: str = "") -> QVBoxLayout:
        return field(label, widget, hint)

    # ------------------------------------------------------------------
    # наборы

    def set_test_sets(self, sets: list[TestSet]) -> None:
        """Заполнить список наборов прочитанными с диска."""
        self._sets = {s.id: s for s in sets}
        self._rebuild_type_checks(sets)
        current = self.set_combo.currentData() or self.cfg.default_test_set

        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        if not sets:
            self.set_combo.addItem("наборы не найдены", "")
        for tset in sets:
            mark = "" if tset.ok else "  ⚠"
            self.set_combo.addItem(
                "%s (%d)%s" % (tset.name or tset.id, tset.cases_count, mark),
                tset.id,
            )
        idx = self.set_combo.findData(current)
        if idx < 0 and sets:
            idx = 0
        if idx >= 0:
            self.set_combo.setCurrentIndex(idx)
        self.set_combo.blockSignals(False)

        total = sum(s.cases_count for s in sets)
        versions = {s.version for s in sets if s.version}
        parts = ["%d %s" % (total, theme.plural(total, "кейс", "кейса", "кейсов"))]
        if len(versions) == 1:
            parts.insert(0, "v%s" % versions.pop())
        self.sets_card.set_badge(" · ".join(parts))

        self._rebuild_tag_chips()
        self._rebuild_rows()
        self._update_case_count()

    def _rebuild_type_checks(self, sets: list[TestSet]) -> None:
        """Пересобрать флажки наборов по тому, что найдено на диске.

        Раньше список типов был зашит в модуле, и новый набор появлялся в
        прогоне, но не в интерфейсе: отметить его галочкой было нельзя.
        Теперь порядок задаёт `TEST_TYPES`, а всё незнакомое дописывается
        снизу. Отметки, которые пользователь уже расставил, сохраняются.
        """
        known = dict(TEST_TYPES)
        order: list[tuple[str, str]] = [
            (tid, label) for tid, label in TEST_TYPES if any(s.id == tid for s in sets)
        ]
        order += [
            (s.id, s.name or s.id) for s in sorted(sets, key=lambda x: x.id) if s.id not in known
        ]
        if not order:
            order = list(TEST_TYPES)

        keep = set(self.selected_types())
        first_build = not self._checks

        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._checks.clear()
        self._order = [tid for tid, _ in order]

        for tid, _label in order:
            tset = self._sets.get(tid)
            if tset is None:
                continue
            checked = (tid in self.cfg.default_test_types) if first_build else (tid in keep)
            row = _SetRow(tset, checked, self._rows_host)
            row.check.stateChanged.connect(self._on_row_toggled)
            self._rows[tid] = row
            self._checks[tid] = row.check

    def _rebuild_rows(self) -> None:
        """Переложить строки в список с учётом фильтра «только упавшие»."""
        # Разбираем список, но строки не удаляем: они переиспользуются,
        # иначе на каждом прогоне терялись бы отметки и подсказки.
        while self._rows_lay.count():
            self._rows_lay.takeAt(0)
        for row in self._rows.values():
            row.setVisible(False)

        scores = results_index.latest_by_set(self._runs, self._model)
        shown = 0
        for tid in self._order:
            row = self._rows.get(tid)
            if row is None:
                continue
            rec = scores.get(tid)
            row.set_score(rec)
            row.set_on(row.check.isChecked())
            if self._only_failed and not self._is_failed(rec):
                continue
            self._rows_lay.addWidget(row)
            row.setVisible(True)
            shown += 1
        self._rows_lay.addStretch(1)
        self.empty_label.setVisible(shown == 0 and bool(self._sets))

    @staticmethod
    def _is_failed(rec) -> bool:
        """Провал прошлого прогона: есть счёт и пройдено меньше всего."""
        return rec is not None and rec.scored and rec.passed < rec.counted

    def _toggle_only_failed(self) -> None:
        self._only_failed = self.failed_btn.isChecked()
        self.failed_btn.setProperty("on", self._only_failed)
        theme.restyle(self.failed_btn)
        self._rebuild_rows()

    def _on_row_toggled(self) -> None:
        sender = self.sender()
        for row in self._rows.values():
            if row.check is sender:
                row.set_on(sender.isChecked())
                break
        self._emit()
        self._update_case_count()

    def current_set(self) -> TestSet | None:
        return self._sets.get(self.set_combo.currentData() or "")

    def _on_set_changed(self) -> None:
        """Выбор в выпадашке меняет только цель редактора.

        Раньше здесь ещё и ставилась галочка на этот набор — «выбрал в
        списке, значит хочу прогнать». Отметка при этом не снималась с
        прежних, и прогон незаметно захватывал лишний набор. Теперь источник
        правды для прогона один: флажки.
        """
        self._update_case_count()
        self._emit()

    # ------------------------------------------------------------------
    # счёт прошлого прогона

    def set_model(self, model: str) -> None:
        """Сменить модель, для которой показывается счёт прошлых прогонов."""
        if results_index.model_key(model) == results_index.model_key(self._model):
            return
        self._model = model or ""
        self.refresh_scores()

    def refresh_scores(self) -> None:
        """Перечитать прогоны с диска и переставить метки в строках.

        Читаем только по событию (смена модели, конец прогона), а не на
        каждое движение: файлов в results/ может быть много, и разбор JSON
        в потоке интерфейса на каждый чих был бы заметен.
        """
        try:
            self._runs = results_index.load_runs(self.cfg)
        except OSError:
            self._runs = []
        self._rebuild_rows()

    def sets_summary(self) -> tuple[int, int]:
        """Сколько наборов найдено и сколько в них кейсов — для строки состояния.

        Считаем все наборы на диске, а не отмеченные: подпись отвечает на
        вопрос «что вообще есть», а не «что выбрано» — для второго в панели
        есть своя строка «Отмечено: N наборов».
        """
        return len(self._sets), sum(s.cases_count for s in self._sets.values())

    def scores_summary(self) -> str:
        """Строка «прошлый прогон: N наборов из M» — для статусной строки."""
        scores = results_index.latest_by_set(self._runs, self._model)
        scored = [r for r in scores.values() if r.scored]
        if not scored:
            return "по этой модели прогонов нет"
        ok = sum(1 for r in scored if r.passed == r.counted)
        return "наборов со счётом: %d, из них без провалов: %d" % (len(scored), ok)

    # ------------------------------------------------------------------
    # теги

    def _rebuild_tag_chips(self) -> None:
        """Чипы с тегами всех наборов — по клику подставляются в поле.

        Берём теги **всех** наборов, а не только отмеченных: набор тегов на
        экране не должен перестраиваться при каждой галочке, иначе чип, по
        которому собирались щёлкнуть, уезжает из-под курсора.
        """
        for chip in self._tag_chips.values():
            chip.setParent(None)
            chip.deleteLater()
        self._tag_chips.clear()

        tags: list[str] = []
        for tset in self._sets.values():
            for tag in tset.all_tags():
                if tag not in tags:
                    tags.append(tag)
        active = set(self.tags())
        for tag in sorted(tags)[:16]:
            chip = QPushButton(tag)
            chip.setProperty("chip", True)
            chip.setProperty("on", tag in active)
            chip.setCheckable(True)
            chip.setChecked(tag in active)
            chip.clicked.connect(lambda _=False, t=tag: self._toggle_tag(t))
            self._tag_chips[tag] = chip
            self.tags_lay.addWidget(chip)
        self.tags_lay.addStretch(1)

    def _toggle_tag(self, tag: str) -> None:
        current = self.tags()
        if tag in current:
            current.remove(tag)
        else:
            current.append(tag)
        self.tags_edit.setText(", ".join(current))

    def _on_tags_typed(self) -> None:
        """Ручной ввод в поле отражаем на чипах, но не пересобираем их."""
        active = set(self.tags())
        for tag, chip in self._tag_chips.items():
            on = tag in active
            if chip.isChecked() != on:
                chip.setChecked(on)
            if chip.property("on") != on:
                chip.setProperty("on", on)
                theme.restyle(chip)
        self._update_case_count()
        self._emit()

    # ------------------------------------------------------------------
    # счётчики

    def _update_case_count(self) -> None:
        """Показать «N из M» по всем отмеченным наборам.

        Флажки наборов — это отдельные наборы, поэтому кейсы суммируются по
        всем отмеченным с учётом тегов и лимита (п. 11.5.1 ТЗ). «Все» → все
        кейсы всех наборов, один набор → кейсы одного набора. Лимит
        применяется к каждому набору отдельно, как при прогоне.
        """
        picked_sets = [self._sets[tid] for tid in self.selected_types() if tid in self._sets]
        # Знаменатель — все кейсы всех наборов на диске, а не только
        # отмеченных: «70 из 70» вместо «70 из 97» скрывало бы, что часть
        # наборов не отмечена.
        cases_total = sum(s.cases_count for s in self._sets.values())
        if not picked_sets:
            self.cases_label.setText("Ни один набор не отмечен · всего кейсов: %d" % cases_total)
            self.cases_label.setToolTip("Отметьте наборы в списке выше.")
            return

        picked_total = 0
        for tset in picked_sets:
            picked_total += len(tset.filtered(tags=self.tags(), limit=self.limit_spin.value()))

        n = len(picked_sets)
        text = "Отмечено: %d %s · Кейсов: %d из %d" % (
            n,
            theme.plural(n, "набор", "набора", "наборов"),
            picked_total,
            cases_total,
        )
        self.cases_label.setText(text)

        names = [tset.name or tset.id for tset in picked_sets]
        tooltip = ["Наборы: %s" % ", ".join(names)]
        for tset in picked_sets:
            if tset.errors:
                tooltip.append("")
                tooltip.append("«%s»: %s" % (tset.name or tset.id, "; ".join(tset.errors[:3])))
        self.cases_label.setToolTip("\n".join(tooltip))

    # ------------------------------------------------------------------
    # прочее

    def _select_all_types(self) -> None:
        for cb in self._checks.values():
            cb.setChecked(True)

    def _clear_all_types(self) -> None:
        for cb in self._checks.values():
            cb.setChecked(False)

    def _emit(self) -> None:
        self.settings_changed.emit(self.values())

    def tags(self) -> list[str]:
        return [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]

    def selected_types(self) -> list[str]:
        return [tid for tid, cb in self._checks.items() if cb.isChecked()]

    def values(self) -> dict:
        tset = self.current_set()
        picked = [self._sets[tid] for tid in self.selected_types() if tid in self._sets]
        return {
            "runs": self.runs_spin.value(),
            "test_set": self.set_combo.currentData() or "",
            "test_set_version": tset.version if tset else "",
            "cases_selected": sum(
                len(t.filtered(tags=self.tags(), limit=self.limit_spin.value())) for t in picked
            ),
            "cases_total": sum(t.cases_count for t in picked),
            "test_types": self.selected_types(),
            "tags": self.tags(),
            "case_limit": self.limit_spin.value(),
            "reasoning_allowance": self.reasoning_spin.value(),
            "request_timeout_sec": self.timeout_spin.value(),
        }

    def _open_editor(self) -> None:
        sets = list(self._sets.values())
        if not sets:
            return
        dlg = TestEditorDialog(sets, parent=self)
        dlg.exec()
        # После закрытия перечитаем наборы, чтобы изменения отразились в UI
        self.set_test_sets(sets)

    def apply_to_config(self) -> None:
        """Записать текущие значения в конфиг (перед сохранением)."""
        self.cfg.default_runs = self.runs_spin.value()
        self.cfg.default_test_types = self.selected_types()
        self.cfg.default_test_set = self.set_combo.currentData() or ""
        self.cfg.last_case_limit = self.limit_spin.value()
        self.cfg.reasoning_allowance = self.reasoning_spin.value()
        self.cfg.request_timeout_sec = self.timeout_spin.value()


__all__ = ["TEST_TYPES", "TestSettingsPanel"]
