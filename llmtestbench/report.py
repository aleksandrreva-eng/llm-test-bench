"""HTML-отчёт по результатам прогонов (этап 7 ТЗ).

Отчёт — один файл без внешних зависимостей: ни шрифтов, ни скриптов с CDN.
Причина простая: его будут открывать на машине без интернета и пересылать
коллегам, а «не подгрузилась таблица стилей» — не тот способ узнать
результаты теста.

Что в отчёте важнее всего:

* **Сбой стенда отделён от провала модели.** Кейс, который не доехал до
  сервера, не должен выглядеть как ошибка модели — иначе одна перезагрузка
  портит всю картину.
* **Пустой ответ объясняется.** Не «провал», а «рассуждение зациклилось» или
  «бюджет ушёл в рассуждение»: у этих случаев разные причины и разные выводы.
* **Метрики видны раздельно**: вход, префилл, время до первого токена,
  выход, из него ответ и рассуждение.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .atomic_io import write_text_atomic

# --- палитра приложения ---
#
# Отчёт читает цвета той же темы, что и приложение (`ui/theme.py`): в UI его
# собирают уже с выбранной темой, и светлый отчёт не должен выдать тёмный, и
# наоборот. Но этот модуль — Qt-свободный, и его гонят проверки (`check_*.py`)
# и `run_tests.py` без PySide6. Поэтому тему берём лениво: если `ui.theme`
# импортируется — читаем `active_theme()`, иначе остаёмся на тёмной палитре по
# умолчанию. От этого цвета в отчёте и в приложении не разносятся.
#
# Ключи словаря — те же, что в шаблоне CSS и в прежней зашивке, чтобы CSS не
# менялся: приложение-тема называет их иначе (`SURFACE`, `TEXT_2` и т.д.), а
# отчёт привязан к своим именам.

#: Палитра по умолчанию — тёмная, как в приложении по умолчанию.
DEFAULT_PALETTE = {
    "BG": "#1e1e1e",
    "PANEL": "#252526",
    "HEADER": "#2d2d30",
    "BORDER": "#3e3e42",
    "TEXT": "#ffffff",
    "TEXT_DIM": "#cccccc",
    "TEXT_MUTED": "#9d9d9d",
    "OK": "#4ec9b0",
    "WARN": "#dcdcaa",
    "FAIL": "#f48771",
    "LABEL": "#9cdcfe",
}

#: Как перевести цвета из палитры приложения (`ui/theme.py`) в ключи этого
#: модуля. Роли совпадают: фон/панель/шапка — три ступени яркости, рамки и
#: текст те же, семантика и «синий ключ» — один в один.
_PALETTE_KEYS = {
    "BG": "SURFACE",
    "PANEL": "SURFACE_2",
    "HEADER": "SURFACE_3",
    "BORDER": "BORDER",
    "TEXT": "TEXT",
    "TEXT_DIM": "TEXT_2",
    "TEXT_MUTED": "TEXT_3",
    "OK": "OK",
    "WARN": "WARN",
    "FAIL": "FAIL",
    "LABEL": "LABEL",
}


def _palette(theme_name: str | None) -> dict[str, str]:
    """Палитра отчёта по имени темы.

    `theme_name` — как в `ui.theme`: `dark` / `light` / `auto`. Если не передан
    (Qt-вызов без темы, CLI) и тема импортируется — читаем активную тему
    приложения. Если тема не импортируется (PySide6 нет) — тёмная по умолчанию.
    """
    if theme_name is None:
        try:
            from .ui import theme

            theme_name = theme.active_theme()
        except ImportError:
            return dict(DEFAULT_PALETTE)
    palette = _PALETTE_KEYS
    try:
        from .ui import theme

        tokens = theme.PALETTES[theme_name]
        return {key: tokens[src] for key, src in palette.items()}
    except (ImportError, KeyError):
        return dict(DEFAULT_PALETTE)


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def verdict(case: dict) -> tuple[str, str]:
    """(класс, подпись) для вердикта кейса."""
    if case.get("stand_error"):
        return "stand", "сбой стенда"
    if case.get("passed") is None:
        return "skip", "пропуск"
    if case.get("passed"):
        return "pass", "зачтён"
    return "fail", "провал"


def case_metrics_text(case: dict) -> str:
    """Строка метрик кейса одной строкой.

    Вынесена из `_case_row` затем, что те же числа показывает карточка кейса в
    окне (`ui/case_card_dialog.py`). Две копии формулировки разошлись бы при
    первой же правке, и отчёт с карточкой говорили бы о кейсе разными словами.

    Скорость префилла стоит рядом с его временем, а не в общем хвосте: на
    наборе `speed` это главная метрика — префилл различает модели в разы,
    тогда как генерация у них держится примерно одинаково. «≈» означает, что
    скорости посчитаны по своим замерам, а не пришли от сервера: сборка без
    `timings` точных чисел не отдаёт.
    """
    approx = "≈" if case.get("speeds_estimated") else ""
    return (
        "вход %s · префилл %s мс (%s%s t/s) · TTFT %s мс · "
        "выход %s (ответ %s, рассужд. %s) · %s%s t/s"
        % (
            case.get("prompt_tokens", 0),
            case.get("prompt_ms", 0),
            approx,
            case.get("prompt_tokens_per_sec", 0),
            round(case.get("ttft_ms") or 0),
            case.get("completion_tokens", 0),
            case.get("answer_tokens", 0),
            case.get("reasoning_tokens", 0),
            approx,
            case.get("tokens_per_sec", 0),
        )
    )


def load_runs(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Прочитать файлы результатов."""
    runs = []
    for p in paths:
        path = Path(p)
        runs.append(json.loads(path.read_text(encoding="utf-8")))
    return runs


