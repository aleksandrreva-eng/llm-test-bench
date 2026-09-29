"""Проверка связки «интерфейс ↔ управление сервером» (этап 6 ТЗ).

Окно поднимается в offscreen-режиме. Настоящий llama-server не запускаем:
сначала проверяем режим «использовать существующий сервер» на живом
сервере Инка (только чтение), потом полный цикл — на поддельном сервере
из devtools/.

Запуск:  .venv/Scripts/python.exe check_ui_server.py
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.ui import MainWindow  # noqa: E402
from llmtestbench.ui.main_window import classify_log_line  # noqa: E402
from llmtestbench.ui.theme import apply_theme  # noqa: E402

PY = sys.executable
FAKE = str(Path(__file__).resolve().parent / "devtools" / "fake_llama_server.py")
LIVE_PORT = 8080

ok_count = 0
fail_count = 0
notes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print("  ok   %s" % name)
    else:
        fail_count += 1
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pump(app: QApplication, seconds: float, until=None) -> bool:
    """Крутить цикл событий, пока не сработает условие или не выйдет время."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(until is not None and until())


def live_server_up() -> bool:
    try:
        import requests

        return requests.get("http://127.0.0.1:%d/health" % LIVE_PORT, timeout=2).status_code in (
            200,
            503,
        )
    except Exception:  # noqa: BLE001
        return False


def make_cfg(port: int, tmp: Path, **kw) -> AppConfig:
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        logs_dir=str(tmp / "logs"),
        results_dir=str(tmp / "results"),
        reports_dir=str(tmp / "reports"),
        tests_dir=str(Path(__file__).resolve().parent / "tests"),
        sessions_dir=str(tmp / "sessions"),
        server_host="127.0.0.1",
        server_port=port,
        health_check_timeout_sec=15,
        **kw,
    )
    # Привязка к временному файлу обязательна: менеджер сохраняет PID через
    # cfg.save(), и без неё проверки затёрли бы рабочий config.json проекта.
    cfg.bind(tmp / ("config_%d.json" % port))
    cfg.resolve_dirs()
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------


def test_log_classifier() -> None:
    print("\n1. Цветовая маркировка строк лога (п. 3.4.5)")
    cases = [
        ("main: server is listening on http://127.0.0.1:8080", "OK"),
        ("llama_model_loader: loaded meta data with 30 key-value pairs", "INFO"),
        ("E srv  send_error: processing error", "FAIL"),
        ("srv  update_slots: failed to process task", "FAIL"),
        ("W llama_init_from_generator: no tokens", "WARN"),
        ("", "INFO"),
    ]
    for line, expected in cases:
        got = classify_log_line(line)
        check(
            "%-52s → %s" % (line[:52] or "(пусто)", expected), got == expected, "получено %s" % got
        )


def test_window_builds(app: QApplication, tmp: Path) -> MainWindow:
    print("\n2. Окно собирается, разделы и кнопки на месте")
    cfg = make_cfg(LIVE_PORT, tmp)
    win = MainWindow(cfg)
    check("разделов в рельсе четыре", len(win.rail.sections()) == 4, str(win.rail.sections()))
    check("страниц в стеке четыре", win.pages.count() == 4, str(win.pages.count()))
    check(
        "разделы называются по макету",
        win.rail.sections() == ["test", "server", "history", "compare"],
        str(win.rail.sections()),
    )
    check("открыт раздел «Тестирование»", win.rail.current() == "test", win.rail.current())
    check("переключение раздела меняет страницу", _switch(win, "history"))
    win.rail.set_current("test")
    check("менеджер сервера создан", win.server is not None)
    check("кнопка «Запустить сервер» включена", win.server_panel.start_btn.isEnabled())
    check(
        "кнопка «Стоп» выключена (своего процесса нет)",
        win.server_panel.stop_btn.isEnabled() is False,
    )
    check(
        "состояние — остановлен",
        win.server.status.state.value == "stopped",
        win.server.status.state.value,
    )
    check(
        "стартовая очистка осиротевших не нашла чужих процессов",
        win.cfg.server_pid == 0,
        str(win.cfg.server_pid),
    )
    return win


def _switch(win: MainWindow, key: str) -> bool:
    """Переключить раздел рельса и проверить, что стек страниц за ним поехал."""
    win.rail.set_current(key)
    return win.pages.currentIndex() == win._page_index[key]


