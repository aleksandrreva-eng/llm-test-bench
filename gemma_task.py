"""CLI: отдать локальной модели одну небольшую задачу.

    python gemma_task.py "текст задачи"
    python gemma_task.py -f task.md -a src/module.py -a tests/case.json
    python gemma_task.py --check "текст"      # только посчитать токены
    python gemma_task.py --list               # журнал сессий

Каждый запуск — новая сессия. История между запусками не переносится: это
не ограничение, а условие работы с локальной моделью, у которой контекст
конечен. Хочешь продолжить — сформулируй новую задачу целиком.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.local_agent import (  # noqa: E402
    BudgetExceeded,
    LocalAgent,
    ServerUnavailable,
)


def build_agent(args: argparse.Namespace) -> LocalAgent:
    cfg = AppConfig.load()
    return LocalAgent(
        base_url=args.base_url or cfg.server_base_url,
        token_cap=args.cap or cfg.agent_token_cap,
        timeout=args.timeout,
        sessions_dir=cfg.sessions_path,
    )


def read_task(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.task:
        parts.append(args.task)
    for path in args.task_file or []:
        parts.append(Path(path).read_text(encoding="utf-8"))
    return "\n\n".join(p.strip() for p in parts if p.strip())


def cmd_list(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    sessions = agent.list_sessions(limit=args.limit)
    if not sessions:
        print("Сессий пока нет: %s" % agent.sessions_dir)
        return 0
    print("Последние сессии (%s):" % agent.sessions_dir)
    for s in sessions:
        err = s.get("error") or ""
        mark = "!" if err else " "
        print(
            "%s %s  вход %6d  выход %5d  %6.1f с  %s"
            % (
                mark,
                s.get("id", "?"),
                int(s.get("prompt_tokens") or 0),
                int(s.get("completion_tokens") or 0),
                float(s.get("seconds") or 0),
                (err[:60] if err else (s.get("stop_reason") or "")),
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows по умолчанию не в UTF-8: без этого русский текст
    # в выводе превращается в кракозябры, хотя в файлах всё цело.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="Разовое задание для локальной модели (одна задача = одна сессия)"
    )
    ap.add_argument("task", nargs="?", help="текст задачи")
    ap.add_argument(
        "-f",
        "--task-file",
        action="append",
        metavar="PATH",
        help="файл с текстом задачи (можно несколько)",
    )
    ap.add_argument(
        "-a",
        "--attach",
        action="append",
        metavar="PATH",
        help="приложить файл к задаче (можно несколько)",
    )
    ap.add_argument("-s", "--system", default=None, help="заменить системную подсказку")
    ap.add_argument("--cap", type=int, default=0, help="бюджет токенов на задачу")
    ap.add_argument("--timeout", type=float, default=900.0, help="таймаут, секунды")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--base-url", default="", help="адрес сервера (по умолчанию из config.json)")
    ap.add_argument("--tag", default="", help="метка для имени файла сессии")
    ap.add_argument(
        "--check", action="store_true", help="посчитать токены и выйти, ничего не отправляя"
    )
    ap.add_argument("--no-save", action="store_true", help="не писать журнал сессии")
    ap.add_argument(
        "--reasoning", action="store_true", help="показать рассуждение модели (обычно не нужно)"
    )
    ap.add_argument("--list", action="store_true", help="показать журнал сессий")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list(args)

    task = read_task(args)
    if not task:
        ap.error("нужна задача: текстом или через -f")

    agent = build_agent(args)
    try:
        health = agent.health()
    except ServerUnavailable as exc:
        print("Сервер не отвечает: %s" % exc, file=sys.stderr)
        print("Проверь, что llama-server поднят на %s" % agent.base_url, file=sys.stderr)
        return 2

    print("сервер: %s  (%s)" % (agent.base_url, health.get("status", "?")))
    print("модель: %s" % agent.model_name)
    print(
        "контекст сервера: %d токенов, бюджет задачи: %d" % (agent.context_size, agent.token_cap)
    )

    cfg = AppConfig.load()
    try:
        session = agent.ask(
            task,
            system=args.system,
            files=args.attach,
            temperature=args.temperature
            if args.temperature is not None
            else cfg.agent_temperature,
            max_tokens=args.max_tokens if args.max_tokens is not None else cfg.agent_max_tokens,
            tag=args.tag,
            dry_run=args.check,
            save=not args.no_save,
        )
    except BudgetExceeded as exc:
        print("\nНЕ ОТПРАВЛЕНО: %s" % exc, file=sys.stderr)
        return 3

    print(session.brief())
    if args.check:
        print("\n(dry-run: запрос не отправлялся)")
        return 0

    print("\n" + "=" * 72)
    print(session.reply or "(ответ пуст)")
    print("=" * 72)
    if args.reasoning and session.reasoning:
        print("\n--- рассуждение ---")
        print(session.reasoning)
    if not session.reply:
        print(
            "\nОтвет пуст при стопе «%s»: бюджет вывода ушёл в рассуждение. "
            "Подними --max-tokens." % (session.stop_reason or "—"),
            file=sys.stderr,
        )
    if session.log_md:
        print("\nжурнал: %s" % session.log_md)
    return 0 if session.ok else 1


if __name__ == "__main__":
    sys.exit(main())
