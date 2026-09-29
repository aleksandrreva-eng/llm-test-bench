"""Сборка MSI-установщика LLM Test Bench через WiX Toolset 4.

Запуск из корня проекта:
    .venv\\Scripts\\python.exe make_msi.py

Что делает:
  1. проверяет, что собран `dist/LLMTestBench.exe` (его делает `build_app.py`);
  2. берёт версию из `llmtestbench/__init__.py` — единственного источника;
  3. готовит `installer/License.rtf` из `LICENSE` для страницы лицензии;
  4. вызывает `wix build` и кладёт результат в `dist/`.

Требуется WiX 4 (`dotnet tool install --global wix`) и расширение
`WixToolset.UI.wixext` той же версии, что сам WiX:

    wix extension add -g WixToolset.UI.wixext/4.0.6

Собирается установщик **в профиль пользователя** — подробности и причина
в шапке `installer/LLMTestBench.wxs`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / "installer"
WXS = INSTALLER / "LLMTestBench.wxs"
LICENSE = ROOT / "LICENSE"
LICENSE_RTF = INSTALLER / "License.rtf"
EXE = ROOT / "dist" / "LLMTestBench.exe"
INIT = ROOT / "llmtestbench" / "__init__.py"

#: WiX ставится как глобальный инструмент dotnet и в PATH может не оказаться.
WIX_CANDIDATES = [
    Path.home() / ".dotnet" / "tools" / "wix.exe",
    Path(r"C:\Program Files\WiX Toolset v4\bin\wix.exe"),
]


def find_wix() -> str | None:
    found = shutil.which("wix")
    if found:
        return found
    for path in WIX_CANDIDATES:
        if path.is_file():
            return str(path)
    return None


def read_version() -> str:
    """Версия пакета — из `__init__.py`, а не из второго места."""
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("не нашёл __version__ в %s" % INIT)
    version = match.group(1)
    # MSI понимает только числовую версию вида major.minor.build.
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("версия %r не годится для MSI: нужен вид 1.2.3" % version)
    return version


def write_license_rtf() -> None:
    """Переложить текст лицензии в RTF — страница лицензии в WiX ждёт RTF."""
    text = LICENSE.read_text(encoding="utf-8")
    # В RTF служебные символы экранируются обратным слешем.
    escaped = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    lines = escaped.splitlines()
    body = "\\par\n".join(lines)
    rtf = (
        r"{\rtf1\ansi\ansicpg1251\deff0"
        r"{\fonttbl{\f0\fmodern Consolas;}{\f1\fswiss Segoe UI;}}"
        r"\viewkind4\uc1\pard\f0\fs18 " + body + r"\par}"
    )
    LICENSE_RTF.write_text(rtf, encoding="ascii", errors="replace")


def build(version: str) -> int:
    wix = find_wix()
    if wix is None:
        print(
            "Ошибка: не нашёл wix.exe.\n"
            "Поставьте WiX 4:  dotnet tool install --global wix\n"
            "и расширение:     wix extension add -g WixToolset.UI.wixext/4.0.6"
        )
        return 1

    if not EXE.is_file():
        print("Ошибка: нет %s — сначала соберите его: build_app.py" % EXE)
        return 1

    target = ROOT / "dist" / ("LLMTestBench-%s.msi" % version)
    cmd = [
        wix,
        "build",
        "-ext",
        "WixToolset.UI.wixext",
        "-d",
        "Version=%s" % version,
        str(WXS),
        "-o",
        str(target),
    ]
    print("Команда:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, cwd=str(INSTALLER))
    except subprocess.CalledProcessError as exc:
        print("\nСборка MSI упала (код %d)" % exc.returncode)
        return exc.returncode

    size = target.stat().st_size / 1024 / 1024
    print("\nГотово! Установщик: %s (%.1f МБ)" % (target, size))
    return 0


def main() -> int:
    for path in (WXS, LICENSE, INIT):
        if not path.is_file():
            print("Ошибка: нет %s" % path)
            return 1
    version = read_version()
    print("Версия: %s" % version)
    write_license_rtf()
    print("Лицензия для установщика: %s" % LICENSE_RTF)
    return build(version)


if __name__ == "__main__":
    sys.exit(main())
