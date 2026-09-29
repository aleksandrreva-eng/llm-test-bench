"""Сборка «стога» для набора context_long: тексты-наполнители от локальной модели.

Зачем отдельный скрипт. Набору context_long нужен длинный нейтральный текст,
в который подкладывается «иголка» — код доступа. Взять для этого что-нибудь
готовое неудобно: в реальном тексте попадаются цифры и похожие на код
конструкции, а они мешают проверке. Поэтому наполнитель генерируется.

Каждая группа тем — отдельная сессия субагента. Это не формальность: если
попросить всё сразу, модель уходит в цикл самопроверки и вместо текста
начинает считать слова и спорить сама с собой. Проверено.

    python build_haystack.py            # дособрать недостающие группы
    python build_haystack.py --force    # пересобрать всё заново
    python build_haystack.py --check    # только проверить уже собранное
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.local_agent import (  # noqa: E402
    BudgetExceeded,
    LocalAgent,
    ServerUnavailable,
)

ROOT = Path(__file__).resolve().parent
TOPICS_FILE = ROOT / "agent_tasks" / "haystack_topics.json"
HAYSTACK_DIR = ROOT / "tests" / "context_long" / "haystack"

TASK_TEMPLATE = """Напиши {n} абзацев связного русского текста, по одному абзацу на тему:
{topics}.

Правила:

1. Только текст абзацами. Никаких заголовков, нумерации, маркеров списков,
   жирного шрифта и вообще никакой разметки.
2. Абзацы отделяются друг от друга ровно одной пустой строкой.
3. В каждом абзаце от {smin} до {smax} предложений.
4. Ни одной арабской цифры в тексте и ни одного сочетания вида «СЛОВО-1234».
   Обычные слова русского языка ограничивать не нужно.
5. Ни одно предложение не должно повторяться.
6. Пиши сразу готовый текст. Не считай слова, не проверяй себя, не объясняй,
   что делаешь, — в ответе должен быть только текст."""


def split_paragraphs(text: str) -> list[str]:
    """Разбить ответ на абзацы по пустым строкам."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def count_sentences(paragraph: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()])


def check_paragraph(p: str, smin: int, smax: int) -> list[str]:
    """Замечания к абзацу. Пустой список — абзац годится."""
    problems: list[str] = []
    if re.search(r"[0-9]", p):
        problems.append("есть цифры")
    if re.search(r"[A-Za-z]{2,}", p):
        problems.append("есть латиница")
    if len(p) < 150:
        problems.append("слишком короткий (%d символов)" % len(p))
    n = count_sentences(p)
    if n < smin - 1 or n > smax + 2:
        problems.append("предложений %d, ждали %d–%d" % (n, smin, smax))
    return problems


def sentences_of(text: str) -> list[str]:
    out: list[str] = []
    for p in split_paragraphs(text):
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
    return out


def build_group(
    agent: LocalAgent, group: list[str], index: int, smin: int, smax: int, force: bool
) -> dict:
    """Собрать одну группу абзацев отдельной сессией."""
    out_path = HAYSTACK_DIR / ("group_%02d.txt" % index)
    if out_path.is_file() and not force:
        text = out_path.read_text(encoding="utf-8")
        return {
            "group": index,
            "path": str(out_path),
            "skipped": True,
            "paragraphs": len(split_paragraphs(text)),
            "chars": len(text),
        }

    task = TASK_TEMPLATE.format(
        n=len(group),
        topics=", ".join(group),
        smin=smin,
        smax=smax,
    )
    session = agent.ask(
        task,
        tag="стог-%02d" % index,
        max_tokens=9000,
        temperature=0.4,
    )
    paragraphs = split_paragraphs(session.reply)
    problems: list[str] = []
    good: list[str] = []
    for i, p in enumerate(paragraphs, 1):
        bad = check_paragraph(p, smin, smax)
        if bad:
            problems.append("абзац %d: %s" % (i, "; ".join(bad)))
        else:
            good.append(p)

    if good:
        HAYSTACK_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n\n".join(good) + "\n", encoding="utf-8")

    return {
        "group": index,
        "path": str(out_path),
        "session": session.id,
        "paragraphs": len(good),
        "rejected": len(paragraphs) - len(good),
        "chars": sum(len(p) for p in good),
        "tokens_in": session.prompt_tokens,
        "tokens_out": session.completion_tokens,
        "seconds": session.seconds,
        "stop": session.stop_reason,
        "problems": problems,
    }


