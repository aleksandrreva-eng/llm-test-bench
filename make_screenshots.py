"""Снимок окна для docs/ — на настоящей платформе, а не в offscreen.

В offscreen-режиме Qt не подхватывает системные шрифты, и текст выходит
квадратиками, поэтому снимок делается обычным плагином `windows`: окно
мелькнёт на экране на доли секунды, это нормально.

Разделы переключаются рельсом, поэтому снимок делается всего окна целиком —
на нём видно и навигацию, и содержимое раздела.

Запуск:  .venv/Scripts/python.exe make_screenshots.py
         .venv/Scripts/python.exe make_screenshots.py --theme light
         .venv/Scripts/python.exe make_screenshots.py --manual

Тема берётся из `config.json` (`ui_theme`); `--theme` её переопределяет. У
светлой темы к имени файла добавляется «-светлая», чтобы тёмные снимки
документации не затирались.

Режим `--manual` снимает не пять разделов, а каждую функцию интерфейса
отдельно — разделы целиком, панели, диалоги, меню — и кладёт результат в
`docs/manual/` с именами вида `01-Тестирование.png`. Эти снимки встроены в
руководство пользователя (`docs/Руководство-пользователя.md`). История и
сравнение берутся из `dist/results/` — там лежат настоящие прогоны семи
моделей; в рабочей папке `results/` после уборки остаётся один файл, и
разворот строки истории или столбцы сравнения показать было бы не на чем.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFrame, QMessageBox  # noqa: E402

from llmtestbench import __version__, testsets  # noqa: E402
from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.ui import MainWindow  # noqa: E402
from llmtestbench.ui.case_card_dialog import CaseCardDialog  # noqa: E402
from llmtestbench.ui.set_compare_dialog import SetCompareDialog  # noqa: E402
from llmtestbench.ui.test_editor_dialog import TestEditorDialog  # noqa: E402
from llmtestbench.ui.theme import apply_theme, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
MANUAL = DOCS / "manual"
#: Настоящие прогоны семи моделей — источник данных для истории и сравнения.
DEMO_RESULTS = ROOT / "dist" / "results"
#: Конфиг снимков. Рабочий `config.json` не трогаем: в нём живут настройки
#: пользователя, а `cfg.save()` из окна затёр бы их путями снимков.
SHOT_CONFIG = MANUAL / "_config.json"

#: Куда писать ход работы: stdout дочернего процесса из-под Bash пропадает
#: целиком, а по пустому выводу непонятно, дошло ли дело до конца.
LOG: list[str] = []


def _note(message: str) -> None:
    LOG.append(message)
    print(message, flush=True)


#: Путь к llama-server.exe для снимков. Настоящий путь выдаёт имя пользователя
#: (`C:\Users\<имя>\...`), а снимки уезжают в публичный репозиторий и в PDF
#: релиза — там ему не место. Значение подставляется в конфиг снимков, рабочий
#: `config.json` при этом не трогается.
SAFE_SERVER_PATH = (
    r"C:\Users\user\AppData\Roaming\LlamaServerLauncherAvalonia\llama.cpp\llama-server.exe"
)
SAFE_HOME = r"C:\Users\user"


def _anonymize(text: str) -> str:
    """Заменить домашний каталог на обезличенный путь."""
    home = str(Path.home())
    return text.replace(home, SAFE_HOME).replace(home.replace("\\", "/"), SAFE_HOME)


def _anonymize_logs(win: MainWindow) -> int:
    """Убрать домашний путь из журналов перед снимком.

    Строки журнала сервера приходят из автопоиска `llama-server` и содержат
    настоящие пути машины. Хранятся они в `ServerLogTab._lines`, а вид
    пересобирается из них `restyle_theme()` — значит, достаточно поправить
    строки и перерисовать. Полоса прогона — обычный `QPlainTextEdit`.
    """
    changed = 0
    tab = win.server_log
    for i, (line, level) in enumerate(tab._lines):
        fixed = _anonymize(line)
        if fixed != line:
            tab._lines[i] = (fixed, level)
            changed += 1
    if changed:
        tab.restyle_theme()
    run_text = win.run_log.toPlainText()
    fixed_run = _anonymize(run_text)
    if fixed_run != run_text:
        win.run_log.setPlainText(fixed_run)
    return changed


def _take_theme_arg(argv: list[str]) -> str | None:
    """Вынуть `--theme X` из аргументов.

    Из argv, а не из `QApplication.arguments()`: неизвестный ключ Qt потом
    ругает в stderr, и разбирать его вывод становится нельзя.
    """
    if "--theme" not in argv:
        return None
    i = argv.index("--theme")
    if i + 1 >= len(argv):
        return None
    value = argv[i + 1]
    del argv[i : i + 2]
    return value


# имя файла → раздел рельса. «case» — не раздел, а карточка кейса: она
# отдельное окно, и в снимке раздела её не видно.
SHOTS: list[tuple[str, str]] = [
    ("Предпросмотр-Тестирование.png", "test"),
    ("Предпросмотр-Сервер.png", "server"),
    ("Предпросмотр-История.png", "history"),
    ("Предпросмотр-Карточка-кейса.png", "case"),
    ("Предпросмотр-Сравнение.png", "compare"),
]


def shoot_case_card(app: QApplication, win: MainWindow, path: Path) -> None:
    """Снимок карточки кейса — первого кейса самого свежего прогона.

    Диалог показывается, а не запускается через `exec()`: `exec` заблокировал
    бы цикл событий, и до следующего снимка дело бы не дошло.
    """
    tab = win.history_tab
    if not tab._filtered:
        _note("карточка кейса: прогонов нет — снимок пропущен")
        return
    cases = tab._filtered[0].cases
    if not cases:
        _note("карточка кейса: в прогоне нет кейсов — снимок пропущен")
        return
    record, case = cases[0]
    dialog = CaseCardDialog(record, case, win)
    dialog.resize(900, 760)
    dialog.show()
    app.processEvents()
    app.processEvents()
    dialog.grab().save(str(path))
    _note("сохранено: %s (%d×%d)" % (path, dialog.width(), dialog.height()))
    dialog.close()


def shoot_compare(app: QApplication, win: MainWindow) -> None:
    """Наполнить сравнение двумя свежими прогонами и развернуть строку метрики.

    Пустая вкладка сравнения на снимке бесполезна: в документации нужен экран
    с данными. Прогоны берутся из истории (свежие первыми) — те же, что
    попадают в сравнение по кнопке «Сравнить выбранные».
    """
    groups = win.history_tab._filtered[:2]
    if len(groups) < 2:
        _note("сравнение: прогонов меньше двух — снимок будет с заглушкой")
        return
    tab = win.compare_tab
    tab.load([str(path) for group in groups for path in group.paths])

    # Разворот строки «Качество»: без него на снимке только сводка, и не
    # видно, что строка раскрывается по наборам.
    for row in range(tab.table.rowCount()):
        if tab._row_key(row) == "score" and tab._row_set(row) is None:
            tab._on_cell_clicked(row, 0)
            break
    app.processEvents()


def _save(app: QApplication, widget, path: Path) -> None:
    """Снять виджет в файл: два прохода событий, чтобы стили успели лечь."""
    path.parent.mkdir(parents=True, exist_ok=True)
    app.processEvents()
    app.processEvents()
    pix = widget.grab()
    pix.save(str(path))
    _note("сохранено: %s (%d×%d)" % (path.name, pix.width(), pix.height()))


def _shoot_menu(app: QApplication, menu, path: Path) -> None:
    """Снимок выпадающего меню.

    Меню — всплывающее окно, в `win.grab()` оно не попадает, поэтому снимаем
    сам `QMenu`. Размер берём из `sizeHint()`: без `resize` виджет, который
    ни разу не показывали, отдаёт кадр нулевой высоты.
    """
    menu.ensurePolished()
    menu.resize(menu.sizeHint())
    menu.show()
    app.processEvents()
    app.processEvents()
    menu.grab().save(str(path))
    menu.hide()
    _note("сохранено: %s" % path.name)


def _menu_by_title(win: MainWindow, title: str):
    for act in win.menuBar().actions():
        if act.text() == title:
            return act.menu()
    return None


def shoot_manual(app: QApplication, win: MainWindow, suffix: str) -> None:
    """Снять каждую функцию интерфейса для руководства пользователя."""

    def out(name: str) -> Path:
        stem = Path(name).stem + suffix
        return MANUAL / (stem + Path(name).suffix)

    # --- 1. Раздел «Тестирование» целиком -----------------------------
    _anonymize_logs(win)
    win.rail.set_current("test")
    app.processEvents()
    _save(app, win, out("01-Тестирование.png"))

    # --- 2–5. Панели раздела «Тестирование» ---------------------------
    _save(app, win.models_panel, out("02-Панель-моделей.png"))
    _save(app, win.settings_panel.sets_card, out("03-Наборы-тестов.png"))
    _save(app, win.settings_panel.params_card, out("04-Параметры-прогона.png"))
    run_bar = win.findChild(QFrame, "runBar")
    if run_bar is not None:
        _save(app, run_bar, out("05-Полоса-прогона.png"))
    else:
        _note("полоса прогона: виджет runBar не найден")

    # --- 6–7. Раздел «Сервер» ----------------------------------------
    win.rail.set_current("server")
    app.processEvents()
    _anonymize_logs(win)
    _save(app, win, out("06-Сервер.png"))
    _save(app, win.server_log, out("07-Журнал-сервера.png"))

    # --- 8. Раздел «История» с развёрнутым прогоном -------------------
    win.rail.set_current("history")
    app.processEvents()
    if win.history_tab._filtered:
        # Разворот делается ПОСЛЕ переключения раздела: пока страница скрыта
        # в стеке, у таблицы геометрия-заглушка, и развёрнутые строки легли
        # бы не так, как в показанном окне.
        win.history_tab._toggle_group(win.history_tab._filtered[0].key)
        app.processEvents()
    _save(app, win, out("08-История.png"))

    # --- 9. Карточка кейса -------------------------------------------
    shoot_case_card(app, win, out("09-Карточка-кейса.png"))

    # --- 10. Раздел «Сравнение» --------------------------------------
    win.rail.set_current("compare")
    shoot_compare(app, win)
    _save(app, win, out("10-Сравнение.png"))

    # --- 11. Покейсовое сравнение набора ------------------------------
    _shoot_set_compare(app, win, out("11-Сравнение-набора.png"))

    # --- 12. Редактор тестов -----------------------------------------
    sets = testsets.discover_test_sets(win.cfg.tests_path)
    if sets:
        editor = TestEditorDialog(sets, win)
        editor.resize(1180, 720)
        editor.show()
        app.processEvents()
        app.processEvents()
        _save(app, editor, out("12-Редактор-тестов.png"))
        editor.close()
    else:
        _note("редактор тестов: наборы не найдены — снимок пропущен")

    # --- 13–16. Меню --------------------------------------------------
    for i, title in enumerate(("Файл", "Сервис", "Вид", "Справка"), start=13):
        menu = _menu_by_title(win, title)
        if menu is None:
            _note("меню «%s» не найдено" % title)
            continue
        _shoot_menu(app, menu, out("%02d-Меню-%s.png" % (i, title)))
        if title == "Вид":
            # Подменю «Тема» — отдельный снимок: в нём выбор темы, а это
            # функция, которую в руководстве нужно показать, а не описать.
            for act in menu.actions():
                if act.text() == "Тема" and act.menu() is not None:
                    _shoot_menu(app, act.menu(), out("15b-Меню-Тема.png"))
                    break

    # --- 17. О программе ----------------------------------------------
    _shoot_about(app, win, out("17-О-программе.png"))

    # --- 18. Светлая тема ---------------------------------------------
    win.rail.set_current("test")
    win._set_theme("light")
    app.processEvents()
    _save(app, win, out("18-Светлая-тема.png"))
    win._set_theme(resolve("dark") if suffix else "dark")
    app.processEvents()


def _shoot_set_compare(app: QApplication, win: MainWindow, path: Path) -> None:
    """Снимок покейсового сравнения набора — набор, который гоняли все прогоны."""
    groups = win.history_tab._filtered[:3]
    if len(groups) < 2:
        _note("сравнение набора: прогонов меньше двух — снимок пропущен")
        return
    counts: dict[str, int] = {}
    for group in groups:
        for set_id in group.set_ids:
            counts[set_id] = counts.get(set_id, 0) + 1
    if not counts:
        _note("сравнение набора: у прогонов нет наборов — снимок пропущен")
        return
    set_id = max(counts, key=lambda k: counts[k])
    set_name = set_id
    for group in groups:
        if set_id in group.set_ids:
            idx = group.set_ids.index(set_id)
            if idx < len(group.set_names):
                set_name = group.set_names[idx]
            break
    dialog = SetCompareDialog(set_id, set_name, groups, win.cfg, win)
    dialog.resize(1000, 700)
    dialog.show()
    app.processEvents()
    app.processEvents()
    _save(app, dialog, path)
    dialog.close()


def _shoot_about(app: QApplication, win: MainWindow, path: Path) -> None:
    """Снимок окна «О программе».

    `MainWindow._about` зовёт статический `QMessageBox.about`, который ждёт
    нажатия кнопки, — в снимке это тупик. Собираем такое же окно сами; текст
    зеркалит `MainWindow._about`.
    """
    box = QMessageBox(win)
    box.setWindowTitle("О программе")
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        "<b>LLM Test Bench</b> v%s<br><br>"
        "Стенд тестирования локальных моделей (GGUF) на llama.cpp.<br>"
        "Реализованы этапы 1–7 ТЗ: интерфейс, сканирование моделей, "
        "метаданные GGUF, генерация команды запуска, управление процессом "
        "сервера с health-check, исполнитель тестов, сохранение JSON и "
        "HTML-отчёты.<br><br>"
        "Прямой режим (llama-cpp-python) отложен." % __version__
    )
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.show()
    app.processEvents()
    app.processEvents()
    _save(app, box, path)
    box.close()


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    manual = "--manual" in sys.argv
    if manual:
        sys.argv.remove("--manual")
        MANUAL.mkdir(parents=True, exist_ok=True)

    cfg = AppConfig.load()
    requested = _take_theme_arg(sys.argv)
    theme_name = requested or getattr(cfg, "ui_theme", "dark") or "dark"
    suffix = "" if resolve(theme_name) == "dark" else "-светлая"

    # Прогоны для истории и сравнения — настоящие, из dist/results: рабочий
    # `results/` почти пуст, и без подмены обе таблицы выходят заглушкой.
    if DEMO_RESULTS.is_dir():
        cfg.results_dir = str(DEMO_RESULTS)
    else:
        _note("нет папки %s — история будет пустой" % DEMO_RESULTS)

    # Привязка к копии конфига — в обоих режимах: `save()` из окна (закрытие
    # окна, «Сохранить настройки») иначе перепишет рабочий `config.json`.
    cfg.bind(SHOT_CONFIG)
    # Настоящий путь к llama-server.exe виден в панели сервера, а имя
    # пользователя в снимке, который уезжает в публичный репозиторий, лишнее.
    cfg.llama_server_path = SAFE_SERVER_PATH

    app = QApplication(sys.argv)
    apply_theme(app, theme_name)
    _note("тема: %s → %s%s" % (theme_name, resolve(theme_name), suffix))

    win = MainWindow(cfg)
    win.resize(1360, 860)
    win.show()

    if manual:
        # Снимки делаются одним проходом после сканирования моделей: панель
        # моделей без списка — это пустая рамка, а ждать её в таймерной цепочке
        # пришлось бы на каждом шаге.
        state = {"done": False}

        def after_scan() -> None:
            if state["done"]:
                return
            state["done"] = True
            try:
                shoot_manual(app, win, suffix)
            except Exception:  # noqa: BLE001 — падение снимка не должно съедать лог
                import traceback

                _note(traceback.format_exc())
            finally:
                _write_log()
                win.close()
                app.quit()

        win.models_panel.scan_finished.connect(lambda _: QTimer.singleShot(1500, after_scan))
        QTimer.singleShot(60000, after_scan)  # страховка от вечного ожидания
        return app.exec()

    state = {"i": 0}

    def shot_path(name: str) -> Path:
        stem = Path(name).stem + suffix
        return DOCS / (stem + Path(name).suffix)

    def shoot() -> None:
        i = state["i"]
        if i >= len(SHOTS):
            _write_log()
            win.close()
            app.quit()
            return
        name, section = SHOTS[i]
        if section == "case":
            shoot_case_card(app, win, shot_path(name))
        else:
            win.rail.set_current(section)
            if section == "history":
                tab = win.history_tab
                if tab._filtered:
                    tab._toggle_group(tab._filtered[0].key)
            elif section == "compare":
                shoot_compare(app, win)
            app.processEvents()
            _anonymize_logs(win)
            pix = win.grab()
            path = shot_path(name)
            pix.save(str(path))
            _note("сохранено: %s (%d×%d)" % (path, pix.width(), pix.height()))
        state["i"] = i + 1
        QTimer.singleShot(900, shoot)

    # Дать окну дозагрузиться: сканирование моделей читает метаданные GGUF
    # и на папке в полсотни файлов занимает около десяти секунд.
    QTimer.singleShot(14000, shoot)
    QTimer.singleShot(40000, lambda: (win.close(), app.quit()))
    return app.exec()


def _write_log() -> None:
    """Записать ход снимков в файл рядом с ними."""
    try:
        target = MANUAL / "_shots.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(LOG) + "\n", encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
