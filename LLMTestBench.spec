# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация для LLM Test Bench.

Сборка one-file (один .exe). В бандл кладём только read-only ресурсы
(tests, docs) — конфиг и рабочие папки (results, reports, logs, sessions,
history.db) создаются рядом с .exe в рантайме, чтобы приложение было
переносимым и не зависело от путей машины разработчика.

Собрать:  .venv/Scripts/python.exe build_app.py
"""

import os

# Папка, где лежит этот .spec (корень проекта). SPECPATH в PyInstaller — это
# каталог спеки (без имени файла). Делаем пути относительными, чтобы
# спецификация не была привязана к конкретному диску/пользователю.
ROOT = os.path.abspath(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "tests"), "tests"),
        (os.path.join(ROOT, "docs"), "docs"),
        (os.path.join(ROOT, "icon.ico"), "icon.ico"),
    ],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        # Печать сравнения импортируется лениво, внутри `_print()` — без этой
        # строки кнопка «Печать» в собранном приложении падала бы с
        # ModuleNotFoundError, а заметить это можно только вручную.
        "PySide6.QtPrintSupport",
        "requests",
        "gguf",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # В requirements.txt их больше нет — оставлены страховкой на случай,
        # если какая-то зависимость притащит их транзитивно.
        "pandas",
        "jinja2",
        # Гарантированно не нужны в собранном приложении:
        "matplotlib",
        "tkinter",
        "unittest",
        "PyQt5",
        "PyQt6",
        "PySide2",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LLMTestBench",
    debug=False,
    icon=os.path.join(ROOT, "icon.ico"),
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
