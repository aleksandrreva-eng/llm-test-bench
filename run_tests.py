"""Прогон тестового набора против поднятого llama-server.

    python run_tests.py --set chat_single
    python run_tests.py --set chat_single --limit 3 --tags format
    python run_tests.py --set chat_single,chat_multi --runs 2
    python run_tests.py --set speed --reasoning-allowance 0
    python run_tests.py --set chat_single --dry-run      # собрать промпты и выйти

Сервер не поднимается и не останавливается: скрипт тестирует то, что уже
запущено на указанном адресе. Результат каждого прогона пишется в
`results/<модель>_<набор>_<дата>.json`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.local_agent import LocalAgent, ServerUnavailable  # noqa: E402
from llmtestbench.logging_setup import setup_logging  # noqa: E402
from llmtestbench.runner import (  # noqa: E402
    DEFAULT_REASONING_ALLOWANCE,
    CaseResult,
    TestRunner,
)
from llmtestbench.server_cmd import describe_server  # noqa: E402
from llmtestbench.testsets import discover_test_sets, load_test_set  # noqa: E402


def parse_sets(cfg: AppConfig, wanted: str) -> list:
    """Найти наборы по именам. `all` — все, что есть в tests/."""
    root = cfg.tests_path
    if wanted.strip().lower() in ("all", "*"):
        return discover_test_sets(root)
    sets = []
    for name in [x.strip() for x in wanted.split(",") if x.strip()]:
        directory = root / name
        if not directory.is_dir():
            raise SystemExit("набор не найден: %s" % directory)
        sets.append(load_test_set(directory))
    return sets


def print_run(run, verbose: bool = True) -> None:
    if verbose:
        print()
        for case in run.cases:
            print("  " + case.brief())
    s = run.summary
    print()
    print("-" * 74)
    print("Набор: %s (%s)   модель: %s" % (run.set_id, run.set_version, run.model))
    print(
        "Всего кейсов: %d   прошло: %d   провалено: %d   пропущено: %d"
        % (s["total"], s["passed"], s["failed"], s["skipped"])
    )
    if s["stand_errors"]:
        print("Сбоев стенда: %d (в счёт не идут)" % s["stand_errors"])
    if s["empty_answers"]:
        print("Пустых ответов: %d" % s["empty_answers"])
    score = s["score"]
    if score is not None:
        print("Итог: %.0f%% (%d из %d)" % (score * 100, s["passed"], s["counted"]))
    else:
        # Набор без критериев проверки (например speed). Здесь важен не вердикт,
        # а метрики — иначе строка «нет проверенных кейсов» выглядит как поломка.
        print("Итог: набор без проверок — смотри метрики")
    # Средние метрики печатаются всегда, когда они есть, а не только у набора
    # без счёта. У набора `speed` часть кейсов проверяется (длина ответа), и
    # счёт у него теперь появляется — но ходят в этот набор не за процентом,
    # а за скоростями. Раньше строка стояла в ветке «без проверок» и пропадала
    # ровно тогда, когда метрики нужнее всего.
    ttft = [c.ttft_ms for c in run.cases if c.ttft_ms]
    pre = [c.prompt_tokens_per_sec for c in run.cases if c.prompt_tokens_per_sec]
    gen = [c.tokens_per_sec for c in run.cases if c.tokens_per_sec]
    if ttft:
        print(
            "Средние метрики: TTFT %.0f мс, префилл %.0f t/s, генерация %.1f t/s"
            % (
                sum(ttft) / len(ttft),
                (sum(pre) / len(pre)) if pre else 0.0,
                (sum(gen) / len(gen)) if gen else 0.0,
            )
        )
    print("Время: %.1f с" % run.seconds)
    if run.status != "finished":
        print("Статус: %s" % run.status)
    if run.path:
        print("Результат: %s" % run.path)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = AppConfig.load()
    # Лог — в файл, без консоли: с пользователем здесь говорит stdout, и
    # дублировать те же строки в stderr смысла нет. Файл нужен для разбора
    # того, что осталось за кадром (неудачная запись в SQLite, разбор лога).
    setup_logging(cfg.logs_path, cfg.log_level, console=False)
    ap = argparse.ArgumentParser(description="Прогон тестового набора на llama-server")
    ap.add_argument(
        "--set", dest="sets", required=True, help="имя набора, несколько через запятую, или all"
    )
    ap.add_argument("--runs", type=int, default=1, help="повторов каждого кейса")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N кейсов")
    ap.add_argument("--tags", default="", help="фильтр по тегам, через запятую")
    ap.add_argument(
        "--reasoning-allowance",
        type=int,
        default=DEFAULT_REASONING_ALLOWANCE,
        help="запас токенов на рассуждение сверх бюджета кейса",
    )
    ap.add_argument("--base-url", default="", help="адрес сервера (по умолчанию из config.json)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument(
        "--server-command",
        default="",
        help=(
            "команда, которой поднят сервер — попадает в запись прогона. "
            "Консоль сервером не управляет, поэтому команду берут извне: без "
            "неё два прогона с разными флагами запуска неотличимы"
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="собрать промпты и посчитать токены, ничего не отправляя",
    )
    ap.add_argument("--report", action="store_true", help="сделать HTML-отчёт сразу после прогона")
    ap.add_argument("--quiet", action="store_true", help="не печатать построчный ход")
    args = ap.parse_args(argv)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    sets = parse_sets(cfg, args.sets)
    bad = [s for s in sets if not s.ok]
    if bad:
        for s in bad:
            print("Набор %s не загрузился: %s" % (s.id, "; ".join(s.errors)), file=sys.stderr)
        return 2

    agent = LocalAgent(
        base_url=args.base_url or cfg.server_base_url,
        timeout=args.timeout,
        sessions_dir=cfg.sessions_path,
    )
    try:
        agent.health()
    except ServerUnavailable as exc:
        print("Сервер не отвечает: %s" % exc, file=sys.stderr)
        return 2

    print("Сервер: %s" % agent.base_url)
    print("Модель: %s" % agent.model_name)
    print("Контекст: %d токенов" % agent.context_size)
    print("Запас на рассуждение: %d токенов" % args.reasoning_allowance)
    if args.server_command:
        # Приложение эту команду не применяло — она пришла от человека.
        # Поэтому «заявлена», а не «запущено»: в записи прогона она встанет
        # с признаком applied=False и своей причиной.
        print(f"Команда сервера (заявлена снаружи): {args.server_command}")

    runner = TestRunner(
        agent,
        cfg,
        reasoning_allowance=args.reasoning_allowance,
        server_provider=lambda: describe_server(args.server_command),
    )

    if args.dry_run:
        for test_set in sets:
            cases = test_set.filtered(tags=tags, limit=args.limit)
            print("\n%s: кейсов %d" % (test_set.id, len(cases)))
            for case in cases:
                try:
                    messages, prompt = runner.messages_for(case)
                except (RuntimeError, ValueError) as exc:
                    print("  ПЛОХО %-22s %s" % (case.id, exc))
                    continue
                tokens = sum(agent.count_tokens(m["content"]) for m in messages)
                budget, effective = runner.effective_max_tokens(
                    case, test_set.default_params or {}
                )
                print(
                    "  ок   %-22s сообщений %d, промпт %6d ток., бюджет %d+%d"
                    % (case.id, len(messages), tokens, budget, args.reasoning_allowance)
                )
        return 0

    failures = 0
    for test_set in sets:

        def on_progress(result: CaseResult, done: int, total: int) -> None:
            if not args.quiet:
                print("  [%d/%d] %s" % (done, total, result.brief()))

        run = runner.run_set(
            test_set,
            runs=args.runs,
            limit=args.limit,
            tags=tags,
            progress=None if args.quiet else on_progress,
        )
        runner.save(run)
        print_run(run, verbose=False)
        if run.params.get("server_busy"):
            # В консоль лог не пишется (setup_logging с console=False), поэтому
            # о недостоверных замерах говорим прямо здесь.
            print(
                f"\nВНИМАНИЕ: сервер был занят посторонним запросом "
                f"({run.params['server_busy']}) — скорости этого прогона "
                f"показывают скорость очереди, а не модели."
            )
        if args.report:
            from llmtestbench.report import write_report

            html_path = cfg.reports_path / ("%s.html" % run.run_id)
            write_report(
                [run.as_dict()],
                html_path,
                "Тестирование %s — %s" % (run.model, run.set_name or run.set_id),
            )
            print("Отчёт: %s" % html_path)
        if run.summary["failed"] or run.status != "finished":
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