def _pct(value: float | None) -> str:
    return "—" if value is None else "%.0f%%" % (value * 100)


def _score_class(score: float | None) -> str:
    if score is None:
        return "skip"
    if score >= 0.9:
        return "pass"
    if score >= 0.7:
        return "warn"
    return "fail"


def _summary_card(run: dict) -> str:
    s = run.get("summary") or {}
    cls = _score_class(s.get("score"))
    bits = [
        '<div class="card %s">' % cls,
        '<div class="score">%s</div>' % _pct(s.get("score")),
        '<div class="of">%d из %d</div>' % (s.get("passed", 0), s.get("counted", 0)),
        '<div class="set">%s</div>' % esc(run.get("set_name") or run.get("set_id")),
        "</div>",
    ]
    return "".join(bits)


def _overview_table(runs: list[dict]) -> str:
    rows = []
    for run in runs:
        s = run.get("summary") or {}
        rows.append(
            "<tr>"
            "<td>%s</td><td>%s</td><td>%s</td>"
            '<td class="num">%d</td><td class="num">%d</td>'
            '<td class="num">%d</td><td class="num">%d</td>'
            '<td class="num %s">%s</td>'
            "</tr>"
            % (
                esc(run.get("model")),
                esc(run.get("set_name") or run.get("set_id")),
                esc((run.get("started") or "")[:16].replace("T", " ")),
                s.get("total", 0),
                s.get("passed", 0),
                s.get("failed", 0),
                s.get("stand_errors", 0),
                _score_class(s.get("score")),
                _pct(s.get("score")),
            )
        )
    return (
        '<table class="grid"><thead><tr>'
        "<th>Модель</th><th>Набор</th><th>Начало</th>"
        '<th class="num">Всего</th><th class="num">Зачтено</th>'
        '<th class="num">Провалено</th><th class="num">Сбои стенда</th>'
        '<th class="num">Итог</th>'
        "</tr></thead><tbody>%s</tbody></table>" % "".join(rows)
    )


def _tag_bars(run: dict) -> str:
    by_tag = (run.get("summary") or {}).get("by_tag") or {}
    if not by_tag:
        return ""
    items = []
    for tag, slot in sorted(by_tag.items(), key=lambda kv: -kv[1]["total"]):
        total = max(1, int(slot.get("total") or 0))
        passed = int(slot.get("passed") or 0)
        share = passed / total
        cls = _score_class(share)
        items.append(
            '<div class="tagrow"><span class="tagname">%s</span>'
            '<span class="bar"><i class="%s" style="width:%.1f%%"></i></span>'
            '<span class="tagnum">%d/%d</span></div>'
            % (esc(tag), cls, share * 100.0, passed, total)
        )
    return '<div class="tags">%s</div>' % "".join(items)


