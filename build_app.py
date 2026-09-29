"""Сборка автономного .exe для LLM Test Bench через PyInstaller.

Запуск из корня проекта:
    .venv\\Scripts\\python.exe build_app.py

Собирает one-file приложение по LLMTestBench.spec. Ресурсы (tests, docs)
кладутся внутрь бандла; config.json и рабочие папки создаются рядом с
.exe в рантайме (см. llmtestbench/config.py: app_root/bundle_root).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
SPEC = PROJECT_ROOT / "LLMTestBench.spec"


def build() -> int:
    if not VENV_PYTHON.is_file():
        print(f"Ошибка: виртуальное окружение не найдено: {VENV_PYTHON}")
        return 1

    if not SPEC.is_file():
        print(f"Ошибка: спецификация не найдена: {SPEC}")
        return 1

    print(f"Сборка из: {PROJECT_ROOT}")
    cmd = [
        str(VENV_PYTHON),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]
    print("Команда:", " ".join(cmd))

    # Запускаем из корня проекта, чтобы SPECPATH в .spec резолвился верно.
    try:
        subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))
    except subprocess.CalledProcessError as exc:
        print(f"\nСборка упала (код {exc.returncode})")
        return exc.returncode

    dist_exe = PROJECT_ROOT / "dist" / "LLMTestBench.exe"
    if dist_exe.is_file():
        size = dist_exe.stat().st_size / 1024 / 1024
        print(f"\nГотово! Исполняемый файл: {dist_exe} ({size:.1f} МБ)")
        return 0
    print("\nСборка завершилась, но dist/LLMTestBench.exe не найден.")
    return 1


if __name__ == "__main__":
    sys.exit(build())
