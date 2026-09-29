"""Сборка PDF руководства пользователя из Markdown.

Цепочка: `docs/Руководство-пользователя.md` → HTML → PDF.

HTML собирается библиотекой `markdown`, а PDF печатает headless-браузер
(Chrome или Edge) — это единственный способ получить на Windows нормальную
кириллицу, таблицы и картинки без тяжёлых зависимостей вроде GTK для
WeasyPrint.

Библиотека `markdown` стоит не в проекте, а рядом с инструментами ассистента
(`~/.workbuddy-ai/tools/mdlib`) — чтобы не тащить её в `requirements.txt`
проекта: она нужна один раз при сборке документации, а не при работе
приложения. Путь подкладывается в `sys.path` ниже.

Запуск:  .venv/Scripts/python.exe make_manual_pdf.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
SOURCE = DOCS / "Руководство-пользователя.md"
BUILD_HTML = DOCS / "_manual_build.html"
TARGET = DOCS / "Руководство-пользователя.pdf"

#: Где искать библиотеку markdown: сначала рядом с инструментами ассистента,
#: потом — вдруг она всё-таки стоит в окружении проекта.
MDLIB_CANDIDATES = [
    Path.home() / ".workbuddy-ai" / "tools" / "mdlib",
    ROOT / ".tools" / "mdlib",
]

BROWSERS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

#: Стиль печати. Документ светлый, а не в тёмной теме интерфейса: PDF читают
#: с листа и с экрана читалки, где тёмный фон — это истраченный тонер и
#: нечитаемый текст в большинстве просмотрщиков.
CSS = """
@page { size: A4; margin: 18mm 15mm 16mm; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  font-family: "Segoe UI", "Noto Sans", Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #1b1b1b;
  margin: 0;
}

h1 {
  font-size: 23pt;
  line-height: 1.2;
  margin: 0 0 6pt;
  padding-bottom: 8pt;
  border-bottom: 3px solid #2f6fd0;
  color: #14315e;
}

h2 {
  font-size: 16pt;
  margin: 0 0 10pt;
  padding-bottom: 5pt;
  border-bottom: 1px solid #d5dbe3;
  color: #14315e;
  page-break-before: always;
  page-break-after: avoid;
}

/* Первый раздел не должен начинаться с пустой страницы после титула. */
h1 + h2, h2:first-of-type { page-break-before: avoid; }

h3 {
  font-size: 12.5pt;
  margin: 14pt 0 6pt;
  color: #1d3f73;
  page-break-after: avoid;
}

p { margin: 6pt 0; }

img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 9pt auto;
  border: 1px solid #ccd2da;
  border-radius: 4px;
  page-break-inside: avoid;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 8pt 0 12pt;
  font-size: 9pt;
}

th, td {
  border: 1px solid #ccd2da;
  padding: 4pt 6pt;
  text-align: left;
  vertical-align: top;
}

th { background: #eef2f7; font-weight: 600; }
tr { page-break-inside: avoid; }

code {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: 9pt;
  background: #f1f3f6;
  padding: 1pt 3pt;
  border-radius: 3px;
}

pre {
  background: #f6f8fa;
  border: 1px solid #d8dee6;
  border-radius: 4px;
  padding: 7pt 9pt;
  font-size: 8.8pt;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  page-break-inside: avoid;
}

pre code { background: none; padding: 0; font-size: inherit; }

blockquote {
  margin: 8pt 0;
  padding: 6pt 10pt;
  border-left: 3px solid #9fb4d0;
  background: #f6f9fd;
  color: #333;
  page-break-inside: avoid;
}

blockquote p { margin: 3pt 0; }

ul, ol { margin: 6pt 0; padding-left: 20pt; }
li { margin: 2pt 0; }

hr { border: 0; border-top: 1px solid #d5dbe3; margin: 14pt 0; }

a { color: #0b5cad; text-decoration: none; }

em { color: #4a4a4a; }
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>LLM Test Bench — руководство пользователя</title>
<style>%s</style>
</head>
<body>
%s
</body>
</html>
"""


def _add_mdlib_to_path() -> None:
    for candidate in MDLIB_CANDIDATES:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            return


def _find_browser() -> Path | None:
    for path in BROWSERS:
        if path.exists():
            return path
    return None


def build_html() -> Path:
    _add_mdlib_to_path()
    try:
        import markdown  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — окружение сборки
        raise SystemExit(
            "не нашёл библиотеку markdown. Поставьте её командой:\n"
            "  .venv\\Scripts\\python.exe -m pip install --target "
            '"%s" markdown' % MDLIB_CANDIDATES[0]
        ) from exc

    text = SOURCE.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "md_in_html"],
    )
    BUILD_HTML.write_text(TEMPLATE % (CSS, body), encoding="utf-8")
    return BUILD_HTML


def build_pdf(html: Path) -> None:
    browser = _find_browser()
    if browser is None:
        raise SystemExit("не нашёл Chrome или Edge для печати PDF")

    # as_uri() процентов кодирует кириллицу в пути — Chrome иначе не откроет
    # файл по file://.
    cmd = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        "--virtual-time-budget=20000",
        "--run-all-compositor-stages-before-draw",
        "--print-to-pdf=%s" % TARGET,
        html.as_uri(),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not TARGET.exists():
        raise SystemExit(
            "PDF не создан.\nstdout: %s\nstderr: %s"
            % (result.stdout[-2000:], result.stderr[-2000:])
        )
    print("браузер: %s" % browser)
    print("exit=%d" % result.returncode)


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit("нет исходника: %s" % SOURCE)
    html = build_html()
    print("HTML: %s (%.1f КБ)" % (html, html.stat().st_size / 1024))
    build_pdf(html)
    print("PDF:  %s (%.1f КБ)" % (TARGET, TARGET.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