def _case_row(case: dict, index: int) -> str:
    cls, label = verdict(case)
    metrics = case_metrics_text(case)
    details = [
        "<details><summary>подробности</summary>",
        '<div class="block"><b>Задание</b><pre>%s</pre></div>' % esc(case.get("prompt")),
    ]
    if case.get("answer"):
        details.append('<div class="block"><b>Ответ</b><pre>%s</pre></div>' % esc(case["answer"]))
    if case.get("reasoning"):
        details.append(
            '<div class="block"><b>Рассуждение модели</b><pre class="dim">%s</pre></div>'
            % esc(case["reasoning"])
        )
    if case.get("error"):
        details.append(
            '<div class="block"><b>Ошибка</b><pre class="bad">%s</pre></div>' % esc(case["error"])
        )
    if case.get("judge_score") is not None or case.get("judge_error"):
        jscore = case.get("judge_score")
        jline = "судья: %s" % ("%.1f/10" % jscore if jscore is not None else "—")
        if case.get("judge_error"):
            jline += " · %s" % esc(case["judge_error"])
        jbits = ['<div class="block"><b>Оценка судьи</b><pre class="dim">%s' % jline]
        if case.get("judge_response"):
            jbits.append("сырой ответ судьи: %s" % esc(case["judge_response"])[:500])
        jbits.append("</pre></div>")
        details.append("".join(jbits))
    details.append(
        '<div class="block"><b>Параметры</b><pre class="dim">'
        "проверка: %s\nбюджет ответа: %s, с запасом на рассуждение: %s\n"
        "причина остановки: %s</pre></div>"
        % (
            esc(case.get("check_type")),
            case.get("max_tokens_answer", 0),
            case.get("max_tokens_effective", 0),
            esc(case.get("stop_reason")),
        )
    )
    details.append("</details>")

    return (
        '<tr class="%s">'
        '<td class="num">%d</td>'
        '<td><span class="dot %s"></span>%s</td>'
        "<td>%s</td>"
        '<td class="reason">%s</td>'
        '<td class="metrics">%s</td>'
        "</tr>"
        '<tr class="detailrow"><td colspan="5">%s</td></tr>'
        % (
            cls,
            index,
            cls,
            esc(case.get("case_id")),
            esc(case.get("name")),
            esc(case.get("reason")),
            esc(metrics),
            "".join(details),
        )
    )


def _mib(value: float) -> str:
    """МиБ в читаемый вид: до гигабайта — как есть, дальше в гибибайтах."""
    if value >= 1024:
        return "%.2f ГиБ" % (value / 1024)
    return "%.1f МиБ" % value