def test_external_mode_startup(app: QApplication, tmp: Path) -> None:
    print("\n2б. Окно поднимается в режиме внешнего сервера из конфига")
    cfg = make_cfg(LIVE_PORT, tmp, use_existing_server=True)
    win = MainWindow(cfg)
    panel = win.server_panel
    check("галочка отражает конфиг", panel.use_existing_cb.isChecked() is True)
    check(
        "подсказка говорит про внешний адрес",
        "127.0.0.1:%d" % LIVE_PORT in panel.autostart_hint.text(),
        panel.autostart_hint.text(),
    )
    check("кнопка «Стоп» выключена: свой процесс не запущен", panel.stop_btn.isEnabled() is False)
    check("кнопка «Запустить» доступна", panel.start_btn.isEnabled() is True)
    check(
        "состояние — остановлен",
        win.server.status.state.value == "stopped",
        win.server.status.state.value,
    )
    # Обратный случай: галочка снята — подсказка про свой процесс.
    cfg2 = make_cfg(free_port(), tmp, use_existing_server=False)
    win2 = MainWindow(cfg2)
    check(
        "со снятой галочкой подсказка про свой процесс",
        "снята галочка" in win2.server_panel.autostart_hint.text(),
        win2.server_panel.autostart_hint.text(),
    )


def test_type_checkboxes(tmp: Path) -> None:
    """У каждого набора с диска должна быть своя галочка в панели.

    Список типов тестирования раньше был зашит в модуле: новый набор
    прогонялся из консоли, но в окне его нельзя было отметить. Проверяем,
    что панель собирается по факту находки наборов.
    """
    print("\n2в. Типы тестирования: в панели есть все наборы с диска")
    from llmtestbench.testsets import discover_test_sets
    from llmtestbench.ui.test_settings_panel import TestSettingsPanel

    cfg = make_cfg(free_port(), tmp)
    cfg.default_test_types = []
    panel = TestSettingsPanel(cfg)
    sets = discover_test_sets(cfg.tests_dir)
    panel.set_test_sets(sets)

    check("наборы найдены", len(sets) >= 7, "найдено %d" % len(sets))
    missing = [s.id for s in sets if s.id not in panel._checks]
    check("у каждого набора есть галочка", not missing, "без галочки: %s" % ", ".join(missing))

    panel._select_all_types()
    total = sum(s.cases_count for s in sets)
    text = panel.cases_label.text()
    check("«Все» считает кейсы всех наборов", ("Кейсов: %d из %d" % (total, total)) in text, text)

    panel._clear_all_types()
    one = sets[0]
    panel._checks[one.id].setChecked(True)
    text = panel.cases_label.text()
    # Числитель — кейсы одного набора, знаменатель — все кейсы на диске:
    # «10 из 97» говорит и что отмечен один набор, и сколько всего есть.
    check(
        "один набор — кейсы только его",
        ("Кейсов: %d из %d" % (one.cases_count, total)) in text,
        text,
    )


def test_external_mode(app: QApplication, win: MainWindow) -> None:
    print("\n3. Режим «использовать существующий сервер» (живой сервер Инка)")
    if not live_server_up():
        notes.append("сервер на 8080 не отвечает — проверка внешнего режима пропущена")
        print("  --   сервер не отвечает, пропускаю")
        return
    panel = win.server_panel
    panel.use_existing_cb.setChecked(True)
    panel.host_edit.setText("127.0.0.1")
    panel.port_spin.setValue(LIVE_PORT)
    check(
        "подсказка сменилась на адрес",
        "127.0.0.1:%d" % LIVE_PORT in panel.autostart_hint.text(),
        panel.autostart_hint.text(),
    )

    win._start_server("")
    pump(app, 20, lambda: win._server_thread is None)
    check("состояние ready", win.server.status.state.value == "ready", win.server.status.error)
    check("свой процесс не поднят", win.server.is_running is False)
    check(
        "в логе есть «Сервер готов»",
        any("Сервер готов" in ln for ln in log_lines(win)),
        "нет строки",
    )
    check("кнопка «Стоп» осталась выключенной", panel.stop_btn.isEnabled() is False)
    check("живой сервер Инка не тронут", live_server_up(), "сервер пропал!")

    # Остановка в этом режиме — вежливый отказ, а не тишина.
    before = len(log_lines(win))
    win._stop_server()
    pump(app, 2)
    new = log_lines(win)[before:]
    check(
        "попытка «Стоп» объясняет, что останавливать нечего",
        any("поднят не этим приложением" in ln for ln in new),
        str(new[:2]),
    )
    check("процесс на 8080 всё ещё жив", live_server_up())


