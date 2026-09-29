"""Генераторы промптов для наборов, где текст не хранится, а собирается.

Двум наборам нужен промпт заданной длины:

* `speed` — ровный префилл на 100/500/1000/4000 токенов. Содержание не важно,
  важна длина: иначе замеры скорости несравнимы между собой.
* `context_long` — «иголка в стоге»: длинный нейтральный текст, в который
  подложен код доступа, и вопрос о нём. Иголок может быть несколько
  (`needles`) — так проверяется, не цепляется ли модель за первый попавшийся
  похожий код вместо нужного.

Текст-наполнитель берётся из `tests/context_long/haystack/` — это абзацы,
сгенерированные локальной моделью отдельными сессиями (см. `build_haystack.py`).
Готовый текст из интернета тут не годится: в нём попадаются цифры и
конструкции вида «СЛОВО-1234», а они мешают проверке — модель находит не ту
строку и тест врёт.

Длина набирается по-настоящему, а не «примерно»: сначала абзацами целиком,
затем предложениями, затем словами. Считать токены можно как оценочно, так и
точно — через `count_tokens` сервера; по умолчанию второе, если передать
функцию подсчёта.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Callable, Iterable

#: Инструкция, которую получает модель в наборе speed. Одна и та же для всех
#: длин — иначе менялось бы не только время префилла, но и задача.
SPEED_INSTRUCTION = (
    "Ниже приведён фрагмент текста. Прочитай его и ответь одним предложением, о чём он."
)

#: Как выглядит предложение с иголкой. Код доступа должен быть узнаваем и
#: единственен — иначе проверка «нашла ли модель иголку» ничего не значит.
NEEDLE_TEMPLATE = "Служебный код доступа к архиву в этом документе — {needle}."


def split_sentences(text: str) -> list[str]:
    """Разбить текст на предложения. Простое правило, но его хватает."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def estimate_tokens(text: str) -> int:
    """Оценка с завышением: около двух символов на токен для русского."""
    return len(text) // 2 + 1


def load_paragraphs(path: str | Path) -> list[str]:
    """Прочитать «стог»: все абзацы из файла или папки с group_*.txt."""
    p = Path(path)
    files: Iterable[Path]
    if p.is_dir():
        files = sorted(p.glob("*.txt"))
    else:
        files = [p]
    paragraphs: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        paragraphs += [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]
    return paragraphs


