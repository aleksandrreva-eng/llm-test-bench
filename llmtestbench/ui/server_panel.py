"""Панель сервера (раздел 3.4 и 5.1 ТЗ).

Путь к llama-server.exe, host:port, редактируемая команда запуска, состояние
и кнопки «Запустить» / «Стоп».

Сам процесс здесь не поднимается: панель только собирает команду и просит
об этом окно. Причина — health-check может ждать до 60 секунд, а держать
интерфейс заблокированным нельзя.

Состояние вынесено в шапку карточки, кнопки — в подвал: раньше и то и другое
стояло в общем потоке полей, и «запущен ли сервер» приходилось выискивать
глазами между полем пути и полем команды.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .. import server_cmd
from . import icons, theme
from .card import Card, StatusDot, field


class _CheckWorker(QObject):
    """Проверка exe или подключения — в фоне, чтобы окно не замирало."""

    done = Signal(str, bool, str)  # что проверяли, успех, сообщение

    def __init__(self, kind: str, value: str):
        super().__init__()
        self.kind = kind
        self.value = value

    def run(self) -> None:
        if self.kind == "exe":
            ok, msg = server_cmd.validate_server_exe(self.value)
            self.done.emit("exe", ok, msg)
            return
        # kind == "conn": GET /v1/models
        try:
            import requests

            url = self.value.rstrip("/") + "/v1/models"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    ids = [m.get("id") for m in (data.get("data") or [])]
                except ValueError:
                    ids = []
                msg = "сервер отвечает"
                if ids:
                    msg = "сервер отвечает, модель: %s" % ", ".join(str(i) for i in ids[:3])
                self.done.emit("conn", True, msg)
            else:
                self.done.emit("conn", False, "HTTP %d" % resp.status_code)
        except Exception as exc:  # noqa: BLE001
            self.done.emit("conn", False, "нет связи: %s" % exc)


class _ServerJob(QObject):
    """Запуск или остановка процесса в отдельном потоке.

    `ServerManager.start()` ждёт health-check до 60 секунд, `stop()` — до
    пяти. И то и другое нельзя делать в потоке интерфейса.
    """

    done = Signal(str, object)  # действие, ServerStatus

    def __init__(self, manager, action: str, command: str = "", timeout=None):
        super().__init__()
        self.manager = manager
        self.action = action
        self.command = command
        self.timeout = timeout

    def run(self) -> None:
        if self.action == "start":
            status = self.manager.start(self.command, timeout=self.timeout)
        else:
            status = self.manager.stop()
        self.done.emit(self.action, status)


class ServerPanel(Card):
    """Настройки и состояние llama-server."""

    command_changed = Signal(str)
    start_requested = Signal(str)  # команда запуска
    stop_requested = Signal()

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__("Сервер", parent)
        self.cfg = cfg
        self._thread: QThread | None = None
        self._worker: _CheckWorker | None = None
        self._params = None
        self._manager = None
        self._busy = False

        # --- шапка: состояние ---
        self.state_dot = StatusDot("idle")
        self.state_label = QLabel("остановлен")
        self.state_label.setProperty("status", "FAIL")
        self.state_label.setStyleSheet("QLabel { background: transparent; font-weight: 600; }")
        head = QWidget()
        head_lay = QHBoxLayout(head)
        head_lay.setContentsMargins(0, 0, 0, 0)
        head_lay.setSpacing(6)
        head_lay.addWidget(self.state_dot)
        head_lay.addWidget(self.state_label)
        self.add_action(head)

        # --- путь к exe ---
        self.exe_edit = QLineEdit(cfg.llama_server_path)
        self.exe_edit.setPlaceholderText("llama-server.exe")
        # Курсор в начало: `setText` ставит его в конец, и длинный путь
        # показывается хвостом («...LauncherAvalonia\\llama.cpp\\llama-server.EXE»),
        # по которому не понять, что за файл выбран. Полный путь — в подсказке.
        self.exe_edit.setCursorPosition(0)
        self.exe_edit.textChanged.connect(lambda t: self._on_exe_changed(t))
        self.add_body_layout(field("Путь к llama-server.exe", self.exe_edit))

        exe_row = QHBoxLayout()
        exe_row.setSpacing(6)
        self.exe_browse_btn = QPushButton("Обзор…")
        self.exe_browse_btn.setProperty("size", "sm")
        self.exe_browse_btn.setIcon(icons.icon("folder", "TEXT_2", 13))
        self.exe_browse_btn.clicked.connect(self._browse_exe)
        exe_row.addWidget(self.exe_browse_btn)

        self.exe_check_btn = QPushButton("Проверить")
        self.exe_check_btn.setProperty("size", "sm")
        self.exe_check_btn.clicked.connect(self._check_exe)
        exe_row.addWidget(self.exe_check_btn)

        self.exe_status = QLabel("путь не проверен")
        self.exe_status.setProperty("role", "hint")
        self.exe_status.setWordWrap(True)
        exe_row.addWidget(self.exe_status, 1)
        self.add_body_layout(exe_row)

        # --- адрес ---
        addr = QWidget()
        addr_lay = QHBoxLayout(addr)
        addr_lay.setContentsMargins(0, 0, 0, 0)
        addr_lay.setSpacing(8)

        self.host_edit = QLineEdit(cfg.server_host)
        self.host_edit.setMaximumWidth(150)
        self.host_edit.textChanged.connect(lambda _: self.refresh_command())
        addr_lay.addLayout(field("Host", self.host_edit), 1)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(cfg.server_port))
        self.port_spin.setMaximumWidth(110)
        self.port_spin.valueChanged.connect(lambda _: self.refresh_command())
        addr_lay.addLayout(field("Порт", self.port_spin))

        self.conn_btn = QPushButton("Проверить подключение")
        self.conn_btn.setProperty("size", "sm")
        self.conn_btn.setToolTip("GET /v1/models по указанному адресу")
        self.conn_btn.clicked.connect(self._check_conn)
        addr_lay.addWidget(self.conn_btn, 0, Qt.AlignBottom)
        self.add_body(addr)

        self.use_existing_cb = QCheckBox("Использовать существующий сервер (не запускать свой)")
        self.use_existing_cb.setChecked(bool(cfg.use_existing_server))
        self.use_existing_cb.setToolTip(
            "Приложение не будет поднимать свой процесс — тесты пойдут\n"
            "по указанному адресу. Полезно, если сервер уже поднят лаунчером."
        )
        self.use_existing_cb.toggled.connect(self._on_use_existing_toggled)
        self.add_body(self.use_existing_cb)

        # --- команда ---
        self.cmd_edit = QPlainTextEdit()
        self.cmd_edit.setPlaceholderText(
            "Выберите модель в списке на экране «Тестирование» — команда соберётся автоматически"
        )
        self.cmd_edit.setMinimumHeight(78)
        self.cmd_edit.setMaximumHeight(140)
        self.cmd_edit.setObjectName("console")
        self.cmd_edit.textChanged.connect(
            lambda: self.command_changed.emit(self.cmd_edit.toPlainText())
        )
        self.add_body_layout(
            field(
                "Команда запуска",
                self.cmd_edit,
                "параметры берутся из метаданных GGUF и перекрываются полями выше",
            )
        )

        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(6)
        self.reset_cmd_btn = QPushButton("Сбросить к рекомендациям")
        self.reset_cmd_btn.setProperty("size", "sm")
        self.reset_cmd_btn.setIcon(icons.icon("refresh", "TEXT_2", 13))
        self.reset_cmd_btn.clicked.connect(self.refresh_command)
        cmd_row.addWidget(self.reset_cmd_btn)

        self.copy_cmd_btn = QPushButton("Копировать")
        self.copy_cmd_btn.setProperty("size", "sm")
        self.copy_cmd_btn.setIcon(icons.icon("copy", "TEXT_2", 13))
        self.copy_cmd_btn.clicked.connect(self._copy_command)
        cmd_row.addWidget(self.copy_cmd_btn)
        cmd_row.addStretch(1)
        self.add_body_layout(cmd_row)

        self.autostart_hint = QLabel(
            "Свой процесс поднимается только если снята галочка "
            "«Использовать существующий сервер»."
        )
        self.autostart_hint.setProperty("role", "hint")
        self.autostart_hint.setWordWrap(True)
        self.add_body(self.autostart_hint)

        # Растяжка в конце тела: карточка занимает всю высоту страницы, и без
        # неё лишние пиксели уходили в зазор между подсказкой поля «Команда
        # запуска» и кнопками «Сбросить/Копировать» — пустое место посреди
        # формы читается как незагрузившийся блок. Пусть пустота будет внизу.
        self.body_layout().addStretch(1)

        # --- подвал: управление ---
        self.start_btn = QPushButton("Запустить сервер")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setIcon(icons.icon("play", "#ffffff", 14))
        self.start_btn.setMinimumHeight(theme.PRIMARY_H)
        self.start_btn.setEnabled(False)
        self.start_btn.setToolTip(
            "Поднять llama-server с командой из поля выше и дождаться\n"
            "готовности по GET /health (до %d сек)." % int(cfg.health_check_timeout_sec)
        )
        self.start_btn.clicked.connect(self._on_start_clicked)
        self.add_footer(self.start_btn)

        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.setIcon(icons.icon("stop", "FAIL", 13))
        self.stop_btn.setMinimumHeight(theme.PRIMARY_H)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip(
            "Остановить процесс: вежливая попытка, затем terminate,\nчерез 5 секунд — kill."
        )
        self.stop_btn.clicked.connect(lambda: self.stop_requested.emit())
        self.add_footer(self.stop_btn)
        self.add_footer_stretch()

        # Автопоиск при первом запуске, если путь не задан
        if not self.exe_edit.text().strip():
            found = server_cmd.autodetect_server()
            if found:
                self.exe_edit.setText(str(found))
                self.exe_edit.setCursorPosition(0)
                self._set_exe_status("найден автоматически: %s" % found, "OK")

        # Галочка выставлена из конфига до того, как появилась подсказка,
        # и сигнал `toggled` поэтому не сработал. Синхронизируем текст вручную:
        # иначе при включённом режиме внешнего сервера подсказка рассказывает
        # про свой процесс.
        self._on_use_existing_toggled(self.use_existing_cb.isChecked())

    # ------------------------------------------------------------------
    # управление процессом

    def set_manager(self, manager) -> None:
        """Отдать панели менеджер процесса — кнопки включатся."""
        self._manager = manager
        self.start_btn.setEnabled(True)
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        """Кнопки по состоянию: занят — не нажимай.

        «Стоп» включается только тогда, когда процесс поднят этим
        приложением. Чужой сервер останавливать нечем и не нужно: кнопка,
        которая ничего не делает, хуже выключенной кнопки с подсказкой.
        """
        if self._manager is None:
            return
        running = self._manager.is_running
        self.start_btn.setEnabled(not self._busy)
        self.stop_btn.setEnabled(not self._busy and running)
        self.stop_btn.setToolTip(
            "Остановить процесс сервера (pid %d)." % self._manager.status.pid
            if running
            else "Останавливать нечего: свой процесс сервера не запущен."
        )

    def _on_start_clicked(self) -> None:
        command = self.cmd_edit.toPlainText().strip()
        if not command and not self.cfg.use_existing_server:
            self.set_status_text("команда пуста — выберите модель", "WARN")
            return
        self.start_requested.emit(command)

    def set_busy(self, busy: bool) -> None:
        """Пока идёт запуск или остановка — кнопки не трогаем."""
        self._busy = busy
        self.start_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(not busy)
        self.exe_browse_btn.setEnabled(not busy)
        self.reset_cmd_btn.setEnabled(not busy)

    def set_status_text(self, text: str, status: str = "") -> None:
        self.state_label.setText(text)
        self.state_label.setProperty("status", status)
        theme.restyle(self.state_label)
        self.state_dot.set_state(
            {
                "OK": "ok",
                "WARN": "warn",
                "FAIL": "fail",
            }.get(status, "idle")
        )

    def apply_status(self, status) -> None:
        """Показать состояние, пришедшее от менеджера."""
        marks = {
            "stopped": ("FAIL", "остановлен"),
            "starting": ("WARN", "запускается"),
            "ready": ("OK", "готов"),
            "testing": ("OK", "идёт тестирование"),
            "error": ("FAIL", "ошибка"),
        }
        kind, text = marks.get(status.state.value, ("FAIL", status.state.value))
        if status.state.value == "error" and status.error:
            text = "ошибка"
        self.set_status_text(text, kind)
        tip = []
        if status.pid:
            tip.append("pid %d" % status.pid)
        if status.startup_time_ms:
            tip.append("старт %.1f сек" % (status.startup_time_ms / 1000.0))
        if status.error:
            tip.append(status.error)
        elif status.health_message:
            tip.append(status.health_message)
        self.state_label.setToolTip("\n".join(tip))
        self.state_dot.setToolTip("\n".join(tip))
        self.refresh_buttons()

    # ------------------------------------------------------------------

    def _on_use_existing_toggled(self, checked: bool) -> None:
        self.cfg.use_existing_server = bool(checked)
        self.autostart_hint.setText(
            "Свой процесс не поднимается: тесты пойдут по адресу %s:%d."
            % (self.host_edit.text().strip(), self.port_spin.value())
            if checked
            else "Свой процесс поднимается только если снята галочка "
            "«Использовать существующий сервер»."
        )
        self.refresh_buttons()

    def _set_exe_status(self, text: str, status: str = "") -> None:
        self.exe_status.setText(text)
        self.exe_status.setProperty("status", status)
        # property меняется на лету — стилю нужно перечитать виджет
        theme.restyle(self.exe_status)

    def _on_exe_changed(self, text: str) -> None:
        self.cfg.llama_server_path = text
        self.exe_edit.setToolTip(text.strip() or "llama-server.exe не задан")
        if not text.strip():
            self._set_exe_status("путь не задан", "WARN")

    def _browse_exe(self) -> None:
        start = str(Path(self.exe_edit.text()).parent) if self.exe_edit.text() else ""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Выберите llama-server.exe", start, "Исполняемые файлы (*.exe);;Все файлы (*)"
        )
        if chosen:
            self.exe_edit.setText(chosen)
            self.exe_edit.setCursorPosition(0)

    def _run_worker(self, kind: str, value: str) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self._thread = QThread(self)
        self._worker = _CheckWorker(kind, value)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_check_done)
        self._thread.start()

    def _on_check_done(self, kind: str, ok: bool, msg: str) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
            self._worker = None
        if kind == "exe":
            self._set_exe_status(msg, "OK" if ok else "FAIL")
        else:
            self.set_status_text("подключён" if ok else "нет связи", "OK" if ok else "FAIL")
            self.state_label.setToolTip(msg)
            self.state_dot.setToolTip(msg)

    def _check_exe(self) -> None:
        path = self.exe_edit.text().strip()
        if not path:
            self._set_exe_status("путь не задан", "WARN")
            return
        self._set_exe_status("проверяю…")
        self._run_worker("exe", path)

    def _check_conn(self) -> None:
        url = "http://%s:%d" % (self.host_edit.text().strip(), self.port_spin.value())
        self.set_status_text("проверяю…")
        self._run_worker("conn", url)

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.cmd_edit.toPlainText())

    # ------------------------------------------------------------------

    def refresh_command(self) -> None:
        """Пересобрать команду для выбранной модели (п. 3.4.2 ТЗ)."""
        model = getattr(self, "_current_model", None)
        exe = self.exe_edit.text().strip() or server_cmd.EXE_NAME
        params = server_cmd.resolve_params(
            model,
            self.cfg,
            overrides={
                "host": self.host_edit.text().strip() or self.cfg.server_host,
                "port": self.port_spin.value(),
            },
        )
        self._params = params
        self.cmd_edit.setPlainText(server_cmd.build_command(exe, params))
        self.reset_cmd_btn.setToolTip(self._explain_params(params))

    @staticmethod
    def _explain_params(params) -> str:
        """Откуда взялись параметры — по п. 3.4.2 приоритет трёх источников."""
        names = {
            "n_ctx": "контекст",
            "temp": "temperature",
            "top_p": "top-p",
            "top_k": "top-k",
            "seed": "seed",
            "n_gpu_layers": "ngl",
        }
        ru = {
            "override": "задано вручную",
            "gguf": "из метаданных GGUF",
            "default": "по умолчанию llama.cpp",
        }
        lines = ["Источник параметров:"]
        for key, label in names.items():
            src = params.sources.get(key)
            if src:
                lines.append("  %s — %s" % (label, ru.get(src, src)))
        return "\n".join(lines)

    def set_model(self, model) -> None:
        """Сменить модель, под которую строится команда."""
        self._current_model = model
        self.refresh_command()

    def values(self) -> dict:
        return {
            "llama_server_path": self.exe_edit.text().strip(),
            "server_host": self.host_edit.text().strip() or "127.0.0.1",
            "server_port": self.port_spin.value(),
            "use_existing_server": self.use_existing_cb.isChecked(),
            "command": self.cmd_edit.toPlainText(),
        }

    def apply_to_config(self) -> None:
        v = self.values()
        self.cfg.llama_server_path = v["llama_server_path"]
        self.cfg.server_host = v["server_host"]
        self.cfg.server_port = v["server_port"]
        self.cfg.use_existing_server = v["use_existing_server"]
