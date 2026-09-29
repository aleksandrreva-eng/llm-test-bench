"""Главное окно приложения (раздел 5 ТЗ).

Разделы: Тестирование · Сервер · История · Сравнение.

Про расхождение с ТЗ: п. 5.1 перечисляет три раздела (Тестирование, История,
Сравнение), но п. 3.4.5 требует отдельный лог сервера, а макет показывает
четыре пункта, включая «Сервер». Делаем четыре — так сходятся и макет, и
требование про лог.

Переключатель разделов — рельс слева, а не вкладки сверху. Вкладки забирали
ширину у таблиц и спорили за внимание с содержимым; рельс сворачивается до
60 px, когда ширина особенно нужна, и его состояние помнится в конфиге.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, server_cmd, server_manager
from ..server_log import read_memory_report
from ..config import AppConfig, bundle_root
from ..local_agent import LocalAgent, ServerUnavailable
from ..report import write_report
from ..runner import TestRunner
from ..testsets import discover_test_sets
from . import icons, theme
from .compare_tab import CompareTab
from .history_tab import HistoryTab
from .models_panel import ModelsPanel
from .nav_rail import NavRail
from .server_panel import ServerPanel, _ServerJob
from .server_tab import ServerLogTab
from .test_settings_panel import TestSettingsPanel


def _app_icon() -> QIcon:
    """Иконка окна. В dev — рядом с приложением, в сборке — из бандла
    (bundle_root), потому что `icon.ico` кладётся в `datas` спеки."""
    path = bundle_root() / "icon.ico"
    return QIcon(str(path))


def classify_log_line(line: str) -> str:
    """Уровень строки лога llama.cpp — для цветовой маркировки (п. 3.4.5).

    llama.cpp помечает уровни буквой в начале строки: `E` — ошибка,
    `W` — предупреждение, `I` — информация.
    """
    text = (line or "").strip()
    if not text:
        return "INFO"
    low = text.lower()
    if "error" in low or "failed" in low or "ошибк" in low:
        return "FAIL"
    if text.startswith(("E ", "E\t")) or ": error" in low:
        return "FAIL"
    if "warn" in low or "предупрежд" in low or text.startswith(("W ", "W\t")):
        return "WARN"
    if (
        "server is listening" in low
        or "model loaded" in low
        or "all slots are idle" in low
        or "starting the main loop" in low
    ):
        return "OK"
    return "INFO"


class _RunJob(QObject):
    """Прогон одного или нескольких наборов — в отдельном потоке.

    Один job = список наборов (как и в `run_tests.py --set a,b`): прогон идёт
    через тот же `TestRunner`, поэтому JSON и SQLite-записи складываются ровно
    так же. Флажки наборов на экране «Тестирование» — это отдельные наборы,
    поэтому «Запустить прогон» гонит все отмеченные подряд. `stop_flag`
    проверяется между кейсами (`should_stop` в `run_set`) — кнопка «Стоп»
    останавливает прогон на границе кейса, в середине запроса он не рвётся.
    """

    # case_id, done, total, одна строка лога
    case_done = Signal(str, int, int, str)
    # RunResult, исключение (None, если без ошибок), «последний ли это набор»
    finished = Signal(object, object, bool)

    def __init__(
        self,
        agent: LocalAgent,
        cfg,
        test_sets: list,
        runs: int,
        tags: list[str],
        limit: int,
        stop_flag: list,
        server_provider=None,
    ):
        super().__init__()
        self.agent = agent
        self.cfg = cfg
        self.test_sets = list(test_sets)
        self.runs = runs
        self.tags = tags
        self.limit = limit
        self.stop_flag = stop_flag
        # Чем поднят сервер. Считает окно: процессом управляет оно, а не job.
        # Нет источника — прогон запишет «неизвестно», и это тоже сведение.
        self.server_provider = server_provider
        # Совпадение с run_set: кейсы уже отфильтрованы по тегам и лимиту,
        # поэтому общий счётчик прогресса не «залипает» под 100% раньше времени.
        total = 0
        for t in self.test_sets:
            total += len(t.filtered(tags=self.tags, limit=self.limit))
        self._total = total * max(1, self.runs)
        self._done = 0

    def _memory_provider(self):
        """Память модели — из лога загрузки сервера.

        Лог пишет только `ServerManager`, и только когда сервер поднят из
        приложения. У сервера, поднятого лаунчером, своего лога у нас нет — и
        тогда отчёт честно скажет «данных нет», а не покажет нули.
        """
        return read_memory_report(self.cfg.logs_path / "server.log")

    def run(self) -> None:
        # Запас на рассуждение берём из конфига: в макете он вынесен в поле
        # «Параметры прогона», и прогон из окна обязан его учитывать — иначе
        # значение в поле ни на что не влияет.
        runner = TestRunner(
            self.agent,
            self.cfg,
            reasoning_allowance=int(getattr(self.cfg, "reasoning_allowance", 2048)),
            memory_provider=self._memory_provider,
            server_provider=self.server_provider,
        )
        try:
            for index, test_set in enumerate(self.test_sets):
                result = runner.run_set(
                    test_set,
                    runs=self.runs,
                    limit=self.limit,
                    tags=self.tags,
                    should_stop=lambda: bool(self.stop_flag[0]),
                    progress=self._progress,
                )
                runner.save(result)
                self.finished.emit(result, None, index == len(self.test_sets) - 1)
        except Exception as exc:  # noqa: BLE001 — поток не должен молчать
            self.finished.emit(None, exc, True)

    def _progress(self, case_result, done: int, total: int) -> None:
        self._done += 1
        self.case_done.emit(
            case_result.case_id,
            self._done,
            self._total,
            "  " + case_result.brief(),
        )


class MainWindow(QMainWindow):
    """Окно приложения."""

    # Менеджер процесса зовёт свои колбэки из рабочего потока, поэтому
    # наружу они выходят сигналами: Qt сам превратит их в отложенные
    # вызовы, и в виджеты мы попадём только из потока интерфейса.
    sig_server_state = Signal(object)
    sig_server_log = Signal(str)

    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self.cfg.ensure_dirs()

        self.setWindowTitle("LLM Test Bench")
        self.resize(1360, 860)
        self.setMinimumSize(1080, 660)
        self.setWindowIcon(_app_icon())

        self.server = server_manager.ServerManager(
            cfg,
            on_state=lambda st: self.sig_server_state.emit(st),
            on_log=lambda ln: self.sig_server_log.emit(ln),
        )
        self._server_thread: QThread | None = None
        self._server_job = None
        self._busy_dialog_shown = False

        self._run_thread: QThread | None = None
        self._run_job = None
        self._stop_flag: list[bool] = [False]
        # Последний выбор в списке моделей — для постоянных сегментов строки
        # состояния (адрес, модель, наборы).
        self._selected_models: list = []
        #: Подписка на тему системы — только пока выбрано «Авто».
        self._system_theme_watch = None

        self._build_menu()
        self._build_shell()
        self._build_testing_page()
        self._build_server_page()
        self._build_history_page()
        self._build_compare_page()
        self._build_statusbar()

        self._restore_geometry()
        self._wire()

        self.rail.set_current("test")
        self.rail.set_collapsed(bool(getattr(cfg, "ui_rail_collapsed", False)))

        self.log("Приложение запущено. Версия %s." % __version__)
        self._report_environment()
        self._load_test_sets()
        self._cleanup_orphans()
        self._update_rail_badges()
        if self.cfg.models_dir:
            self.models_panel.rescan()
        self._watch_system_theme(self.cfg.ui_theme == "auto")

    # ------------------------------------------------------------------
    # построение: каркас

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Файл")

        save_act = QAction("Сохранить настройки", self)
        save_act.setShortcut(QKeySequence.StandardKey.Save)
        save_act.triggered.connect(self.save_config)
        file_menu.addAction(save_act)

        open_results = QAction("Открыть папку результатов", self)
        open_results.triggered.connect(self._open_results)
        file_menu.addAction(open_results)

        file_menu.addSeparator()
        quit_act = QAction("Выход", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        tools_menu = self.menuBar().addMenu("Сервис")
        env_act = QAction("Проверить окружение", self)
        env_act.triggered.connect(self._report_environment)
        tools_menu.addAction(env_act)

        find_act = QAction("Найти llama-server.exe", self)
        find_act.triggered.connect(self._autodetect_server)
        tools_menu.addAction(find_act)

        tools_menu.addSeparator()
        rescan_act = QAction("Перечитать модели", self)
        rescan_act.setShortcut("F5")
        rescan_act.triggered.connect(lambda: self.models_panel.rescan())
        tools_menu.addAction(rescan_act)

        refresh_act = QAction("Перечитать историю", self)
        refresh_act.setShortcut("Ctrl+R")
        refresh_act.triggered.connect(self._refresh_history)
        tools_menu.addAction(refresh_act)

        view_menu = self.menuBar().addMenu("Вид")
        rail_act = QAction("Свернуть панель разделов", self)
        rail_act.setShortcut("Ctrl+B")
        rail_act.triggered.connect(self._toggle_rail)
        view_menu.addAction(rail_act)

        self.log_act = QAction("Журнал прогона", self)
        self.log_act.setCheckable(True)
        self.log_act.setChecked(True)
        self.log_act.triggered.connect(self._toggle_log)
        view_menu.addAction(self.log_act)

        view_menu.addSeparator()

        # Тема — подменю, а не два пункта рядом с «Журналом прогона»: те
        # пункты независимые, а эти взаимоисключающие, и в одном списке
        # читались бы как ещё два тумблера.
        theme_menu = view_menu.addMenu("Тема")
        self.theme_acts: dict[str, QAction] = {}
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        chosen = self.cfg.ui_theme if self.cfg.ui_theme in theme.THEME_CHOICES else "dark"
        for key in theme.THEME_CHOICES:
            act = QAction(theme.THEME_LABELS[key], self)
            act.setCheckable(True)
            act.setChecked(key == chosen)
            act.setToolTip(theme.THEME_HINTS[key])
            act.triggered.connect(lambda _checked=False, name=key: self._set_theme(name))
            self._theme_group.addAction(act)
            theme_menu.addAction(act)
            self.theme_acts[key] = act

        help_menu = self.menuBar().addMenu("Справка")
        about_act = QAction("О программе", self)
        about_act.triggered.connect(self._about)
        help_menu.addAction(about_act)

    def _build_shell(self) -> None:
        """Рельс разделов слева, стек страниц справа."""
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.rail = NavRail()
        self.rail.add_section("test", "Тестирование", "flask")
        self.rail.add_section("server", "Сервер", "server")
        self.rail.add_section("history", "История", "clock")
        self.rail.add_section("compare", "Сравнение", "chart")
        lay.addWidget(self.rail)

        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self._page_index: dict[str, int] = {}
        self._section_titles: dict[str, str] = {}

    def _add_page(self, key: str, page: QWidget, title: str) -> None:
        self._page_index[key] = self.pages.addWidget(page)
        self._section_titles[key] = title

    @staticmethod
    def _page_head(title: str, subtitle: str = "") -> QVBoxLayout:
        """Заголовок раздела: крупное имя и пояснение под ним."""
        head = QVBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        head.setSpacing(2)

        name = QLabel(title)
        name.setProperty("role", "pageTitle")
        head.addWidget(name)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setProperty("role", "pageSub")
            head.addWidget(sub)
        return head

    # ------------------------------------------------------------------
    # построение: страницы

    def _build_testing_page(self) -> None:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.addLayout(
            self._page_head(
                "Тестирование",
                "Модели слева, наборы в центре, параметры справа. Прогон — кнопкой внизу.",
            )
        )
        head.addStretch(1)

        rescan_btn = QPushButton("Обновить список")
        rescan_btn.setProperty("flat", True)
        rescan_btn.setProperty("size", "sm")
        rescan_btn.setIcon(icons.icon("refresh", "TEXT_3", 14))
        rescan_btn.setToolTip("Перечитать папку моделей (F5)")
        rescan_btn.clicked.connect(lambda: self.models_panel.rescan())
        head.addWidget(rescan_btn, 0, Qt.AlignTop)
        root.addLayout(head)

        # Три колонки: модели · наборы · параметры. Вложенный сплиттер внутри
        # панели настроек даёт третью колонку, поэтому внешний сплиттер
        # остаётся двухколоночным и не ломает соотношение ширин.
        cols = QSplitter(Qt.Horizontal)
        cols.setChildrenCollapsible(False)
        self.models_panel = ModelsPanel(self.cfg)
        self.models_panel.setMaximumWidth(430)
        self.settings_panel = TestSettingsPanel(self.cfg)
        cols.addWidget(self.models_panel)
        cols.addWidget(self.settings_panel)
        cols.setSizes([320, 860])
        cols.setStretchFactor(0, 0)
        cols.setStretchFactor(1, 1)
        root.addWidget(cols, 1)

        root.addWidget(self._build_run_box())
        self._add_page("test", page, "Тестирование")

    def _build_run_box(self) -> QFrame:
        """Полоса прогона: кнопки, прогресс, состояние и сворачиваемый журнал."""
        bar = QFrame()
        bar.setObjectName("runBar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)

        self.start_btn = QPushButton("Запустить прогон")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setIcon(icons.icon("play", "#ffffff", 14))
        self.start_btn.setMinimumHeight(theme.PRIMARY_H)
        self.start_btn.setToolTip(
            "Прогнать отмеченные наборы против поднятого сервера.\n"
            "Результат — в папке results, JSON, SQLite и HTML-отчёт."
        )
        self.start_btn.clicked.connect(self._on_start_clicked)
        top.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.setIcon(icons.icon("stop", "FAIL", 13))
        self.stop_btn.setMinimumHeight(theme.PRIMARY_H)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip(
            "Остановить прогон на границе следующего кейса.\n"
            "Текущий запрос не прерывается — дождитесь его конца."
        )
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        top.addWidget(self.stop_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setToolTip("Прогресс прогона")
        top.addWidget(self.progress, 1)

        self.run_stat = QLabel("готов к запуску")
        self.run_stat.setObjectName("runStat")
        top.addWidget(self.run_stat)

        self.log_toggle = QPushButton("Журнал")
        self.log_toggle.setProperty("flat", True)
        self.log_toggle.setProperty("size", "sm")
        self.log_toggle.setProperty("on", True)
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(True)
        self.log_toggle.setToolTip("Показать или скрыть журнал прогона")
        self.log_toggle.clicked.connect(self._toggle_log)
        top.addWidget(self.log_toggle)
        lay.addLayout(top)

        self.run_log = QPlainTextEdit()
        self.run_log.setObjectName("console")
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(5000)
        self.run_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        # В макете журнал — компактная полоса на две-три строки, а не полотно:
        # при 150 px полоса прогона занимала четверть окна и отбирала высоту у
        # списка моделей и наборов. Пяти строк хватает, чтобы видеть ход
        # прогона, а полностью он прячется кнопкой «Журнал».
        self.run_log.setMinimumHeight(58)
        self.run_log.setMaximumHeight(104)
        self.run_log.setPlaceholderText(
            "Здесь появится ход прогона: строки кейсов, итог и пути к отчётам."
        )
        self.run_log.setVisible(bool(getattr(self.cfg, "ui_run_log_open", True)))
        lay.addWidget(self.run_log)
        return bar

    def _build_server_page(self) -> None:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(
            self._page_head(
                "Сервер", "Состояние подключения, команда запуска и полный лог llama.cpp."
            )
        )

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.server_panel = ServerPanel(self.cfg)
        self.server_log = ServerLogTab(self.cfg)
        split.addWidget(self.server_panel)
        split.addWidget(self.server_log)
        split.setSizes([480, 720])
        root.addWidget(split, 1)
        self._add_page("server", page, "Сервер")
        self.rail.set_badge("server", "лог")

    def _build_history_page(self) -> None:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.addLayout(
            self._page_head(
                "История", "Каждый прогон лежит в JSON и в SQLite. Фильтры применяются сразу."
            )
        )
        head.addStretch(1)

        open_btn = QPushButton("Открыть папку результатов")
        open_btn.setProperty("flat", True)
        open_btn.setProperty("size", "sm")
        open_btn.setIcon(icons.icon("folder", "TEXT_3", 14))
        open_btn.clicked.connect(self._open_results)
        head.addWidget(open_btn, 0, Qt.AlignTop)
        root.addLayout(head)

        self.history_tab = HistoryTab(self.cfg)
        root.addWidget(self.history_tab, 1)
        self._add_page("history", page, "История")

    def _build_compare_page(self) -> None:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(
            self._page_head(
                "Сравнение", "Прогоны одной модели или разных — рядом, по общим метрикам."
            )
        )
        self.compare_tab = CompareTab(self.cfg)
        root.addWidget(self.compare_tab, 1)
        self._add_page("compare", page, "Сравнение")

    def _build_statusbar(self) -> None:
        # Слева — сообщение момента (состояние сервера, ход прогона), справа —
        # постоянный контекст, как в нижней полосе макета: адрес сервера,
        # выбранная модель и объём наборов видны с любого экрана, а не только
        # с «Сервера». Раньше строка состояния почти всегда была пустой.
        self.status_label = QLabel("готов")
        self.statusBar().addWidget(self.status_label, 1)

        self.sb_server = QLabel("")
        self.sb_model = QLabel("")
        self.sb_sets = QLabel("")
        for chip in (self.sb_server, self.sb_model, self.sb_sets):
            chip.setProperty("role", "hint")
            self.statusBar().addPermanentWidget(chip)

        self.statusBar().addPermanentWidget(QLabel("v%s" % __version__))
        self._refresh_status_chips()

    def _refresh_status_chips(self) -> None:
        """Пересобрать постоянные сегменты строки состояния.

        Значения берём из живых виджетов, а не из конфига: адрес и галочки
        правятся прямо в панели, и подпись не должна отставать от того, что
        видно на экране.
        """
        host = self.server_panel.host_edit.text().strip() or "127.0.0.1"
        self.sb_server.setText("%s:%d" % (host, self.server_panel.port_spin.value()))
        self.sb_server.setToolTip("Адрес, по которому приложение ищет llama-server")

        if self._selected_models:
            first = self._selected_models[0].file_name
            extra = (
                "  (+%d)" % (len(self._selected_models) - 1)
                if len(self._selected_models) > 1
                else ""
            )
            self.sb_model.setText("модель: %s%s" % (first, extra))
            self.sb_model.setToolTip("\n".join(m.file_name for m in self._selected_models))
        else:
            self.sb_model.setText("модель не выбрана")
            self.sb_model.setToolTip("Отметьте модель в списке на «Тестировании»")

        count, cases = self.settings_panel.sets_summary()
        self.sb_sets.setText("наборов %d · кейсов %d" % (count, cases))
        self.sb_sets.setToolTip("Столько наборов лежит в %s" % self.cfg.tests_path)

    def _wire(self) -> None:
        self.rail.current_changed.connect(self._on_section)

        self.models_panel.selection_changed.connect(self._on_selection_changed)
        self.models_panel.scan_finished.connect(self._on_scan_finished)
        self.settings_panel.settings_changed.connect(self._on_settings_changed)
        self.history_tab.compare_requested.connect(self._on_compare_requested)

        self.server_panel.set_manager(self.server)
        self.server_panel.start_requested.connect(self._start_server)
        self.server_panel.stop_requested.connect(self._stop_server)
        # Адрес правится прямо в панели — сегмент строки состояния должен
        # успевать за ним, иначе он показывает старый порт.
        self.server_panel.host_edit.textChanged.connect(lambda _: self._refresh_status_chips())
        self.server_panel.port_spin.valueChanged.connect(lambda _: self._refresh_status_chips())
        self.sig_server_state.connect(self._apply_server_state)
        self.sig_server_log.connect(self._apply_server_log)

    # ------------------------------------------------------------------
    # навигация

    def _on_section(self, key: str) -> None:
        index = self._page_index.get(key)
        if index is None:
            return
        self.pages.setCurrentIndex(index)
        if key == "history":
            # Пока шёл прогон, в results/ могли появиться новые файлы.
            self._refresh_history()
        self._sync_view_actions(key)

    def _sync_view_actions(self, key: str) -> None:
        """Пункты меню «Вид» показываем только там, где они что-то делают."""
        on_test = key == "test"
        self.log_act.setEnabled(on_test)
        self.log_act.setChecked(self.log_toggle.isChecked())

    def _toggle_rail(self) -> None:
        self.rail.toggle_collapsed()
        self.cfg.ui_rail_collapsed = self.rail.is_collapsed()

    def _toggle_log(self) -> None:
        """Свернуть журнал прогона.

        Раньше журнал занимал 96 px постоянно и отбирал их у списка моделей и
        наборов, хотя нужен он в двух случаях: когда прогон идёт и когда он
        сорвался. Теперь он сворачивается, а состояние помнится.
        """
        shown = (
            self.log_toggle.isChecked()
            if self.sender() is self.log_toggle
            else self.log_act.isChecked()
        )
        self.log_toggle.setChecked(shown)
        self.log_act.setChecked(shown)
        self.run_log.setVisible(shown)
        self.log_toggle.setProperty("on", shown)
        theme.restyle(self.log_toggle)
        self.cfg.ui_run_log_open = bool(shown)

    # ------------------------------------------------------------------
    # тема

    def _set_theme(self, name: str) -> None:
        """Переключить тему: применить сейчас и запомнить в конфиге.

        Применяем сразу, а не при следующем запуске: тема — то, что видно
        глазами, и «выбрал, но не изменилось» читается как поломка.
        """
        self.cfg.ui_theme = name
        self._apply_theme(name)
        self._watch_system_theme(name == "auto")
        # Галочку ставим здесь, а не полагаемся на нажатие: тему можно
        # переключить и не из меню, а галочка обязана показывать, что выбрано.
        act = self.theme_acts.get(name)
        if act is not None and not act.isChecked():
            act.setChecked(True)

    def _apply_theme(self, name: str) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, name)

    def _watch_system_theme(self, on: bool) -> None:
        """Подписаться на смену темы системы — только пока выбрано «Авто».

        Подписка, а не опрос по таймеру: система сообщает о смене сама, а
        опрос держал бы таймер живым всё время работы ради события, которое
        случается раз в день.
        """
        app = QApplication.instance()
        if app is None:
            return
        hints = app.styleHints()
        if on and self._system_theme_watch is None:
            self._system_theme_watch = hints.colorSchemeChanged.connect(
                self._on_system_theme_changed
            )
        elif not on and self._system_theme_watch is not None:
            hints.colorSchemeChanged.disconnect(self._on_system_theme_changed)
            self._system_theme_watch = None

    def _on_system_theme_changed(self, *_args) -> None:
        """Система сменила схему — перечитать тему, если выбрано «Авто».

        Проверка нужна: подписка снимается при выборе явной темы, но сигнал
        мог быть уже поставлен в очередь.
        """
        if self.cfg.ui_theme == "auto":
            self._apply_theme("auto")

    # ------------------------------------------------------------------
    # сервер: запуск и остановка

    def _cleanup_orphans(self) -> None:
        """Убить свой осиротевший процесс по PID из config.json (п. 4.6)."""
        killed = self.server.kill_orphans()
        if killed:
            self.log(
                "Найден и остановлен свой осиротевший llama-server (pid %s) — "
                "приложение было закрыто аварийно." % ", ".join(map(str, killed)),
                "WARN",
            )

    def _run_server_job(self, action: str, command: str = "", timeout=None) -> None:
        if self._server_thread is not None and self._server_thread.isRunning():
            self.log("Сервер уже занят другой операцией — подождите.", "WARN")
            return
        self.server_panel.set_busy(True)
        self._server_thread = QThread(self)
        self._server_job = _ServerJob(self.server, action, command, timeout)
        self._server_job.moveToThread(self._server_thread)
        self._server_thread.started.connect(self._server_job.run)
        self._server_job.done.connect(self._on_server_job_done)
        self._server_thread.start()

    def _start_server(self, command: str) -> None:
        self.server_panel.apply_to_config()
        if self.cfg.use_existing_server:
            self.log(
                "Проверяю внешний сервер %s:%d (свой не запускаю)."
                % (self.cfg.server_host, self.cfg.server_port)
            )
        else:
            self.log("Запускаю llama-server…")
        self.server_panel.set_status_text("запускается", "WARN")
        self._run_server_job("start", command, self.cfg.health_check_timeout_sec)

    def _stop_server(self) -> None:
        if self.cfg.use_existing_server and not self.server.is_running:
            self.log(
                "Останавливать нечего: сервер поднят не этим приложением. "
                "Если это лаунчер — выключите его там.",
                "WARN",
            )
            return
        self.log("Останавливаю сервер…")
        self.server_panel.set_status_text("останавливается", "WARN")
        self._run_server_job("stop")

    def _on_server_job_done(self, action: str, status) -> None:
        if self._server_thread is not None:
            self._server_thread.quit()
            self._server_thread.wait(3000)
            self._server_thread = None
            self._server_job = None
        self.server_panel.set_busy(False)
        self.server_panel.apply_status(status)

        if action == "start":
            if status.state.value == "ready":
                self.log(
                    "Сервер готов за %.1f сек%s."
                    % (
                        status.startup_time_ms / 1000.0,
                        ", pid %d" % status.pid if status.pid else "",
                    ),
                    "OK",
                )
            elif "занят" in (status.error or ""):
                self.log("Порт занят: %s" % status.error, "FAIL")
                self._offer_port_actions()
            else:
                self.log("Сервер не поднялся: %s" % (status.error or "без причины"), "FAIL")
        else:
            self.log(
                "Сервер остановлен за %.2f сек." % (status.shutdown_time_ms / 1000.0),
                "OK",
            )

    def _offer_port_actions(self) -> None:
        """Диалог «Порт занят» — п. 4.6 ТЗ: освободить / выбрать другой / отмена.

        Важная оговорка. «Освободить» умеет убирать только НАШ осиротевший
        процесс (по PID из config.json). Чужой сервер — например, поднятый
        лаунчером Инка, — приложение не трогает: выдёргивать чужую модель
        нельзя. Если порт держит кто-то другой, честнее предложить работать
        через него.
        """
        port = self.cfg.server_port
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Порт занят")
        box.setText("Порт %d занят — свой сервер туда не встанет." % port)
        box.setInformativeText(
            "«Освободить» остановит только процесс, запущенный этим "
            "приложением. Если порт держит чужой сервер (например, лаунчер), "
            "его нужно освободить там — либо включите галочку "
            "«Использовать существующий сервер»."
        )
        free_btn = box.addButton("Освободить", QMessageBox.AcceptRole)
        other_btn = box.addButton("Выбрать другой", QMessageBox.ActionRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is free_btn:
            killed = self.server.kill_orphans()
            if killed:
                self.log(
                    "Освободил порт: остановлен свой процесс pid %s."
                    % ", ".join(map(str, killed)),
                    "OK",
                )
            else:
                self.log(
                    "Своих процессов на порту нет — порт держит чужая "
                    "программа. Запускать второй сервер нельзя.",
                    "WARN",
                )
        elif box.clickedButton() is other_btn:
            self.server_panel.port_spin.setFocus()
            self.server_panel.port_spin.selectAll()
            self.log("Укажите другой порт в поле «Порт».", "INFO")

    def _apply_server_state(self, status) -> None:
        self.server_panel.apply_status(status)
        self.status_label.setText("сервер: %s" % status.state.ru)

    def _apply_server_log(self, line: str) -> None:
        """Строка из stdout/stderr сервера — на страницу «Сервер»."""
        self.server_log.append(line, classify_log_line(line))

    # ------------------------------------------------------------------
    # прогон из интерфейса: «Запустить прогон» / «Стоп»

    def _on_start_clicked(self) -> None:
        if self._run_thread is not None and self._run_thread.isRunning():
            return
        if self._server_thread is not None and self._server_thread.isRunning():
            self.log("Сервер занят другой операцией — дождитесь её конца.", "WARN")
            return

        values = self.settings_panel.values()
        # Выборка — по флажкам наборов (каждый набор = свой набор кейсов), а не
        # по одному полю в выпадашке: «Все» означает прогон нескольких наборов
        # подряд — так же, как `run_tests.py --set a,b`.
        test_sets: list = []
        for tid in self.settings_panel.selected_types():
            tset = self.settings_panel._sets.get(tid)
            if tset is None or not tset.ok:
                continue
            cases = tset.filtered(
                tags=self.settings_panel.tags(), limit=self.settings_panel.limit_spin.value()
            )
            if cases:
                test_sets.append(tset)
        if not test_sets:
            self.log(
                "Не отмечен ни один набор (или под фильтры не попал ни один "
                "кейс) — старт невозможен.",
                "FAIL",
            )
            self._set_run_stat("нечего запускать")
            return

        # Адрес берём из панели: пользователь мог поменять host:port в полях,
        # а не в конфиге.
        self.server_panel.apply_to_config()
        # Модель, которая реально загружена, берём с сервера, а не из списка
        # слева: левая — это выбор под команду запуска, а тесты идут по
        # уже поднятой модели.
        agent = LocalAgent(
            base_url=self.cfg.server_base_url,
            sessions_dir=self.cfg.sessions_path,
            timeout=float(getattr(self.cfg, "request_timeout_sec", 900)),
        )
        try:
            agent.health()
        except ServerUnavailable as exc:
            self.log(
                "Сервер не отвечает: %s. Поднимите его на странице «Сервер» "
                "или в лаунчере, затем нажмите «Запустить прогон»." % exc,
                "FAIL",
            )
            self._set_run_stat("сервер не отвечает")
            return

        # Занят ли сервер посторонним запросом. Спрашиваем до старта: у сборки
        # один слот, поэтому чужой запрос встаёт впереди наших, и замеры
        # скорости показывают скорость очереди, а не модели. Прогон не
        # запрещаем — решает человек, но молча пускать недостоверные замеры
        # нельзя.
        busy = agent.server_busy(samples=2, timeout=2.0)
        if busy and not self._confirm_busy_server(busy):
            self._set_run_stat("отменено — сервер занят")
            return

        self._stop_flag[0] = False
        self._set_running(True)
        self.run_log.clear()
        self.progress.setValue(0)
        self._set_run_stat("проверяю сервер…")
        model = agent.model_name
        self.log("Сервер: %s" % self.cfg.server_base_url, "OK")
        self.log("Модель: %s" % model, "OK")
        # Чем сервер поднят — в лог прогона. Скорость решают флаги запуска,
        # поэтому «неизвестно» здесь тоже полезно: оно объясняет, почему два
        # прогона могут разойтись, а по записи этого не видно.
        server_info = self._server_info()
        started_with = server_info["command"] or server_info["note"]
        self.log(f"Запуск сервера: {started_with}", "INFO")
        self.log(
            "Прогон: %d наборов, кейсов %d · прогонов %d · запас на рассуждение %d"
            % (
                len(test_sets),
                sum(len(t.cases) for t in test_sets),
                values["runs"],
                values["reasoning_allowance"],
            ),
            "INFO",
        )
        for t in test_sets:
            self.log("  • %s — %d кейсов" % (t.name or t.id, len(t.cases)), "INFO")

        self._run_job = _RunJob(
            agent,
            self.cfg,
            test_sets,
            values["runs"],
            self.settings_panel.tags(),
            self.settings_panel.limit_spin.value(),
            self._stop_flag,
            server_provider=self._server_info,
        )
        self._run_thread = QThread(self)
        self._run_job.moveToThread(self._run_thread)
        self._run_thread.started.connect(self._run_job.run)
        self._run_job.case_done.connect(self._on_case_done)
        self._run_job.finished.connect(self._on_run_finished)
        self._run_thread.start()

    def _server_info(self) -> dict:
        """Чем поднят сервер, против которого идёт прогон.

        Разница принципиальная. Если процесс подняло приложение, команда —
        факт: её видно в поле и она же ушла в `subprocess`. Если сервер
        поднят снаружи (лаунчер, консоль), приложение о его флагах не знает
        ничего, и записывать туда текст из поля нельзя: поле собирается под
        выбранную модель и к чужому процессу отношения не имеет. Врать за
        сервер хуже, чем признаться, что флагов нет.
        """
        status = getattr(self.server, "status", None)
        managed = bool(getattr(status, "pid", 0))
        command = str(getattr(status, "command", "") or "")
        return server_cmd.describe_server(command, applied=managed)

    def _confirm_busy_server(self, reason: str) -> bool:
        """Спросить, продолжать ли прогон на занятом сервере.

        Возвращает `True`, если человек решил продолжать. Причина называется
        прямо: без неё предупреждение читается как «что-то не так», и его
        пролистывают не читая.
        """
        answer = QMessageBox.warning(
            self,
            "Сервер занят посторонним запросом",
            f"На {self.cfg.server_base_url} {reason}.\n\n"
            "Замеры скорости будут недостоверны: у сборки один слот, и чужой "
            "запрос встаёт впереди наших. Цифры покажут скорость очереди, а не "
            "модели — первым врёт скорость префилла.\n\nПродолжить прогон?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.log(f"Сервер занят ({reason}) — прогон продолжен по решению человека.", "WARN")
            return True
        self.log(f"Прогон отменён: сервер занят ({reason}).", "WARN")
        return False

    def _on_stop_clicked(self) -> None:
        self._stop_flag[0] = True
        self.stop_btn.setEnabled(False)
        self._set_run_stat("останавливаюсь…")
        self.log("Остановка: дождёмся конца текущего кейса.", "WARN")

    def _set_run_stat(self, text: str) -> None:
        self.run_stat.setText(text)

    def _on_case_done(self, case_id: str, done: int, total: int, line: str) -> None:
        self.progress.setValue(int(round(done / total * 100.0)))
        self._set_run_stat("%d из %d" % (done, total))
        self.run_log.appendPlainText(line)

    def _on_run_finished(self, result, error, is_last: bool) -> None:
        if self._run_thread is not None:
            self._run_thread.quit()
            self._run_thread.wait(3000)
            self._run_thread = None
            self._run_job = None

        if error is not None:
            self.log("Прогон сорвался: %s" % error, "FAIL")
            self._set_running(False)
            self._set_run_stat("ошибка")
            self._after_run()
            return

        s = result.summary
        if result.status == "cancelled":
            self.log(
                "Прогон остановлен на кейсе «%s»."
                % (result.cases[-1].case_id if result.cases else "—"),
                "WARN",
            )
        elif result.status == "failed":
            self.log("Прогон остановлен: сервер перестал отвечать.", "FAIL")

        if result.status == "finished":
            score = s["score"]
            if score is not None:
                self.log(
                    "Итог: %d из %d (%.0f%%)" % (s["passed"], s["counted"], score * 100), "OK"
                )
            else:
                self.log("Итог: набор без проверок — смотри метрики в отчёте.", "OK")
        if s["failed"]:
            self.log("Провалено кейсов: %d" % s["failed"], "WARN")
        if s["stand_errors"]:
            self.log("Сбоев стенда: %d (в счёт не идут)" % s["stand_errors"], "WARN")
        self.log("Время: %.1f с" % result.seconds, "INFO")
        if result.path:
            self.log("Результат: %s" % result.path, "OK")

        # HTML-отчёт — сразу, как делает run_tests.py --report
        try:
            html_path = self.cfg.reports_path / ("%s.html" % result.run_id)
            write_report(
                [result.as_dict()],
                html_path,
                "Тестирование %s — %s" % (result.model, result.set_name or result.set_id),
            )
            self.log("Отчёт: %s" % html_path, "OK")
        except OSError as exc:
            self.log("Не удалось собрать HTML-отчёт: %s" % exc, "WARN")

        if is_last:
            self._set_running(False)
            self.progress.setValue(100)
            self._set_run_stat("готово")
            self.status_label.setText("прогон завершён: %s" % (result.set_name or result.set_id))
        else:
            self.status_label.setText(
                "готово «%s», продолжаю…" % (result.set_name or result.set_id)
            )
        self._after_run()

    def _after_run(self) -> None:
        """Обновить всё, что зависит от свежих результатов.

        История, счёт прошлого прогона в наборах и метка раздела — три места,
        которые без этого показывали бы состояние до прогона.
        """
        self.history_tab.refresh()
        self.settings_panel.refresh_scores()
        self._update_rail_badges()

    def _set_running(self, running: bool) -> None:
        """Кнопки прогона и прогресс по состоянию."""
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.server_panel.start_btn.setEnabled(not running)
        if not running:
            self._set_run_stat("готов к запуску")

    # ------------------------------------------------------------------
    # реакция на события

    def _on_selection_changed(self, models: list) -> None:
        self._selected_models = list(models)
        self._refresh_status_chips()
        if not models:
            self.server_panel.set_model(None)
            self.settings_panel.set_model("")
            self.status_label.setText("модель не выбрана")
            return
        self.server_panel.set_model(models[0])
        # Счёт прошлого прогона показывается для выбранной модели — иначе
        # метка в строке набора относится к другой модели и вводит в заблуждение.
        self.settings_panel.set_model(models[0].file_name)
        self.status_label.setText(
            "выбрано моделей: %d · первая: %s" % (len(models), models[0].file_name)
        )

    def _on_scan_finished(self, models: list) -> None:
        real = [m for m in models if m.is_model]
        sidecars = len(models) - len(real)
        self.log(
            "Сканирование завершено: моделей %d, сайдкаров %d." % (len(real), sidecars),
            "OK",
        )
        broken = [m for m in models if m.error]
        if broken:
            self.log(
                "Не удалось прочитать %d файл(ов): %s"
                % (len(broken), ", ".join(m.file_name for m in broken[:3])),
                "WARN",
            )

    def _on_settings_changed(self, values: dict) -> None:
        self.cfg.default_runs = values["runs"]
        self.cfg.default_test_types = values["test_types"]
        self.cfg.default_test_set = values["test_set"]
        self.cfg.last_case_limit = values["case_limit"]
        self.cfg.reasoning_allowance = values["reasoning_allowance"]
        self.cfg.request_timeout_sec = values["request_timeout_sec"]
        self._refresh_status_chips()

    def _on_compare_requested(self, paths: list) -> None:
        self.compare_tab.load(paths)
        self.rail.set_current("compare")

    # ------------------------------------------------------------------
    # сервис

    def log(self, message: str, level: str = "INFO") -> None:
        """Записать в лог сервера и в короткий журнал прогона."""
        self.server_log.append(message, level)
        self.run_log.appendPlainText(message)

    def _refresh_history(self) -> None:
        self.history_tab.refresh()
        self._update_rail_badges()

    def _update_rail_badges(self) -> None:
        """Метки в рельсе: сколько прогонов в истории и что известно о счёте.

        Считаются прогоны, а не файлы: на экране «История» строка — это прогон
        целиком, и число в рельсе должно совпадать с числом строк.
        """
        try:
            count = self.history_tab.total_groups()
        except Exception:  # noqa: BLE001 — метка не повод падать
            return
        self.rail.set_badge("history", str(count) if count else "")

    def _load_test_sets(self) -> None:
        """Прочитать наборы из tests/ и отдать их панели настроек."""
        sets = discover_test_sets(self.cfg.tests_path)
        self.settings_panel.set_test_sets(sets)
        self.settings_panel.refresh_scores()
        self._refresh_status_chips()
        if not sets:
            self.log(
                "Наборы тестов не найдены в %s — прогонять пока нечего." % self.cfg.tests_path,
                "WARN",
            )
            return
        total = sum(s.cases_count for s in sets)
        broken = [s for s in sets if not s.ok or s.bad_cases]
        self.log("Наборов тестов: %d, кейсов всего %d." % (len(sets), total), "OK")
        for s in broken:
            self.log(
                "Набор «%s»: %s" % (s.id, "; ".join(s.errors[:2]) or "есть проблемные кейсы"),
                "WARN",
            )

    def _report_environment(self) -> None:
        env = server_cmd.describe_environment()
        self.log("Python %s, платформа %s" % (env["python"], env["platform"]))
        if env["server_candidates"]:
            for c in env["server_candidates"][:5]:
                self.log("llama-server: %s — %s" % (c["label"], c["path"]), "OK")
        else:
            self.log(
                "llama-server.exe не найден. Укажите путь вручную в панели сервера.",
                "WARN",
            )

    def _autodetect_server(self) -> None:
        found = server_cmd.autodetect_server()
        if not found:
            self.log("llama-server.exe не найден ни в одном из известных мест.", "FAIL")
            QMessageBox.information(
                self,
                "Поиск llama-server",
                "Не нашёл llama-server.exe.\n\nПроверенные места:\n"
                "• папка приложения и llama.cpp рядом с ним\n"
                "• PATH\n"
                "• %LOCALAPPDATA%\\Programs\\llama.cpp\n"
                "• C:\\llama.cpp\\build\\bin\n"
                "• сборка лаунчера LlamaServerLauncherAvalonia\n"
                "• бэкенды LM Studio\n\n"
                "Укажите путь вручную кнопкой «Обзор…».",
            )
            return
        self.server_panel.exe_edit.setText(str(found))
        self.log("llama-server найден: %s" % found, "OK")

    def _open_results(self) -> None:
        import os
        import subprocess
        import sys

        path = self.cfg.results_path
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self.log("не удалось открыть папку результатов: %s" % exc, "ERROR")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "О программе",
            "<b>LLM Test Bench</b> v%s<br><br>"
            "Стенд тестирования локальных моделей (GGUF) на llama.cpp.<br>"
            "Реализованы этапы 1–7 ТЗ: интерфейс, сканирование моделей, "
            "метаданные GGUF, генерация команды запуска, управление процессом "
            "сервера с health-check, исполнитель тестов, сохранение JSON и "
            "HTML-отчёты.<br><br>"
            "Прямой режим (llama-cpp-python) отложен." % __version__,
        )

    # ------------------------------------------------------------------
    # сохранение состояния

    def save_config(self) -> None:
        self.settings_panel.apply_to_config()
        self.server_panel.apply_to_config()
        self.cfg.ui_rail_collapsed = self.rail.is_collapsed()
        # Состояние журнала берём у переключателя, а не у `isVisible()`:
        # save_config зовётся и из closeEvent, когда окно уже скрыто, и
        # видимость вернула бы False — журнал «сам» схлопнулся бы при
        # следующем запуске.
        self.cfg.ui_run_log_open = self.log_toggle.isChecked()
        self.cfg.window_geometry = self.saveGeometry().toBase64().data().decode("ascii")
        try:
            path = self.cfg.save()
            self.log("Настройки сохранены: %s" % path, "OK")
        except OSError as exc:
            self.log("не удалось сохранить настройки: %s" % exc, "ERROR")

    def _restore_geometry(self) -> None:
        if self.cfg.window_geometry:
            try:
                from PySide6.QtCore import QByteArray

                self.restoreGeometry(
                    QByteArray.fromBase64(self.cfg.window_geometry.encode("ascii"))
                )
            except Exception:  # noqa: BLE001 — битая геометрия не повод падать
                pass

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt-имя
        # Если свой процесс жив — прибираем за собой, иначе останется
        # висеть и займёт видеопамять (п. 4.6).
        try:
            if self.server.is_running:
                if self.cfg.auto_stop_server:
                    self.log("Закрытие: останавливаю свой сервер.")
                    self.server.stop()
                else:
                    self.log(
                        "Закрытие: сервер оставлен работать (auto_stop_server "
                        "выключен), pid %d." % self.server.status.pid,
                        "WARN",
                    )
        except Exception:  # noqa: BLE001 — закрытие не должно падать
            pass
        try:
            self.save_config()
        except Exception:  # noqa: BLE001
            pass
        event.accept()
