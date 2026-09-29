"""Проверка: прогон идёт по флажкам наборов, а не по выпадашке.

История вопроса. Каждый флажок на экране «Тестирование» — это отдельный
набор кейсов (chat_single, chat_multi, …). Одно время кнопка «Старт» гнала
набор, выбранный в выпадающем списке, и флажки не влияли ни на что: отметил
`chat_single`, нажал старт — ушёл `speed`. Проверка это и ловила.

Сейчас источник правды один — флажки. Выпадашка выбирает только набор для
кнопки «Редактор» и на прогон не влияет. Проверка ставит флажок на
`chat_single`, уводит выпадашку на `speed` и убеждается, что в результате
оказался именно `chat_single`.

Запуск:  .venv/Scripts/python.exe check_set_selection.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PY = sys.executable
PROJECT = Path(__file__).resolve().parent
FAKE = PROJECT / "devtools" / "fake_llama_server.py"

from PySide6.QtWidgets import QApplication  # noqa: E402

from llmtestbench.config import AppConfig  # noqa: E402

CHECKED = "chat_single"
DECOY = "speed"


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_with_checked_set(cfg, checked: str, decoy: str) -> str:
    """Отметить один набор, увести выпадашку на другой, нажать «Старт»."""
    from llmtestbench.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(cfg)
    win._load_test_sets()  # заполняет выпадашку наборов

    win.settings_panel._clear_all_types()
    win.settings_panel._checks[checked].setChecked(True)

    # Уводим выпадашку на другой набор: если прогон пойдёт по ней, это будет
    # видно по имени файла результата.
    idx = win.settings_panel.set_combo.findData(decoy)
    if idx >= 0:
        win.settings_panel.set_combo.setCurrentIndex(idx)

    win.run_log.clear()
    win.start_btn.click()

    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline:
        app.processEvents()
        if win._run_thread is None:
            break
        time.sleep(0.05)
    app.processEvents()

    return "\n".join(win.run_log.toPlainText().splitlines())


def main() -> int:
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="chk_setsel_"))
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        logs_dir=str(tmp / "logs"),
        results_dir=str(tmp / "results"),
        reports_dir=str(tmp / "reports"),
        tests_dir=str(PROJECT / "tests"),
        sessions_dir=str(tmp / "sessions"),
        server_host="127.0.0.1",
        server_port=port,
        health_check_timeout_sec=15,
        use_existing_server=False,
        default_test_types=[CHECKED],
    )
    cfg.bind(tmp / ("config_%d.json" % port))
    cfg.resolve_dirs()
    cfg.ensure_dirs()

    import subprocess

    proc = subprocess.Popen(
        [PY, str(FAKE), str(port), "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    import requests

    for _ in range(60):
        try:
            if requests.get("http://127.0.0.1:%d/health" % port, timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)

    try:
        log = run_with_checked_set(cfg, CHECKED, DECOY)
        print("=== ЖУРНАЛ ПРОГОНА (отмечен %s, выпадашка %s) ===" % (CHECKED, DECOY))
        print(log)

        # Смотрим на имя файла результата: в нём есть id набора.
        results = [ln for ln in log.splitlines() if ln.startswith("Результат:")]
        ran = results[-1] if results else ""
        if "_%s_" % CHECKED in ran:
            print("\nИТОГ: верно — прогнан отмеченный набор «%s»" % CHECKED)
            return 0
        if "_%s_" % DECOY in ran:
            print(
                "\nИТОГ: ОШИБКА — прогнан набор из выпадашки «%s», "
                "а не отмеченный «%s»" % (DECOY, CHECKED)
            )
            return 1
        print("\nИТОГ: не удалось определить, какой набор прогнан")
        return 2
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
