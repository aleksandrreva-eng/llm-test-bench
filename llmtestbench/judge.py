"""LLM-as-judge: оценка ответа отдельной моделью-судьёй (раздел 10.10 / 11.8 ТЗ).

Судья — это тот же llama-server, но с другим промптом (и, опционально, на
другом адресе, если поднят второй сервер под судью). Стенд собирает задание,
ответ тестируемой модели и (при наличии) эталон, отдаёт это судье и просит
вернуть число от 0 до 10. Число сравнивается с порогом `min_score` из
`scoring` набора — это и есть вердикт кейса.

Почему судья живёт отдельно от `checks.py`:
- `checks.py` — это чистые функции `ответ → вердикт`, без сети.
- Судья сам делает сетевой запрос, поэтому логика вынесена сюда, а в
  `runner.py` она подключается на уровне набора (`TestSet.scoring`), а не
  отдельного кейса. Так совпадает со схемой: в `manifest.json` блок `scoring`
  описывает судью один раз на весь набор, а `expected` у кейсов — это
  запасной (keyword) критерий на случай, если судья недоступен.

Если судья не вернул валидное число или сервер упал — это не провал модели.
Возвращается `score=None` и текст ошибки, а `runner` откатывается на
keyword-проверку из `expected`.

Вердикты кэшируются (`JudgeCache`). Причина простая: судья — это ещё один
запрос к модели, и повторный прогон того же набора с теми же ответами платил
бы токенами за уже полученные оценки. Ключ включает всё, что влияет на
вердикт, поэтому смена промпта судьи или модели-судьи старые записи просто
перестаёт находить.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_io import write_json_atomic
from .local_agent import LocalAgent

#: Ищем первое вещественное число в ответе судьи. Судьи часто пишут «8/10»,
#: «Оценка: 7» или «7.5 баллов» — регулярка берёт первое подходящее.
_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)")

#: Где лежит кэш вердиктов. Как и у кэша метаданных GGUF — в `.cache` рядом
#: с приложением, а не в CWD: в собранном .exe рабочая папка произвольная.
CACHE_DIR = ".cache"
CACHE_FILE = "judge_cache.json"
#: Версия схемы ключа. Меняется вместе с составом ключа: старые записи после
#: этого не находятся и не мешают, а файл перезаписывается сам.
CACHE_VERSION = 1
#: Потолок записей. Кэш — удобство, а не архив: при переполнении выкидываем
#: самые старые (dict хранит порядок вставки).
CACHE_MAX_ENTRIES = 5000

#: Системная роль судьи. Короткая и жёсткая: судья не должен рассуждать
#: вслух, иначе парсер утонет в тексте вокруг цифры.
JUDGE_SYSTEM = (
    "Ты — строгий и объективный судья качества ответов языковой модели. "
    "Оценивай только по содержанию, а не по длине или вежливости. "
    "Возвращай единственное число — свою оценку, без пояснений и без текста."
)


@dataclass
class JudgeResult:
    """Итог работы судьи по одному кейсу."""

    score: float | None = None  # распознанное число 0..10, либо None
    raw: str = ""  # что именно вернул судья (для отладки/отчёта)
    error: str = ""  # текст сбоя (пусто, если всё ок)

    @property
    def ok(self) -> bool:
        return self.score is not None and not self.error


def default_cache_path() -> Path:
    """Путь к кэшу вердиктов рядом с приложением."""
    from .config import app_root

    return app_root() / CACHE_DIR / CACHE_FILE


class JudgeCache:
    """Кэш вердиктов судьи на диске.

    Ключ — хеш всего, что влияет на вердикт: сообщения (в них уже сидят
    промпт судьи, вопрос, ответ и эталон), температура, бюджет ответа и имя
    модели-судьи. Смена любого из них меняет ключ, поэтому «протухших»
    записей в строгом смысле не бывает — они просто не находятся.

    Кэшируются только успешные вердикты. Сбой судьи — это состояние момента
    (сервер грузился, сеть отвалилась), а не свойство кейса; запомнить его
    означало бы навсегда приписать кейсу ошибку, которой уже нет.
    """

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.enabled = bool(enabled)
        self.hits = 0
        self.misses = 0
        self._entries: dict[str, dict[str, Any]] | None = None
        self._dirty = False

    # ------------------------------------------------------------------
    # загрузка / запись

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        self._entries = {}
        if not self.enabled or not self.path.is_file():
            return self._entries
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Битый кэш — не повод ронять прогон: судья просто спросится заново.
            return self._entries
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return self._entries
        entries = data.get("entries")
        if isinstance(entries, dict):
            self._entries = {k: v for k, v in entries.items() if isinstance(v, dict)}
        return self._entries

    def save(self) -> bool:
        """Записать кэш на диск. Возвращает False, если писать было нечего."""
        if not self.enabled or not self._dirty or self._entries is None:
            return False
        payload = {"version": CACHE_VERSION, "entries": self._entries}
        try:
            write_json_atomic(self.path, payload, indent=None)
        except OSError:
            return False
        self._dirty = False
        return True

    # ------------------------------------------------------------------
    # ключ и доступ

    def make_key(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        judge_model: str,
    ) -> str:
        """Ключ записи. `sort_keys` — чтобы порядок ключей не влиял на хеш."""
        payload = json.dumps(
            {
                "v": CACHE_VERSION,
                "judge": judge_model,
                "temperature": round(float(temperature), 6),
                "max_tokens": int(max_tokens),
                "messages": messages,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> JudgeResult | None:
        """Вердикт из кэша, если он там есть и в нём есть число."""
        if not self.enabled:
            return None
        entry = self._load().get(key)
        if not isinstance(entry, dict) or entry.get("score") is None:
            self.misses += 1
            return None
        try:
            score = float(entry["score"])
        except (TypeError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return JudgeResult(score=score, raw=str(entry.get("raw") or ""))

    def put(self, key: str, result: JudgeResult) -> None:
        """Запомнить успешный вердикт."""
        if not self.enabled or not result.ok:
            return
        entries = self._load()
        entries[key] = {"score": result.score, "raw": result.raw}
        while len(entries) > CACHE_MAX_ENTRIES:
            entries.pop(next(iter(entries)))
        self._dirty = True

    def __len__(self) -> int:
        return len(self._load())


class JudgeEvaluator:
    """Вызов модели-судьи по конфигу `scoring` набора."""

    def __init__(
        self,
        agent: LocalAgent,
        scoring: dict[str, Any],
        cache: JudgeCache | None = None,
        judge_model: str = "",
    ) -> None:
        self.agent = agent
        self.scoring = scoring or {}
        # Агент-судья может быть отдельным (второй сервер), но по умолчанию —
        # тот же, что тестируется. Подмена происходит в runner'е при создании.
        self.judge_prompt = str(self.scoring.get("judge_prompt") or "").strip()
        # Кэш вердиктов. Передаётся снаружи, потому что путь к нему знает
        # только вызывающий: у проверок он временный, у приложения — рядом
        # с .exe. Без кэша судья просто спрашивается каждый раз.
        self.cache = cache
        # Имя модели-судьи входит в ключ кэша: тот же промпт на другой модели
        # даёт другую оценку, и путать их нельзя.
        self.judge_model = judge_model or str(getattr(agent, "model_name", "") or "")
        try:
            self.min_score = float(self.scoring.get("min_score", 0))
        except (TypeError, ValueError):
            self.min_score = 0.0
        # Температура судьи — 0: оценка должна быть воспроизводимой, иначе
        # один и тот же ответ получит разные баллы при повторных прогонах.
        self.temperature = float(self.scoring.get("temperature", 0.0))
        # Бюджет судьи щедрее обычного: модели с каналом рассуждений
        # (gemma-4 и подобные) тратят часть лимита на «думанье» и только потом
        # выдают число в content. При 64 токенах рассуждение обрывается и
        # content приходит пустым — парсер ничего не находит.
        self.max_tokens = int(self.scoring.get("max_tokens", 512))
        # Нижняя и верхняя граница шкалы. По ТЗ — 0..10, но позволяем
        # задать через scoring при необходимости.
        try:
            self.scale_max = float(self.scoring.get("scale_max", 10))
        except (TypeError, ValueError):
            self.scale_max = 10.0

    # ------------------------------------------------------------------
    # построение запроса

    def build_messages(
        self,
        question: str,
        answer: str,
        golden: str = "",
    ) -> list[dict[str, str]]:
        """Собрать сообщения для судьи.

        Структура: инструкция из `judge_prompt`, затем вопрос, ответ модели и
        (если есть) эталон — как справочный, чтобы судья понимал, чего
        ожидалось, но не копировал его дословно.
        """
        blocks = [self.judge_prompt or "Оцени качество ответа."]
        blocks.append("Вопрос пользователя:\n%s" % (question or "").strip())
        blocks.append("Ответ модели:\n%s" % (answer or "").strip())
        if golden:
            blocks.append(
                "Эталонный ответ (для справки, не требуй дословного "
                "совпадения):\n%s" % golden.strip()
            )
        blocks.append("Оцени ответ по шкале от 0 до %g. Верни ТОЛЬКО число." % self.scale_max)
        return [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]

    # ------------------------------------------------------------------
    # вызов

    def evaluate(
        self,
        question: str,
        answer: str,
        golden: str = "",
    ) -> JudgeResult:
        """Оценить ответ. Не бросает исключений — сбой возвращается в error."""
        if not self.judge_prompt:
            return JudgeResult(error="в scoring набора не задан judge_prompt")
        messages = self.build_messages(question, answer, golden)

        key = ""
        if self.cache is not None:
            key = self.cache.make_key(
                messages, self.temperature, self.max_tokens, self.judge_model
            )
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        result = self._ask(messages)
        if self.cache is not None and key:
            self.cache.put(key, result)
        return result

    def _ask(self, messages: list[dict[str, str]]) -> JudgeResult:
        """Спросить судью по уже собранным сообщениям."""
        try:
            comp = self.agent.complete(
                messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — сеть/сервер не должны ронять прогон
            return JudgeResult(error="сбой запроса к судье: %s" % exc)

        if comp.error:
            return JudgeResult(error="судья недоступен: %s" % comp.error[:200])
        raw = (comp.text or "").strip()
        score = self._parse_score(raw)
        if score is None and comp.reasoning:
            # Модели с рассуждениями (gemma-4) иногда выдают число только в
            # reasoning_content, а content оставляют пустым. Берём последнее
            # число из рассуждения — это итоговый вердикт в конце «думанья».
            raw_reason = comp.reasoning.strip()
            score_reason = self._parse_score(raw_reason, prefer_last=True)
            if score_reason is not None:
                return JudgeResult(score=score_reason, raw=raw_reason)
        if score is None:
            return JudgeResult(
                raw=raw or comp.reasoning,
                error="судья не вернул число в диапазоне 0..%g" % self.scale_max,
            )
        return JudgeResult(score=score, raw=raw)

    # ------------------------------------------------------------------
    # парсинг

    def _parse_score(self, text: str, prefer_last: bool = False) -> float | None:
        """Вытащить оценку из ответа судьи.

        Берём первое (или последнее, если `prefer_last`) число в ответе и
        проверяем, что оно в шкале. Если судья выдал число выше верхней
        границы (например, оценил «по стобалльной», хотя просили 0..10), —
        это ошибка формата, а не высший балл: лучше честно пометить, чем
        приписать модели 10 из 10 на пустом месте.
        """
        if not text:
            return None
        matches = _SCORE_RE.findall(text)
        if not matches:
            return None
        token = matches[-1] if prefer_last else matches[0]
        try:
            value = float(token)
        except ValueError:
            return None
        if value < 0 or value > self.scale_max + 1e-9:
            return None
        return value

    # ------------------------------------------------------------------
    # вердикт

    def passed(self, score: float | None) -> bool | None:
        """Сравнить оценку с порогом. None — если оценки нет (пропуск)."""
        if score is None:
            return None
        return score >= self.min_score