def test_full_lifecycle(app: QApplication, tmp: Path) -> MainWindow:
    print("\n4. Полный цикл через интерфейс — на поддельном сервере")
    port = free_port()
    cfg = make_cfg(port, tmp)
    win = MainWindow(cfg)
    panel = win.server_panel

    panel.use_existing_cb.setChecked(False)
    panel.host_edit.setText("127.0.0.1")
    panel.port_spin.setValue(port)
    panel.exe_edit.setText(PY)
    panel.cmd_edit.setPlainText("%s %s %d" % (_quote(PY), _quote(FAKE), port))

    check(
        "подсказка вернулась к «свой процесс»",
        "снята галочка" in panel.autostart_hint.text(),
        panel.autostart_hint.text(),
    )

    win._start_server(panel.cmd_edit.toPlainText())
    check("кнопки заблокированы на время запуска", panel.start_btn.isEnabled() is False)
    pump(app, 25, lambda: win._server_thread is None)

    check("состояние ready", win.server.status.state.value == "ready", win.server.status.error)
    check("процесс жив", win.server.is_running is True)
    check("pid записан в config.json", cfg.server_pid > 0, str(cfg.server_pid))
    check("кнопка «Стоп» включилась", panel.stop_btn.isEnabled() is True)
    check("кнопка «Запустить» разблокирована", panel.start_btn.isEnabled() is True)
    check(
        "в логе есть «Сервер готов»",
        any("Сервер готов" in ln for ln in log_lines(win)),
        "нет строки",
    )
    check(
        "во вкладку «Сервер» попали строки процесса",
        win.server_log.view.blockCount() > 3,
        "строк %d" % win.server_log.view.blockCount(),
    )
    check("состояние в статус-баре", "сервер:" in win.status_label.text(), win.status_label.text())

    log_before = win.server_log.view.blockCount()
    win._stop_server()
    pump(app, 20, lambda: win._server_thread is None)
    check("состояние stopped", win.server.status.state.value == "stopped", win.server.status.error)
    check("процесс остановлен", win.server.is_running is False)
    check("pid сброшен", cfg.server_pid == 0, str(cfg.server_pid))
    check("кнопка «Стоп» снова выключена", panel.stop_btn.isEnabled() is False)
    check(
        "в логе есть «Сервер остановлен»",
        any("Сервер остановлен" in ln for ln in log_lines(win)),
        "нет строки",
    )
    check("лог пополнился при остановке", win.server_log.view.blockCount() > log_before)
    check("порт освободился", not _busy(port))
    return win


def test_port_busy_state(app: QApplication, tmp: Path) -> None:
    print("\n5. Занятый порт: интерфейс получает ошибку, а не зависает")
    port = free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    holder.listen(128)
    try:
        cfg = make_cfg(port, tmp)
        win = MainWindow(cfg)
        panel = win.server_panel
        panel.use_existing_cb.setChecked(False)
        panel.port_spin.setValue(port)
        panel.exe_edit.setText(PY)
        panel.cmd_edit.setPlainText("%s %s %d" % (_quote(PY), _quote(FAKE), port))
        # Диалог «Порт занят» модальный: в offscreen-режиме нажимать некому,
        # и box.exec() повесил бы прогон. Подменяем его заглушкой и заодно
        # проверяем, что он вообще был предложен.
        offered: list[int] = []
        win._offer_port_actions = lambda: offered.append(1)

        win._start_server(panel.cmd_edit.toPlainText())
        pump(app, 15, lambda: win._server_thread is None)
        check(
            "состояние error",
            win.server.status.state.value == "error",
            win.server.status.state.value,
        )
        check(
            "причина — занятый порт", "занят" in win.server.status.error, win.server.status.error
        )
        check(
            "в логе сказано про порт",
            any("Порт занят" in ln for ln in log_lines(win)),
            "нет строки",
        )
        check("пользователю предложен диалог «Порт занят»", offered == [1], str(offered))
        check("кнопки вернулись в рабочее состояние", panel.start_btn.isEnabled() is True)
    finally:
        holder.close()


