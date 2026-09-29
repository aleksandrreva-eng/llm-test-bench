"""Точка входа LLM Test Bench.

Обычный запуск:
    .venv\\Scripts\\python.exe main.py

Проверка без графики (сканирование + команда запуска):
    .venv\\Scripts\\python.exe main.py --selfcheck "F:\\Models"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from llmtestbench import __version__
from llmtestbench.config import AppConfig
from llmtestbench.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="llm-test-bench",
        description="Стенд тестирования локальных моделей (GGUF) на llama.cpp",
    )
    parser.add_argument("--models-dir", help="папка с .gguf-моделями")
    parser.add_argument("--config", help="путь к config.json (по умолчанию рядом с приложением)")
    parser.add_argument(
        "--selfcheck",
        nargs="?",
        const="",
        metavar="DIR",
        help="проверить сканирование и генерацию команды без запуска GUI",
    )
    parser.add_argument("--version", action="version", version="LLM Test Bench %s" % __version__)
    return parser.parse_args(argv)


def _robust_stdout() -> None:
    """Консоль Windows по умолчанию cp1251: символы вроде ⤷ или «·» в
    именах моделей роняют print с UnicodeEncodeError. Оставляем кодировку
    консоли, но допускаем замену неподдерживаемых символов, чтобы
    headless-режим не падал."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def selfcheck(directory: str, cfg: AppConfig) -> int:
    """Headless-проверка: сканер, метаданные, команда запуска."""
    from llmtestbench import server_cmd
    from llmtestbench.gguf_scanner import scan_models

    directory = directory or cfg.models_dir
    print("LLM Test Bench %s — самопроверка" % __version__)
    print("Папка моделей: %s" % (directory or "не задана"))

    env = server_cmd.describe_environment()
    print("Python %s, платформа %s" % (env["python"], env["platform"]))
    if env["server_candidates"]:
        for c in env["server_candidates"]:
            print("  llama-server: %s -> %s" % (c["label"], c["path"]))
    else:
        print("  llama-server: не найден")
    print()

    if not directory or not Path(directory).is_dir():
        print("Папка не найдена — сканирование пропущено.")
        return 1

    models = scan_models(directory, recursive=cfg.recursive_scan)
    real = [m for m in models if m.is_model]
    print(
        "Найдено .gguf: %d (моделей %d, сайдкаров %d)"
        % (len(models), len(real), len(models) - len(real))
    )
    print()

    for m in models:
        mark = "  " if m.is_model else "⤷ "
        print("%s%s" % (mark, m.file_name))
        print(
            "     размер %s · параметров %s · контекст %s · %s · %s"
            % (
                m.size_human,
                m.params_human or "—",
                m.n_ctx_train or "—",
                m.quant or "—",
                m.arch or "—",
            )
        )
        if m.error:
            print("     ОШИБКА: %s" % m.error)
    print()

    exe = cfg.llama_server_path or server_cmd.autodetect_server()
    if real and exe:
        params = server_cmd.resolve_params(real[0], cfg)
        print("Пример команды для «%s»:" % real[0].file_name)
        print("  %s" % server_cmd.build_command(exe, params))
        print("  источники: %s" % params.sources)
    elif real:
        print("llama-server.exe не найден — команду не собрать.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _robust_stdout()
    args = _parse_args(argv)
    cfg_path = Path(args.config) if args.config else None
    cfg = AppConfig.load(cfg_path)
    if args.models_dir:
        cfg.models_dir = args.models_dir

    # Логи включаем до всего остального: если падение случится при сканировании
    # моделей или построении окна, причина должна остаться в файле, а не только
    # на экране, который пользователь закроет.
    setup_logging(cfg.logs_path, cfg.log_level)
    log.info("LLM Test Bench %s запускается (режим %s)", __version__, cfg.ui_theme)

    if args.selfcheck is not None:
        return selfcheck(args.selfcheck, cfg)

    from PySide6.QtWidgets import QApplication

    from llmtestbench.ui import MainWindow
    from llmtestbench.ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("LLM Test Bench")
    app.setApplicationVersion(__version__)
    # Тема — до постройки окна: панели читают цвета в момент создания, и
    # окно, собранное в тёмной теме, пришлось бы потом перекрашивать целиком.
    apply_theme(app, getattr(cfg, "ui_theme", "dark"))

    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