def collect() -> tuple[str, list[str]]:
    """Склеить все собранные группы в один стог."""
    parts: list[str] = []
    used: list[str] = []
    for path in sorted(HAYSTACK_DIR.glob("group_*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            parts.append(text)
            used.append(path.name)
    return "\n\n".join(parts), used


def report_check() -> int:
    """Проверить собранное: цифры, латиница, дубли предложений."""
    text, used = collect()
    if not text:
        print("Стог пуст: %s" % HAYSTACK_DIR)
        return 1
    paragraphs = split_paragraphs(text)
    sents = sentences_of(text)
    seen: dict[str, int] = {}
    for s in sents:
        key = s.lower().strip(" .,!?;:—")
        seen[key] = seen.get(key, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}

    print("Файлов: %d (%s)" % (len(used), ", ".join(used)))
    print("Абзацев: %d, предложений: %d, символов: %d" % (len(paragraphs), len(sents), len(text)))
    print("Цифры: %s" % (re.findall(r"[0-9]+", text)[:8] or "нет"))
    print("Латиница: %s" % ("есть" if re.search(r"[A-Za-z]{2,}", text) else "нет"))
    print("Иголка в стоге: %s" % ("ЕСТЬ — плохо" if "КОВАЛЧУК" in text.upper() else "нет"))
    print("Повторяющихся предложений: %d" % len(dups))
    for k, v in list(dups.items())[:5]:
        print("   ×%d %s" % (v, k[:70]))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Сборка «стога» текстов от локальной модели")
    ap.add_argument("--force", action="store_true", help="пересобрать все группы")
    ap.add_argument("--check", action="store_true", help="только проверить собранное")
    ap.add_argument("--groups", type=int, default=0, help="ограничить число групп")
    args = ap.parse_args(argv)

    if args.check:
        return report_check()

    spec = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    groups = spec["groups"]
    if args.groups:
        groups = groups[: args.groups]
    smin, smax = spec.get("sentences_per_paragraph", [6, 9])

    cfg = AppConfig.load()
    agent = LocalAgent(
        base_url=cfg.server_base_url,
        token_cap=cfg.agent_token_cap,
        sessions_dir=cfg.sessions_path,
    )
    try:
        agent.health()
    except ServerUnavailable as exc:
        print("Сервер не отвечает: %s" % exc, file=sys.stderr)
        return 2
    print("Модель: %s, бюджет %d токенов на группу" % (agent.model_name, agent.token_cap))

    results = []
    for i, group in enumerate(groups, 1):
        print("\n[%d/%d] %s" % (i, len(groups), ", ".join(group[:2]) + "…"))
        try:
            r = build_group(agent, group, i, smin, smax, args.force)
        except BudgetExceeded as exc:
            print("   бюджет: %s" % exc)
            continue
        except ServerUnavailable as exc:
            print("   сервер: %s" % exc)
            break
        if r.get("skipped"):
            print("   уже собран: %s" % r["path"])
        else:
            print(
                "   абзацев %d (отброшено %d), %d символов, %.0f с, стоп %s"
                % (
                    r["paragraphs"],
                    r.get("rejected", 0),
                    r["chars"],
                    r.get("seconds", 0),
                    r.get("stop", "?"),
                )
            )
            for p in r.get("problems", [])[:3]:
                print("   ! %s" % p)
        results.append(r)

    print("\n" + "=" * 60)
    report_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