def _memory_block(run: dict) -> str:
    """Раздел «Память модели» — из блока `server`, собранного по логу загрузки.

    Лог загрузки читается только у сервера, поднятого самим приложением: по HTTP
    этих цифр не отдаёт ни `/props`, ни `/metrics`. Поэтому «данных нет» здесь —
    обычное состояние, и говорим мы о нём прямо, а не показываем нули: ноль
    VRAM у работающей модели читался бы как ошибка, а не как отсутствие замера.
    """
    mem = run.get("memory") or {}
    if not mem:
        return (
            '<div class="mem none">Память модели: нет данных — лог загрузки '
            "сервера не читался. Он есть только тогда, когда сервер поднимает "
            "само приложение и запускает его с подробным выводом.</div>"
        )

    tiles = "".join(
        '<div class="memtile"><span class="memlabel">%s</span>'
        '<span class="memvalue">%s</span></div>' % (label, _mib(value))
        for label, value in (
            ("VRAM (буферы)", float(mem.get("vram_mib") or 0.0)),
            ("RAM (host)", float(mem.get("ram_mib") or 0.0)),
            ("С диска (mmap)", float(mem.get("disk_mib") or 0.0)),
        )
    )

    buffers = mem.get("buffers") or {}
    buf_bits = (
        " · ".join("%s — %s" % (key, _mib(float(mib))) for key, mib in sorted(buffers.items()))
        or "—"
    )

    layers = mem.get("layers") or {}
    total = int(mem.get("layers_total") or 0)
    if layers:
        layer_bits = ", ".join(
            "%s: %d" % (device, count) for device, count in sorted(layers.items())
        )
        if total:
            layer_bits += " (всего слоёв %d)" % total
    else:
        layer_bits = (
            "— раскладки по слоям в логе нет: её печатает только сервер, запущенный с ключом -v"
        )

    devices = mem.get("devices") or {}
    dev_bits = ", ".join(
        "%s — %s, свободно на старте %s"
        % (
            device,
            info.get("name") or "устройство",
            _mib(float(info.get("free_mib") or 0.0)),
        )
        for device, info in sorted(devices.items())
    )

    rows = [
        '<div class="memrow"><b>Буферы</b><span>%s</span></div>' % esc(buf_bits),
        '<div class="memrow"><b>Слои</b><span>%s</span></div>' % esc(layer_bits),
    ]
    if dev_bits:
        rows.append('<div class="memrow"><b>Устройства</b><span>%s</span></div>' % esc(dev_bits))
    return '<div class="mem"><div class="memtiles">%s</div>%s</div>' % (
        tiles,
        "".join(rows),
    )


def _run_section(run: dict, expanded: bool = True) -> str:
    cases = run.get("cases") or []
    params = run.get("params") or {}
    s = run.get("summary") or {}
    judge_bits = ""
    mj = s.get("mean_judge_score")
    if mj is not None:
        judge_bits = " · средняя оценка судьи %.1f/10" % mj
    head = (
        '<h2>%s <span class="muted">· %s</span></h2>'
        '<div class="sub">%s · контекст %s токенов · запас на рассуждение %s · '
        "температура %s · seed %s · кейсов в наборе %s · время %.1f с%s</div>"
        % (
            esc(run.get("model")),
            esc(run.get("set_name") or run.get("set_id")),
            esc(run.get("base_url")),
            run.get("context_size", "?"),
            params.get("reasoning_allowance", "?"),
            params.get("temperature", "?"),
            params.get("seed", "?"),
            params.get("cases_in_set", "?"),
            float(run.get("seconds") or 0),
            judge_bits,
        )
    )
    body = "".join(_case_row(c, i) for i, c in enumerate(cases, 1))
    table = (
        '<table class="grid cases"><thead><tr>'
        '<th class="num">#</th><th>Кейс</th><th>Название</th>'
        "<th>Вердикт</th><th>Метрики</th>"
        "</tr></thead><tbody>%s</tbody></table>" % body
    )
    status = ""
    if run.get("status") and run["status"] != "finished":
        status = '<div class="warnline">Прогон не завершён: %s</div>' % esc(run["status"])
    return "<section>%s%s%s%s%s</section>" % (
        head,
        _memory_block(run),
        _tag_bars(run),
        status,
        table,
    )