def test_orphan_cleanup(tmp: Path) -> None:
    print("\n6. Осиротевший процесс подчищается при старте окна (п. 4.6)")
    port = free_port()
    cfg = make_cfg(port, tmp)
    cfg.health_check_timeout_sec = 15
    from llmtestbench import server_manager as sm

    mgr = sm.ServerManager(cfg)
    mgr.start([PY, FAKE, str(port)], timeout=15)
    pid = cfg.server_pid
    check("процесс поднят", pid > 0)
    # Приложение «упало»: конфиг остался с pid, процесс жив.
    win = MainWindow(cfg)
    pump(QApplication.instance(), 1)
    check("окно нашло и убило осиротевший процесс", sm._pid_alive(pid) is False)
    check(
        "в логе есть строка про осиротевший процесс",
        any("осиротевший" in ln for ln in log_lines(win)),
        "нет строки",
    )
    check("pid вычищен из конфига", cfg.server_pid == 0)


# ---------------------------------------------------------------------------


def log_lines(win: MainWindow) -> list[str]:
    return win.server_log.view.toPlainText().splitlines()


def _quote(path: str) -> str:
    return '"%s"' % path if " " in path else path


def _busy(port: int) -> bool:
    from llmtestbench.server_manager import is_port_busy

    return is_port_busy("127.0.0.1", port)


