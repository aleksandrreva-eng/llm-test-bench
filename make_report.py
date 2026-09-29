"""Сборка HTML-отчёта из уже сохранённых результатов.

    python make_report.py --latest 2                 # два последних прогона
    python make_report.py results/файл.json -o reports/отчёт.html
    python make_report.py --set chat_single          # все прогоны одного набора
    python make_report.py --all                      # все прогоны, что есть

Отчёт самодостаточен: один HTML-файл без внешних ресурсов.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.report import load_runs, write_report  # noqa: E402


def find_runs(cfg: AppConfig, set_id: str = "", limit: int = 0) -> list[Path]:
    """Файлы результатов, свежие первыми."""
    folder = cfg.results_path
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if set_id:
        files = [p for p in files if ("_%s_" % set_id) in p.name]
    if limit:
        files = files[:limit]
    return files


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = AppConfig.load()
    ap = argparse.ArgumentParser(description="HTML-отчёт по результатам прогонов")
    ap.add_argument("files", nargs="*", help="файлы результатов")
    ap.add_argument("-o", "--out", default="", help="куда записать отчёт")
    ap.add_argument("--set", dest="set_id", default="", help="взять прогоны одного набора")
    ap.add_argument("--latest", type=int, default=0, help="взять N последних прогонов")
    ap.add_argument("--all", action="store_true", help="взять все прогоны")
    ap.add_argument("--title", default="", help="заголовок отчёта")
    args = ap.parse_args(argv)

    if args.files:
        paths = [Path(p) for p in args.files]
    elif args.all or args.set_id or args.latest:
        paths = find_runs(cfg, args.set_id, args.latest)
    else:
        paths = find_runs(cfg, "", 1)

    if not paths:
        print("Не найдено ни одного файла результатов в %s" % cfg.results_path, file=sys.stderr)
        return 2

    runs = load_runs(paths)
    runs.reverse()  # в отчёте — от старого к новому

    if args.out:
        out = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = cfg.reports_path / ("отчёт_%s.html" % stamp)

    title = args.title or "Отчёт о тестировании"
    write_report(runs, out, title)

    print("Прогонов в отчёте: %d" % len(runs))
    for run in runs:
        s = run.get("summary") or {}
        print(
            "  %-42s %s из %s (%s)"
            % (
                (run.get("set_name") or run.get("set_id")),
                s.get("passed"),
                s.get("counted"),
                "—" if s.get("score") is None else "%.0f%%" % (s["score"] * 100),
            )
        )
    print("Отчёт: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