#: Готовый CSS собирается в `build_html` из этого шаблона и текущей палитры.
#: `color-scheme` берётся из CSS-переменной, чтобы браузер не переключал скроллбар
#: на светлой теме в тёмный.
CSS_TEMPLATE = """
:root { --report-scheme: dark; color-scheme: var(--report-scheme); }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 32px 60px; background: %(BG)s; color: %(TEXT_DIM)s;
       font-family: "Segoe UI", Arial, sans-serif; font-size: 13px; line-height: 1.5; }
h1 { color: %(TEXT)s; font-size: 20px; font-weight: 600; margin: 0 0 4px; }
h2 { color: %(TEXT)s; font-size: 15px; font-weight: 600; margin: 28px 0 6px; }
.muted { color: %(TEXT_MUTED)s; font-weight: 400; }
.sub { color: %(TEXT_MUTED)s; font-size: 12px; margin-bottom: 14px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 18px 0 8px; }
.card { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-left: 3px solid %(BORDER)s;
        border-radius: 4px; padding: 10px 16px; min-width: 150px; }
.card.pass { border-left-color: %(OK)s; } .card.warn { border-left-color: %(WARN)s; }
.card.fail { border-left-color: %(FAIL)s; } .card.skip { border-left-color: %(TEXT_MUTED)s; }
.score { font-size: 22px; color: %(TEXT)s; font-weight: 600; }
.card.pass .score { color: %(OK)s; } .card.warn .score { color: %(WARN)s; }
.card.fail .score { color: %(FAIL)s; }
.of { font-size: 12px; color: %(TEXT_MUTED)s; }
.set { font-size: 12px; color: %(LABEL)s; margin-top: 4px; }
table.grid { width: 100%%; border-collapse: collapse; background: %(PANEL)s;
             border: 1px solid %(BORDER)s; border-radius: 4px; overflow: hidden; }
table.grid th { background: %(HEADER)s; color: %(TEXT_DIM)s; text-align: left;
                padding: 7px 10px; font-weight: 600; border-bottom: 1px solid %(BORDER)s;
                font-size: 12px; }
table.grid td { padding: 7px 10px; border-bottom: 1px solid %(BORDER)s; vertical-align: top; }
table.grid tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; white-space: nowrap; }
td.reason { color: %(TEXT_DIM)s; max-width: 340px; }
td.metrics { color: %(TEXT_MUTED)s; font-size: 12px; white-space: nowrap; }
tr.pass td:first-child { box-shadow: inset 3px 0 0 %(OK)s; }
tr.fail td:first-child { box-shadow: inset 3px 0 0 %(FAIL)s; }
tr.skip td:first-child { box-shadow: inset 3px 0 0 %(TEXT_MUTED)s; }
tr.stand td:first-child { box-shadow: inset 3px 0 0 %(WARN)s; }
tr.detailrow td { padding: 0 10px 10px; border-bottom: 1px solid %(BORDER)s; }
tr.detailrow:last-child td { border-bottom: none; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%%;
       margin-right: 7px; background: %(TEXT_MUTED)s; }
.dot.pass { background: %(OK)s; } .dot.fail { background: %(FAIL)s; }
.dot.skip { background: %(TEXT_MUTED)s; } .dot.stand { background: %(WARN)s; }
.tags { margin: 10px 0 16px; max-width: 620px; }
.tagrow { display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }
.tagname { width: 130px; color: %(TEXT_MUTED)s; font-size: 12px;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar { flex: 1; height: 7px; background: %(BG)s; border: 1px solid %(BORDER)s;
       border-radius: 4px; overflow: hidden; }
.bar i { display: block; height: 100%%; }
.bar i.pass { background: %(OK)s; } .bar i.warn { background: %(WARN)s; }
.bar i.fail { background: %(FAIL)s; } .bar i.skip { background: %(TEXT_MUTED)s; }
.tagnum { width: 48px; text-align: right; color: %(TEXT_MUTED)s; font-size: 12px; }
details summary { cursor: pointer; color: %(LABEL)s; font-size: 12px; }
.block { margin: 8px 0 0; }
.block b { color: %(TEXT_DIM)s; font-weight: 600; font-size: 12px; }
pre { background: %(BG)s; border: 1px solid %(BORDER)s; border-radius: 3px;
      padding: 8px 10px; margin: 4px 0 0; white-space: pre-wrap; word-break: break-word;
      font-family: Consolas, "Courier New", monospace; font-size: 12px; color: %(TEXT_DIM)s;
      max-height: 320px; overflow: auto; }
pre.dim { color: %(TEXT_MUTED)s; }
pre.bad { color: %(FAIL)s; }
.warnline { background: %(PANEL)s; border: 1px solid %(WARN)s; border-radius: 4px;
            padding: 8px 12px; color: %(WARN)s; margin: 10px 0; }
footer { margin-top: 34px; color: %(TEXT_MUTED)s; font-size: 12px;
         border-top: 1px solid %(BORDER)s; padding-top: 12px; }
.mem { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-radius: 4px;
       padding: 10px 12px; margin: 10px 0 14px; }
.mem.none { color: %(TEXT_MUTED)s; font-size: 12px; }
.memtiles { display: flex; flex-wrap: wrap; gap: 22px; margin-bottom: 8px; }
.memtile { display: flex; flex-direction: column; }
.memlabel { color: %(TEXT_MUTED)s; font-size: 11px; text-transform: uppercase;
            letter-spacing: .04em; }
.memvalue { color: %(TEXT)s; font-size: 15px; font-weight: 600; }
.memrow { display: flex; gap: 10px; font-size: 12px; color: %(TEXT_MUTED)s;
          margin-top: 3px; }
.memrow b { color: %(TEXT_DIM)s; flex: none; width: 92px; font-weight: 600; }
"""


