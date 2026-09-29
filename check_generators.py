"""Проверка генераторов промптов: длины, иголка, чистота «стога».

Запускать перед прогоном наборов speed и context_long.

    python check_generators.py

Что проверяется:

1. «Стог» на месте и чист: без цифр, без латиницы, без повторов предложений.
   Цифра в наполнителе — прямая угроза тесту: модель найдёт её вместо кода
   доступа, и провал будет выглядеть как успех.
2. Каждый кейс speed и context_long собирается и попадает в цель по длине.
3. Иголка присутствует в тексте ровно один раз и не встречается в самом
   наполнителе.
4. Позиция иголки соответствует заявленной в кейсе.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench.generators import PromptBuilder, load_paragraphs  # noqa: E402
from llmtestbench.local_agent import (  # noqa: E402
    TOKENIZE_FAIL_LIMIT,
    LocalAgent,
    ServerUnavailable,
)
from llmtestbench.testsets import load_test_set  # noqa: E402

ROOT = Path(__file__).resolve().parent
HAYSTACK = ROOT / "tests" / "context_long" / "haystack"

ok_count = 0
fail_count = 0
skip_count = 0


def ok(msg: str) -> None:
    global ok_count
    ok_count += 1
    print("  ок   %s" % msg)


def bad(msg: str) -> None:
    global fail_count
    fail_count += 1
    print("  ПЛОХО %s" % msg)


def skip(msg: str) -> None:
    """Не провал и не успех: среда не дала измерить.

    Пропуск не идёт в знаменатель — как `passed=None` у кейса с судьёй.
    """
    global skip_count
    skip_count += 1
    print("  ПРОПУСК %s" % msg)


def check_pool(paragraphs: list[str]) -> None:
    print("\n[1] Текст-наполнитель")
    if not paragraphs:
        bad("пул пуст: %s" % HAYSTACK)
        return
    text = "\n\n".join(paragraphs)
    ok("абзацев %d, символов %d" % (len(paragraphs), len(text)))

    digits = re.findall(r"[0-9]+", text)
    if digits:
        bad("в наполнителе есть цифры: %s" % digits[:8])
    else:
        ok("цифр нет")

    if re.search(r"[A-Za-z]{2,}", text):
        bad("в наполнителе есть латиница")
    else:
        ok("латиницы нет")

    if "КОВАЛЧУК" in text.upper():
        bad("в наполнителе уже есть иголка — тест перестанет что-либо проверять")
    else:
        ok("иголки в наполнителе нет")

    sents = [
        s.lower().strip(" .,!?;:—-")
        for p in paragraphs
        for s in re.split(r"(?<=[.!?])\s+", p)
        if s.strip()
    ]
    dupes = len(sents) - len(set(sents))
    if dupes:
        bad("повторяющихся предложений: %d" % dupes)
    else:
        ok("повторов предложений нет")


def check_needle(case, prompt: str) -> None:
    """Одна иголка: ровно один раз и на заявленной доле."""
    needle = str(case.extra.get("needle") or "")
    n_times = prompt.count(needle)
    if n_times == 1:
        ok("%s: иголка ровно один раз" % case.id)
    else:
        bad("%s: иголка встречается %d раз" % (case.id, n_times))
    pos = float(case.extra.get("position", 0.5))
    at = prompt.find(needle) / max(1, len(prompt))
    if abs(at - pos) <= 0.15:
        ok("%s: иголка на доле %.2f (ждали %.2f)" % (case.id, at, pos))
    else:
        bad("%s: иголка на доле %.2f, ждали %.2f" % (case.id, at, pos))
    if not prompt.rstrip().endswith("."):
        bad("%s: вопрос не в конце промпта" % case.id)


def check_needles_multi(case, prompt: str, needles: list) -> None:
    """Несколько иголок: каждая ровно один раз, и все на своих местах.

    Отдельно проверяем, что тексты иголок не пересекаются: если один код —
    подстрока другого, вопрос «какой код про архив» перестаёт иметь один
    правильный ответ.
    """
    texts = [str(n.get("text") or "") for n in needles if isinstance(n, dict)]
    for text in texts:
        n_times = prompt.count(text)
        if n_times == 1:
            ok("%s: иголка %s ровно один раз" % (case.id, text))
        else:
            bad("%s: иголка %s встречается %d раз" % (case.id, text, n_times))
    for i, first in enumerate(texts):
        for second in texts[i + 1 :]:
            if first and second and (first in second or second in first):
                bad("%s: иголки %s и %s перекрываются" % (case.id, first, second))
    for item in needles:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        pos = float(item.get("position", 0.5))
        at = prompt.find(text) / max(1, len(prompt))
        if abs(at - pos) <= 0.15:
            ok("%s: %s на доле %.2f (ждали %.2f)" % (case.id, text, at, pos))
        else:
            bad("%s: %s на доле %.2f, ждали %.2f" % (case.id, text, at, pos))
    if not prompt.rstrip().endswith("."):
        bad("%s: вопрос не в конце промпта" % case.id)


def check_tokenize_fallback() -> None:
    """Счёт длины не должен сдаваться после первого сбоя.

    Проверка без сервера: адрес заведомо мёртвый, `/tokenize` не ответит.
    После одного сбоя точный счёт ещё в силе — иначе одиночная заминка
    сервера переводила бы весь оставшийся прогон на грубую оценку, и промпт
    собирался бы наполовину точным счётом, наполовину оценкой.
    """
    print("\n[4] Счёт токенов при отказе /tokenize")
    dead = LocalAgent(base_url="http://127.0.0.1:9", sessions_dir=ROOT / "sessions")
    if not dead.tokenize_exact:
        bad("счёт помечен оценочным ещё до первого запроса")
        return
    if dead.count_tokens("привет") <= 0:
        bad("длина пустого текста посчитана как ноль")
    else:
        ok("после первого сбоя длина всё ещё считается")
    if dead.tokenize_exact:
        ok("после первого сбоя точный счёт в силе")
    else:
        bad(
            "точный счёт отключён после первого сбоя — одиночная заминка сервера меняет весь прогон"
        )
    for _ in range(TOKENIZE_FAIL_LIMIT):
        dead.count_tokens("привет")
    if dead.tokenize_exact:
        bad(f"точный счёт в силе и после {TOKENIZE_FAIL_LIMIT} сбоев подряд")
    else:
        ok(f"после {TOKENIZE_FAIL_LIMIT} сбоев подряд счёт переключён на оценку")


def main() -> int:
    global ok_count, fail_count

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = AppConfig.load()
    paragraphs = load_paragraphs(HAYSTACK)
    check_pool(paragraphs)
    if not paragraphs:
        return 1

    agent = LocalAgent(
        base_url=cfg.server_base_url,
        sessions_dir=cfg.sessions_path,
    )
    try:
        agent.health()
        print("\nСервер: %s, модель %s" % (agent.base_url, agent.model_name))
        count = agent.count_tokens
        exact = True
    except ServerUnavailable:
        print("\nСервер недоступен — считаю длину оценочно (точность ниже).")
        count = None
        exact = False

    builder = PromptBuilder(paragraphs, count_tokens=count, seed=42)

    print("\n[2] Промпты по кейсам")
    total_cases = 0
    for set_name in ("speed", "context_long"):
        test_set = load_test_set(ROOT / "tests" / set_name)
        if not test_set.ok:
            bad("набор %s не загрузился: %s" % (set_name, test_set.errors))
            continue
        for case in test_set.cases:
            total_cases += 1
            mode_before = agent.tokenize_exact
            try:
                prompt = builder.build(case)
            except ValueError as exc:
                bad("%s: %s" % (case.id, exc))
                continue
            if mode_before != agent.tokenize_exact:
                # Счёт переключился посреди сборки: часть промпта набрана по
                # точному счёту, часть — по оценке. Длина такого промпта ничего
                # не говорит о генераторе, это состояние сервера.
                skip(
                    f"{case.id}: /tokenize отказал во время сборки — "
                    "длину не проверяю (кейс не засчитан ни в успех, ни в провал)"
                )
                continue
            got = builder.count(prompt)
            target = int(case.target_tokens or 0)
            note = builder.check(prompt, target)
            mark = "ок   " if not note else "ПЛОХО"
            if note:
                fail_count += 1
            else:
                ok_count += 1
            print(
                "  %s %-16s цель %6d → %6d токенов, %7d символов%s"
                % (mark, case.id, target, got, len(prompt), ("  — " + note) if note else "")
            )

            if case.generator == "needle_in_haystack":
                needles = case.extra.get("needles")
                if isinstance(needles, list) and needles:
                    check_needles_multi(case, prompt, needles)
                else:
                    check_needle(case, prompt)

    print("\n[3] Совместимость генераторов с загрузчиком наборов")
    for set_name in ("speed", "context_long"):
        test_set = load_test_set(ROOT / "tests" / set_name)
        gens = {c.generator for c in test_set.cases}
        if len(gens) == 1 and "" not in gens:
            ok("%s: у всех кейсов генератор «%s»" % (set_name, gens.pop()))
        else:
            bad("%s: генераторы не заданы или разные: %s" % (set_name, gens))

    check_tokenize_fallback()

    print("\n" + "=" * 62)
    if skip_count:
        print(f"Пропущено: {skip_count} — сервер отказал во время сборки промпта")
    print(f"Пройдено: {ok_count}   Провалено: {fail_count}")
    if not exact or not agent.tokenize_exact:
        print("Длина считалась оценочно: /tokenize недоступен, точность ниже.")
    print(f"Кейсов проверено: {total_cases}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