def _fingerprint(path: Path) -> tuple:
    """Отпечаток файла: размер + время правки. Пусто — если файла нет."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


# ---------------------------------------------------------------------------
# тема: тёмная, светлая и «авто»


def test_theme(app: QApplication, tmp: Path) -> None:
    """Смена темы без перезапуска (пункт 2 плана).

    Проверяем не «код вызвался», а «цвет переменился»: тема — то, что видно
    глазами, и половина ошибок тут выглядит как рабочий код, оставляющий на
    экране цвета прошлой темы.
    """
    print("\n7. Тема: тёмная, светлая и «авто»")

    from llmtestbench.ui import icons, theme
    from llmtestbench.ui.card import Badge, StatusDot
    from llmtestbench.ui import case_card_dialog
    from llmtestbench.ui.test_settings_panel import _SetRow
    from llmtestbench.testsets import TestSet

    # --- палитры ---
    check(
        "в обеих палитрах одинаковый набор ключей",
        set(theme.DARK) == set(theme.LIGHT),
        "разница: %s" % sorted(set(theme.DARK) ^ set(theme.LIGHT)),
    )
    check("ключей ровно 24", len(theme.DARK) == 24, str(len(theme.DARK)))

    # Тёмные значения сняты с утверждённого макета и правке не подлежат.
    # Сверяем их списком: «улучшение» цвета прошло бы незамеченным, а
    # приложение перестало бы выглядеть как макет.
    approved_dark = {
        "BG": "#131519",
        "SURFACE": "#191c22",
        "SURFACE_2": "#1f232b",
        "SURFACE_3": "#262b34",
        "SURFACE_4": "#2d333d",
        "BORDER": "#2b313b",
        "BORDER_STRONG": "#3a4250",
        "TEXT": "#e8ecf2",
        "TEXT_2": "#a8b2c1",
        "TEXT_3": "#6f7a8c",
        "LABEL": "#8ab4f8",
        "ACCENT": "#3b82f6",
        "ACCENT_HI": "#5b9bff",
        "ACCENT_LO": "#2f6fd8",
        "ACCENT_DIM": "#1c3050",
        "OK": "#3fb950",
        "WARN": "#d29922",
        "FAIL": "#f85149",
        "INFO": "#58a6ff",
        "OK_DIM": "#1b3a22",
        "WARN_DIM": "#3a2f14",
        "FAIL_DIM": "#3a2124",
        "CONSOLE_BG": "#0e1013",
        "DISABLED": "#4a5364",
    }
    changed = sorted(k for k, v in approved_dark.items() if theme.DARK.get(k) != v)
    check(
        "тёмная палитра совпадает с утверждённым макетом", not changed, "изменились: %s" % changed
    )

    # Контраст светлой темы: на белом серый и светлый янтарный читаются хуже,
    # чем те же роли на тёмном, и на глаз это не заметно — только замером.
    def _lum(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        parts = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def _ratio(fg: str, bg: str) -> float:
        a, b = _lum(fg), _lum(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    contrast_pairs = [
        ("основной текст", "TEXT", "SURFACE"),
        ("текст строк таблицы", "TEXT_2", "SURFACE"),
        ("подсказки", "TEXT_3", "SURFACE"),
        ("подсказки на второй поверхности", "TEXT_3", "SURFACE_2"),
        ("подписи параметров", "LABEL", "SURFACE"),
        ("пройдено", "OK", "OK_DIM"),
        ("частично", "WARN", "WARN_DIM"),
        ("провал", "FAIL", "FAIL_DIM"),
        ("метка «не гонялся»", "TEXT_3", "SURFACE_3"),
        ("выбранная строка", "TEXT", "ACCENT_DIM"),
        ("журнал: текст", "TEXT_2", "CONSOLE_BG"),
        ("журнал: пройдено", "OK", "CONSOLE_BG"),
        ("журнал: информация", "INFO", "CONSOLE_BG"),
        ("журнал: ошибка", "FAIL", "CONSOLE_BG"),
    ]
    worst = None
    for label, fg, bg in contrast_pairs:
        t = theme.LIGHT
        value = _ratio(t[fg], t[bg])
        if worst is None or value < worst[1]:
            worst = (label, value)
        check("светлая тема, контраст ≥ 4.5 — %s" % label, value >= 4.5, "%.2f" % value)
    check(
        "белый текст на акцентной кнопке читается",
        _ratio("#ffffff", theme.LIGHT["ACCENT"]) >= 4.5,
        "%.2f" % _ratio("#ffffff", theme.LIGHT["ACCENT"]),
    )
    check(
        "ни одна пара светлой темы не хуже 4.5",
        worst is not None and worst[1] >= 4.5,
        "худшая: %s — %.2f" % worst,
    )

    dark_sheet = theme.build_stylesheet(theme.PALETTES["dark"])
    light_sheet = theme.build_stylesheet(theme.PALETTES["light"])
    check(
        "стиль собирается для обеих тем",
        bool(dark_sheet) and bool(light_sheet),
        "%d и %d знаков" % (len(dark_sheet), len(light_sheet)),
    )
    check("темы дают разные таблицы стилей", dark_sheet != light_sheet)

    leftovers = [t for t in theme.DARK if "{%s}" % t in light_sheet]
    check("в шаблоне не осталось неподставленных токенов", not leftovers, str(leftovers[:5]))
    check(
        "светлая тема подставила свои поверхности",
        theme.LIGHT["SURFACE"] in light_sheet and theme.DARK["SURFACE"] not in light_sheet,
    )

    # --- применение ---
    apply_theme(app, "dark")
    check(
        "тёмная применена",
        theme.active_theme() == "dark" and theme.BG == theme.DARK["BG"],
        theme.BG,
    )
    check(
        "тёмный стиль ушёл в приложение",
        app.styleSheet() == theme.build_stylesheet(theme.PALETTES["dark"]),
    )

    apply_theme(app, "light")
    check(
        "светлая применена без перезапуска",
        theme.active_theme() == "light" and theme.BG == theme.LIGHT["BG"],
        theme.BG,
    )
    check(
        "светлый стиль ушёл в приложение",
        app.styleSheet() == theme.build_stylesheet(theme.PALETTES["light"]),
    )
    check(
        "палитра Qt тоже сменилась",
        app.palette().window().color().name().lower() == theme.LIGHT["BG"].lower(),
        app.palette().window().color().name(),
    )

    # --- «авто» ---
    from PySide6.QtCore import Qt

    resolved = theme.resolve("auto")
    check("«авто» разворачивается в известную тему", resolved in ("dark", "light"), resolved)
    scheme = app.styleHints().colorScheme()
    known = scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark)
    check(
        "«авто» без ответа системы даёт тёмную",
        known or resolved == "dark",
        "схема системы: %s" % scheme,
    )
    check(
        "неизвестное имя темы не ломает resolve", theme.resolve("нет-такой") in ("dark", "light")
    )

    # --- цвета, которые виджет ставит себе сам ---
    badge = Badge("13/15", "pass")
    badge.setParent(None)
    dot = StatusDot("ok")
    dot.setParent(None)
    row = _SetRow(TestSet(id="demo", path="demo.yaml"), True)
    row.setParent(None)

    apply_theme(app, "dark")
    dark_paints = (badge.styleSheet(), dot.styleSheet(), row.styleSheet())
    apply_theme(app, "light")
    light_paints = (badge.styleSheet(), dot.styleSheet(), row.styleSheet())
    for name, before, after in zip(
        ("метка счёта", "кружок состояния", "строка набора"),
        dark_paints,
        light_paints,
        strict=True,
    ):
        check("%s перекрашивается под новую тему" % name, before != after, "осталось: %s" % before)

    check(
        "у вердикта цвет берётся из общего стиля, а не из словаря",
        not hasattr(case_card_dialog, "_VERDICT_COLOR"),
    )
    check(
        "вердикт размечен свойством status",
        set(case_card_dialog._VERDICT_STATUS) == {"pass", "fail", "stand", "skip"},
        str(sorted(case_card_dialog._VERDICT_STATUS)),
    )

    # --- иконки: цвет берётся при отрисовке, а не при создании ---
    ic = icons.icon("clock", "TEXT_3", 16)
    apply_theme(app, "dark")
    dark_pm = ic.pixmap(16, 16).toImage()
    apply_theme(app, "light")
    light_pm = ic.pixmap(16, 16).toImage()
    check("иконка, созданная до смены темы, рисуется новым цветом", dark_pm != light_pm)

    # --- меню ---
    cfg = make_cfg(free_port(), tmp)
    win = MainWindow(cfg)
    check("в подменю «Тема» три пункта", len(win.theme_acts) == 3, str(sorted(win.theme_acts)))
    check(
        "пункты называются по-русски",
        [win.theme_acts[k].text() for k in theme.THEME_CHOICES]
        == [theme.THEME_LABELS[k] for k in theme.THEME_CHOICES],
        str([a.text() for a in win.theme_acts.values()]),
    )
    check("выбор взаимоисключающий", sum(1 for a in win.theme_acts.values() if a.isChecked()) == 1)

    # Нажимаем пункт меню, а не зовём обработчик: у пользователя только этот
    # путь, и он же отвечает за галочку.
    win.theme_acts["light"].trigger()
    check("выбор темы записан в конфиг", cfg.ui_theme == "light", cfg.ui_theme)
    check("выбор темы применён сразу", theme.active_theme() == "light", theme.active_theme())
    check("галочка перешла на светлую", win.theme_acts["light"].isChecked())
    check("галочка снялась с тёмной", not win.theme_acts["dark"].isChecked())

    win._set_theme("auto")
    check("«авто» записан в конфиг", cfg.ui_theme == "auto", cfg.ui_theme)
    check("«авто» подписался на схему системы", win._system_theme_watch is not None)
    check(
        "подписка снимается при явной теме",
        (win._set_theme("dark"), win._system_theme_watch is None)[1],
    )
    check("тёмная вернулась", theme.active_theme() == "dark", theme.active_theme())

    # Возвращаем исходное состояние: дальше проверки идут при тёмной теме.
    apply_theme(app, "dark")
    win.close()


def main() -> int:
    import tempfile

    print("=" * 72)
    print("Проверка связки «интерфейс ↔ управление сервером» (этап 6 ТЗ)")
    print("=" * 72)
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    tmp = Path(tempfile.mkdtemp(prefix="llmtestbench_ui_"))
    project_config = Path(__file__).resolve().parent / "config.json"
    config_before = _fingerprint(project_config)
    try:
        test_log_classifier()
        win = test_window_builds(app, tmp)
        test_type_checkboxes(tmp)
        test_external_mode_startup(app, tmp)
        test_external_mode(app, win)
        test_full_lifecycle(app, tmp)
        test_port_busy_state(app, tmp)
        test_orphan_cleanup(tmp)
        test_theme(app, tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print("\n8. Страж: рабочий config.json проекта")
    check(
        "config.json проекта не изменён прогоном проверок",
        _fingerprint(project_config) == config_before,
        "файл %s изменился" % project_config,
    )

    print("\n" + "=" * 72)
    print("Пройдено: %d   Провалено: %d" % (ok_count, fail_count))
    for n in notes:
        print("Примечание: %s" % n)
    print("=" * 72)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