def _build_css(theme_name: str | None = None) -> str:
    """Собрать CSS из шаблона и текущей палитры.

    Светлую тему выбираем по имени темы, а не по цвету фона. Если бы считали
    по `BG`, пришлось бы держать рядом этлонный тёмный цвет отчёта, и любой
    сдвиг палитры приложения сломал бы сравнение: на тёмной теме отчёт и так
    светлел, потому что реальный `BG` темы не совпадает со старым дефолтом.
    Имя и палитра приходят из одной темы, а значит не разойдутся.
    """
    if theme_name is None:
        try:
            from .ui import theme

            theme_name = theme.active_theme()
        except ImportError:
            theme_name = "dark"
    scheme = "light" if theme_name == "light" else "dark"
    palette = _palette(theme_name)
    css = CSS_TEMPLATE.replace("--report-scheme: dark;", "--report-scheme: %s;" % scheme)
    return css % palette


def build_html(runs: list[dict], title: str = "Отчёт о тестировании") -> str:
    """Собрать HTML-отчёт по одному или нескольким прогонам."""
    if not runs:
        raise ValueError("нет прогонов для отчёта")

    total = sum(len(r.get("cases") or []) for r in runs)
    stand = sum((r.get("summary") or {}).get("stand_errors", 0) for r in runs)
    empty = sum((r.get("summary") or {}).get("empty_answers", 0) for r in runs)

    #: Какую тему подставить в отчёт. UI-вызовы (сбор после прогона, экспорт из
    #: «Сравнения») читают `active_theme()`; Qt-вызовы без неё (`run_tests.py`)
    #: оставляют None — тогда `_palette` догадывается сам.
    theme_name = None
    try:
        from .ui import theme

        theme_name = theme.active_theme()
    except ImportError:
        pass

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s</title><style>%s</style></head><body>" % (esc(title), _build_css(theme_name)),
        "<h1>%s</h1>" % esc(title),
        '<div class="sub">Отчёт собран %s · прогонов: %d · кейсов: %d</div>'
        % (datetime.now().strftime("%d.%m.%Y %H:%M"), len(runs), total),
        '<div class="cards">%s</div>' % "".join(_summary_card(r) for r in runs),
    ]

    notes = []
    if stand:
        notes.append(
            "Сбоев стенда: %d. Эти кейсы исключены из счёта: сервер был "
            "недоступен, и вины модели здесь нет." % stand
        )
    if empty:
        notes.append(
            "Пустых ответов: %d. Причина у каждого указана отдельно: чаще всего "
            "бюджет вывода ушёл в рассуждение или рассуждение зациклилось." % empty
        )
    if notes:
        parts.append('<div class="warnline">%s</div>' % " ".join(esc(n) for n in notes))

    if len(runs) > 1:
        parts.append("<h2>Сводка по прогонам</h2>")
        parts.append(_overview_table(runs))

    for run in runs:
        parts.append(_run_section(run))

    parts.append(
        "<footer>Отчёт сформирован LLM Test Bench. Проверки выполняются "
        "автоматически по правилам кейсов; расхождение с человеческой оценкой "
        "возможно и должно разбираться вручную.</footer>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(
    runs: list[dict],
    path: str | Path,
    title: str = "Отчёт о тестировании",
) -> Path:
    """Записать отчёт в файл."""
    return write_text_atomic(path, build_html(runs, title))