class PromptBuilder:
    """Сборка промптов заданной длины из пула абзацев.

    `count_tokens` — функция подсчёта. Если её не передать, длина считается
    оценочно; для настоящих замеров лучше передать точную (из LocalAgent).
    """

    def __init__(
        self,
        paragraphs: list[str],
        count_tokens: Callable[[str], int] | None = None,
        seed: int = 42,
    ) -> None:
        if not paragraphs:
            raise ValueError("нет текста-наполнителя: соберите его через build_haystack.py")
        self.paragraphs = list(paragraphs)
        self.count = count_tokens or estimate_tokens
        self.seed = int(seed)

    # ------------------------------------------------------------------

    def _assemble(self, target: int) -> tuple[str, int]:
        """Набрать текст ровно на `target` токенов.

        Порядок абзацев перемешивается от запуска к запуску с одним и тем же
        зерном — так тест воспроизводим, но иголка не всегда попадает в
        одинаковое окружение.
        """
        if target <= 0:
            return "", 0
        rng = random.Random(self.seed)
        pool = list(self.paragraphs)
        rng.shuffle(pool)

        out: list[str] = []
        total = 0
        idx = 0
        laps = 0
        while total < target and laps < 500:
            if idx >= len(pool):
                rng.shuffle(pool)
                idx = 0
                laps += 1
                continue
            para = pool[idx]
            idx += 1
            n = self.count(para) + 1
            if total + n <= target:
                out.append(para)
                total += n
                continue

            # Абзац не влезает целиком — добираем предложениями, затем словами,
            # чтобы не перелететь через цель на целый абзац.
            truncated = False
            tail: list[str] = []
            for sent in split_sentences(para):
                ns = self.count(sent) + 1
                if total + ns <= target:
                    tail.append(sent)
                    total += ns
                    continue
                words: list[str] = []
                for word in sent.split():
                    nw = self.count(word) + 1
                    if total + nw > target:
                        break
                    words.append(word)
                    total += nw
                if words:
                    tail.append(" ".join(words))
                truncated = True
                break
            if tail:
                out.append(" ".join(tail))
            if truncated:
                break
        return "\n\n".join(out), total

    # ------------------------------------------------------------------

    def speed(self, target_tokens: int) -> str:
        """Промпт ровно на `target_tokens` токенов для замера скорости."""
        reserve = self.count(SPEED_INSTRUCTION) + 8
        body, _ = self._assemble(max(16, int(target_tokens) - reserve))
        return "%s\n\n%s" % (body, SPEED_INSTRUCTION)

    def needle(
        self,
        target_tokens: int,
        needle: str,
        question: str,
        position: float = 0.5,
        template: str = NEEDLE_TEMPLATE,
    ) -> str:
        """«Иголка в стоге»: код доступа внутри длинного текста плюс вопрос.

        `position` — на какой доле длины спрятать иголку (0 — начало,
        1 — конец). Проверять стоит не только середину: модели по-разному
        теряют начало и конец контекста.
        """
        needle_sentence = template.format(needle=needle)
        reserve = self.count(needle_sentence) + self.count(question) + 16
        body, _ = self._assemble(max(64, int(target_tokens) - reserve))

        parts = [x for x in body.split("\n\n") if x.strip()]
        if not parts:
            return "%s\n\n%s" % (needle_sentence, question)
        at = int(round(len(parts) * max(0.0, min(1.0, position))))
        at = max(0, min(len(parts), at))
        parts.insert(at, needle_sentence)
        return "%s\n\n%s" % ("\n\n".join(parts), question)

    # ------------------------------------------------------------------

    def needle_multi(
        self,
        target_tokens: int,
        items: list[dict],
        question: str,
        position: float = 0.5,
        template: str = NEEDLE_TEMPLATE,
    ) -> str:
        """Несколько «иголок» в одном стоге.

        Нужно для теста на дистрактор: в тексте лежат два похожих кода, а вопрос
        спрашивает только про один. Модель, которая цепляется за первый
        попавшийся код, отвечает неверно — одноигольный тест этого не покажет,
        потому что там искать больше нечего.

        `items` — список словарей `{"text": ..., "position": ..., "template": ...}`.
        """
        marks: list[tuple[str, float]] = []
        for item in items:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            tpl = str(item.get("template") or template)
            marks.append((tpl.format(needle=text), float(item.get("position", position))))
        if not marks:
            raise ValueError("needle_multi: не передано ни одной иголки")

        reserve = sum(self.count(s) for s, _ in marks) + self.count(question) + 16
        body, _ = self._assemble(max(64, int(target_tokens) - reserve))
        parts = [x for x in body.split("\n\n") if x.strip()]
        if not parts:
            return "%s\n\n%s" % ("\n\n".join(s for s, _ in marks), question)

        # Индексы считаем по исходному списку, а вставляем с конца: вставка
        # первой иголки сдвинула бы позиции всех следующих.
        places = sorted(
            ((int(round(len(parts) * max(0.0, min(1.0, pos)))), sent) for sent, pos in marks),
            key=lambda pair: pair[0],
            reverse=True,
        )
        for at, sent in places:
            parts.insert(max(0, min(len(parts), at)), sent)
        return "%s\n\n%s" % ("\n\n".join(parts), question)

    # ------------------------------------------------------------------

    def build(self, case) -> str:
        """Собрать промпт по описанию кейса (TestCase из testsets)."""
        gen = getattr(case, "generator", "") or ""
        target = int(getattr(case, "target_tokens", 0) or 0)
        extra = getattr(case, "extra", None) or {}

        if gen == "lorem_by_tokens":
            return self.speed(target)
        if gen == "needle_in_haystack":
            question = str(extra.get("question") or "").strip()
            needles = extra.get("needles")
            if isinstance(needles, list) and needles:
                if not question:
                    raise ValueError(
                        "кейс %s: для needle_in_haystack нужны поля needles и question"
                        % getattr(case, "id", "?")
                    )
                return self.needle_multi(
                    target,
                    needles,
                    question,
                    float(extra.get("position", 0.5)),
                    str(extra.get("template") or NEEDLE_TEMPLATE),
                )
            needle = str(extra.get("needle") or "").strip()
            if not needle or not question:
                raise ValueError(
                    "кейс %s: для needle_in_haystack нужны поля needle и question"
                    % getattr(case, "id", "?")
                )
            position = float(extra.get("position", 0.5))
            template = str(extra.get("template") or NEEDLE_TEMPLATE)
            return self.needle(target, needle, question, position, template)
        raise ValueError("кейс %s: неизвестный генератор «%s»" % (getattr(case, "id", "?"), gen))

    def check(self, text: str, target_tokens: int, tolerance: float = 0.25) -> str:
        """Замечание к собранному тексту или пустая строка."""
        got = self.count(text)
        if target_tokens <= 0:
            return ""
        low = target_tokens * (1 - tolerance)
        high = target_tokens * (1 + tolerance)
        if got < low:
            return "текст короче цели: %d токенов вместо %d" % (got, target_tokens)
        if got > high:
            return "текст длиннее цели: %d токенов вместо %d" % (got, target_tokens)
        return ""
