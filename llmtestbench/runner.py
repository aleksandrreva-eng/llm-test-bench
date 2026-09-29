"""Исполнитель тестов: прогон кейсов через HTTP API llama-server.

Один прогон — один набор. Для каждого кейса собирается запрос, отправляется
на сервер, замеряется время, ответ проверяется типом проверки из кейса, и
результат ложится в JSON по образцу из ТЗ:

    results/<модель>_<набор>_<ГГГГММДД_ЧЧММСС>.json

Три вещи, которые здесь сделаны намеренно и неочевидны:

1. **Запас на рассуждение.** `max_tokens` в кейсе — это бюджет ОТВЕТА.
   У моделей с отдельным каналом рассуждений (gemma-4 и подобные) размышление
   тратит токены из того же лимита: проверено, что при `max_tokens: 192` ответ
   приходит пустым, а при 1024 — полным. Поэтому к бюджету кейса добавляется
   `reasoning_allowance`, и в результат пишутся оба числа. Без этого прогон
   показывал бы провалы модели там, где модель не виновата.

2. **Пустой ответ — это не ошибка стенда.** Он записывается отдельным полем
   `empty`: причина у него своя, и в отчёте он не должен путаться с обрывом
   связи.

3. **Сбой стенда не считается провалом модели.** Ошибка сервера помечает кейс
   как `stand_error`, и такой кейс выпадает из знаменателя итогового счёта —
   иначе одна перезагрузка модели портит всю статистику.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .atomic_io import write_json_atomic
from .checks import run_check
from .config import AppConfig
from .generators import PromptBuilder, load_paragraphs
from .judge import CACHE_FILE, JudgeCache, JudgeEvaluator, JudgeResult
from .local_agent import (
    Completion,
    LocalAgent,
    ServerUnavailable,
    estimate_prompt_speed,
    is_infra_error,
)
from .logging_setup import get_logger
from .server_cmd import describe_server
from .server_log import MemoryReport
from .testsets import TestCase, TestSet, case_prompt_text
from .database import DatabaseManager, DBRun, DBCaseResult

log = get_logger(__name__)

#: Сколько токенов сверху давать на рассуждение. Значение взято с запасом:
#: у gemma-4 на простой вопрос уходит 130–600 токенов размышления, на
#: структурный ответ — до 2600.
DEFAULT_REASONING_ALLOWANCE = 2048

#: Инструкция системной роли для прогона. Короткая: она одинакова для всех
#: моделей, и её влияние на результат должно быть минимальным.
RUN_SYSTEM = "Ты отвечаешь на вопросы теста. Следуй инструкциям в задании."


def model_stem(model: str) -> str:
    """Имя модели без пути и расширения, пригодное для имени файла.

    Одна функция на два идентификатора — `run_id` и `batch_id`. Раньше
    нормализация стояла прямо в `run_set`, и любая правка одного места
    расходилась бы с другим.
    """
    return re.sub(r"[^\w.-]+", "-", Path(model).stem)[:60]


def make_batch_id(model: str, now: datetime | None = None) -> str:
    """Идентификатор запуска целиком: `{модель}_{ГГГГММДД_ЧЧММСС}`.

    В `run_id` входит ещё и набор, поэтому одно нажатие «Запустить» по семи
    наборам давало семь ничем не связанных файлов: история не могла показать
    прогон одной строкой, а сравнение — отличить наборы одной партии от
    наборов, прогнанных порознь. `batch_id` одинаков у всех файлов одного
    запуска, по нему они и собираются.

    Метка времени — секунды. Прогоны длятся минутами, поэтому двух запусков
    в одну секунду не бывает; если это когда-нибудь случится, наборы просто
    склеятся в одну группу — заметно и поправимо, в отличие от молча
    потерянных прогонов.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return "%s_%s" % (model_stem(model), stamp)


def looks_like_loop(text: str, window: int = 200) -> bool:
    """Зациклилось ли рассуждение.

    Признак простой: хвост текста уже встречался выше. Это не украшение
    отчёта, а разделение двух разных диагнозов. Если ответа нет, потому что
    рассуждению не хватило бюджета, — лечится запасом. Если модель ходит по
    кругу, запас не поможет: она будет кружить, пока не кончится лимит.
    """
    if len(text) < window * 3:
        return False
    tail = text[-window:].strip()
    return bool(tail) and tail in text[:-window]


@dataclass
class CaseResult:
    """Результат одного кейса в одном прогоне."""

    case_id: str
    name: str = ""
    run_index: int = 1
    tags: list[str] = field(default_factory=list)
    weight: float = 1.0

    prompt: str = ""
    answer: str = ""
    reasoning: str = ""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    tokens_per_sec: float = 0.0
    prompt_ms: float = 0.0
    prompt_tokens_per_sec: float = 0.0
    #: Скорости посчитаны по своим замерам, а не пришли от сервера в `timings`.
    #: В интерфейсе такие числа помечаются «≈»: это оценка, а не замер.
    speeds_estimated: bool = False

    max_tokens_answer: int = 0
    max_tokens_effective: int = 0

    check_type: str = ""
    passed: bool | None = None
    reason: str = ""
    stop_reason: str = ""
    empty: bool = False
    stand_error: str = ""
    error: str = ""

    # LLM-as-judge (этап 8). Заполняются, только если набор использует
    # scoring.type == "llm_judge". judge_score — распознанное число 0..10,
    # judge_response — сырой ответ судьи, judge_error — текст сбоя (пусто,
    # если судья отработал). При сбое судьи эти поля не влияют на passed:
    # вердикт берётся из keyword-проверки expected (fallback).
    judge_score: float | None = None
    judge_response: str = ""
    judge_error: str = ""

    @property
    def counted(self) -> bool:
        """Идёт ли кейс в знаменатель итогового счёта."""
        return self.passed is not None and not self.stand_error

    @property
    def answer_tokens(self) -> int:
        """Токены, ушедшие собственно на ответ, без рассуждения."""
        return max(0, self.completion_tokens - self.reasoning_tokens)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["answer_tokens"] = self.answer_tokens
        d["counted"] = self.counted
        return d

    def brief(self) -> str:
        if self.stand_error:
            mark = "СБОЙ"
        elif self.passed is None:
            mark = "ПРОП"
        elif self.passed:
            mark = " ок "
        else:
            mark = "ПЛОХ"
        extra = ""
        if self.empty:
            extra = " (ответ пуст)"
        elif self.reasoning_tokens:
            extra = " (рассуждение %d ток.)" % self.reasoning_tokens
        # «≈» у скоростей, посчитанных по своим замерам: сервер их не прислал
        # (старая сборка без `timings`), и выдавать оценку за замер нельзя.
        approx = "≈" if self.speeds_estimated else ""
        return (
            "[%s] %-22s %7.2f с, TTFT %5.0f мс, префилл %s%6.1f t/s, выход %s%6.1f t/s%s %s"
        ) % (
            mark,
            self.case_id,
            self.total_ms / 1000.0,
            self.ttft_ms,
            approx,
            self.prompt_tokens_per_sec,
            approx,
            self.tokens_per_sec,
            extra,
            self.reason[:70],
        )


@dataclass
class RunResult:
    """Итог прогона одного набора."""

    run_id: str
    model: str
    base_url: str
    set_id: str
    set_name: str = ""
    set_version: str = ""
    model_path: str = ""
    context_size: int = 0
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    status: str = "finished"  # finished | cancelled | failed
    params: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    path: str = ""
    # Идентификатор запуска целиком (все наборы одного нажатия «Запустить»).
    # Новые поля дописываются в конец: вставка в середину сдвигает позиции и
    # ломает тех, кто собирает результат позиционно. Пустая строка — у прогонов,
    # сделанных до появления `batch_id`; история собирает их по времени.
    batch_id: str = ""
    # Память модели и раскладка слоёв по устройствам — блок `memory` в JSON.
    # Имя именно `memory`, а не `server`: `server` в ТЗ (п. 4.8) — это статус
    # процесса (pid, команда, время загрузки), и смешивать их нельзя.
    # Пустой словарь значит «данных нет», и это обычное состояние: цифры берутся
    # из лога загрузки, а его пишет только сервер, поднятый самим приложением.
    # Пустой словарь, а не нули: отчёт обязан отличать «не измерено» от «ноль».
    memory: dict[str, Any] = field(default_factory=dict)

    def compute_summary(self) -> dict[str, Any]:
        """Итоги: сколько прошло, сколько провалено, что выпало из счёта."""
        total = len(self.cases)
        passed = sum(1 for c in self.cases if c.passed is True and not c.stand_error)
        failed = sum(1 for c in self.cases if c.passed is False and not c.stand_error)
        skipped = sum(1 for c in self.cases if c.passed is None and not c.stand_error)
        stand = sum(1 for c in self.cases if c.stand_error)
        empty = sum(1 for c in self.cases if c.empty)

        by_tag: dict[str, dict[str, int]] = {}
        for c in self.cases:
            if not c.counted:
                continue
            for tag in c.tags or ["без тега"]:
                slot = by_tag.setdefault(tag, {"passed": 0, "total": 0})
                slot["total"] += 1
                if c.passed:
                    slot["passed"] += 1

        counted = passed + failed
        judge_scores = [c.judge_score for c in self.cases if c.judge_score is not None]
        mean_judge = round(sum(judge_scores) / len(judge_scores), 3) if judge_scores else None
        self.summary = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "stand_errors": stand,
            "empty_answers": empty,
            "counted": counted,
            "score": round(passed / counted, 4) if counted else None,
            "mean_judge_score": mean_judge,
            "by_tag": by_tag,
        }
        return self.summary

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["app_version"] = __version__
        d["cases"] = [c.as_dict() for c in self.cases]
        return d


class TestRunner:
    """Прогон набора кейсов против поднятого сервера."""

    def __init__(
        self,
        agent: LocalAgent,
        cfg: AppConfig | None = None,
        reasoning_allowance: int = DEFAULT_REASONING_ALLOWANCE,
        judge_agent: LocalAgent | None = None,
        memory_provider: Callable[[], MemoryReport | None] | None = None,
        server_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.agent = agent
        self.cfg = cfg or AppConfig.load()
        self.reasoning_allowance = int(reasoning_allowance)
        # Откуда брать память модели. Задаётся снаружи: путь к логу загрузки
        # знает тот, кто поднимал сервер. У чужих прогонов источника нет, и
        # блок `server` остаётся пустым — это нормально и видно в отчёте.
        self.memory_provider = memory_provider
        # Чем поднят сервер. Тоже снаружи: процессом управляет окно или
        # консоль, а не прогон. Без этого два прогона одной модели с разными
        # флагами запуска неотличимы в истории, а скорость решают именно они.
        self.server_provider = server_provider
        # Агент-судья. По умолчанию — тот же сервер, что тестируется
        # (manifest пишет judge_model: "local"). Если поднят отдельный сервер
        # под судью, его можно передать сюда.
        self.judge_agent = judge_agent or agent
        self._builder: PromptBuilder | None = None
        self._judge: JudgeEvaluator | None = None
        # Кэш вердиктов судьи — один на прогон, чтобы попадания копились
        # в общую статистику, а файл писался один раз в конце набора.
        self._judge_cache: JudgeCache | None = None
        # Считается лениво, при первом обращении, и дальше не меняется: если
        # считать метку в `run_set`, каждый набор получит своё время старта и
        # группировать в истории будет нечего.
        self._batch: str | None = None

    # ------------------------------------------------------------------
    # вспомогательное

    def batch_id(self) -> str:
        """Идентификатор запуска целиком — один на все наборы этого прогона.

        Ленивый намеренно: агент может быть ещё не готов, а прогон — не
        начаться. Метка считается в момент первого набора, то есть когда
        сервер уже ответил и прогон действительно пошёл.
        """
        if self._batch is None:
            self._batch = make_batch_id(self.agent.model_name)
        return self._batch

    def prompt_builder(self) -> PromptBuilder:
        """Сборщик промптов для наборов speed и context_long.

        Создаётся лениво: большинству наборов он не нужен, а чтение «стога»
        и проверка сервера стоят времени.
        """
        if self._builder is None:
            haystack = self.cfg.tests_path / "context_long" / "haystack"
            paragraphs = load_paragraphs(haystack)
            if not paragraphs:
                raise RuntimeError("нет текста-наполнителя: запустите build_haystack.py")
            self._builder = PromptBuilder(
                paragraphs, count_tokens=self.agent.count_tokens, seed=42
            )
        return self._builder

    def messages_for(self, case: TestCase) -> tuple[list[dict[str, str]], str]:
        """Собрать сообщения для запроса и текстовое представление промпта."""
        if case.generator:
            text = self.prompt_builder().build(case)
            return (
                [
                    {"role": "system", "content": RUN_SYSTEM},
                    {"role": "user", "content": text},
                ],
                text,
            )
        if case.is_multi_turn:
            msgs = [
                {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
                for m in case.prompt
                if isinstance(m, dict)
            ]
            # Свою системную роль добавляем только если её нет в кейсе:
            # вторая подсказка перебила бы первую.
            if not msgs or msgs[0].get("role") != "system":
                msgs.insert(0, {"role": "system", "content": RUN_SYSTEM})
            return msgs, case_prompt_text(case)
        return (
            [
                {"role": "system", "content": RUN_SYSTEM},
                {"role": "user", "content": str(case.prompt)},
            ],
            str(case.prompt),
        )

    def effective_max_tokens(self, case: TestCase, defaults: dict[str, Any]) -> tuple[int, int]:
        """Бюджет ответа и фактический лимит с запасом на рассуждение."""
        answer = int((case.params or {}).get("max_tokens") or defaults.get("max_tokens") or 512)
        return answer, answer + self.reasoning_allowance

    # ------------------------------------------------------------------
    # прогон

    def run_case(
        self,
        case: TestCase,
        defaults: dict[str, Any],
        run_index: int = 1,
        scoring: Any = None,
    ) -> CaseResult:
        """Один кейс: запрос, метрики, проверка.

        `scoring` — блок `scoring` набора (dict с type=="llm_judge" или строка
        "none"). Если это llm_judge, поверх обычной keyword-проверки из
        `expected` запускается модель-судья, и её оценка становится основным
        вердиктом. При сбое судьи вердикт берётся из keyword-проверки.
        """
        res = CaseResult(
            case_id=case.id,
            name=case.name,
            run_index=run_index,
            tags=list(case.tags),
            weight=float(case.weight or 1.0),
        )
        try:
            messages, prompt_text = self.messages_for(case)
        except (RuntimeError, ValueError) as exc:
            res.error = str(exc)
            res.reason = "промпт не собран: %s" % exc
            return res

        res.prompt = prompt_text
        params = dict(defaults)
        params.update(case.params or {})
        answer_budget, effective = self.effective_max_tokens(case, defaults)
        res.max_tokens_answer = answer_budget
        res.max_tokens_effective = effective

        t0 = time.monotonic()
        comp: Completion = self.agent.complete(
            messages,
            temperature=float(params.get("temperature", 0.0)),
            top_p=float(params.get("top_p", 0.95)),
            max_tokens=effective,
            seed=int(params["seed"]) if str(params.get("seed", "")).strip() != "" else None,
        )
        res.total_ms = round((time.monotonic() - t0) * 1000.0, 1)
        res.ttft_ms = comp.ttft_ms
        res.tokens_per_sec = round(comp.predicted_per_second, 2)
        res.prompt_ms = round(comp.prompt_ms, 1)
        res.prompt_tokens_per_sec = round(comp.prompt_per_second, 2)
        res.answer = comp.text
        res.reasoning = comp.reasoning
        res.stop_reason = comp.stop_reason
        res.prompt_tokens = comp.prompt_tokens or self.agent.count_tokens(prompt_text)
        res.completion_tokens = comp.completion_tokens
        if comp.reasoning:
            res.reasoning_tokens = self.agent.count_tokens(comp.reasoning)
        res.speeds_estimated = comp.speeds_estimated
        # Скорость префилла могла не посчитаться: в `Completion` число токенов
        # промпта берётся из `usage`, а если старая сборка его не прислала, там
        # ноль. Здесь токены уже уточнены через `/tokenize` — из них скорость
        # и выводится.
        if not res.prompt_tokens_per_sec:
            res.prompt_tokens_per_sec = estimate_prompt_speed(
                res.prompt_tokens, res.prompt_ms or res.ttft_ms
            )
            if res.prompt_tokens_per_sec:
                res.speeds_estimated = True

        if comp.error:
            res.error = comp.error
            if is_infra_error(comp.error):
                res.stand_error = comp.error
                res.reason = "сбой стенда: %s" % comp.error[:160]
            else:
                res.reason = "ошибка запроса: %s" % comp.error[:160]
            return res

        res.empty = not comp.text
        if res.empty:
            if comp.reasoning and looks_like_loop(comp.reasoning):
                res.reason = (
                    "ответ пуст: рассуждение зациклилось и съело весь бюджет "
                    "(%d токенов). Увеличение запаса не поможет" % effective
                )
            elif comp.stop_reason == "length" and comp.reasoning:
                res.reason = (
                    "ответ пуст: бюджет %d токенов ушёл в рассуждение "
                    "(нужен запас больше)" % effective
                )
            else:
                res.reason = "ответ пуст при стопе «%s»" % (comp.stop_reason or "—")

        check = run_check(case.expected, comp.text)
        res.check_type = check.check_type
        res.passed = check.passed
        if res.empty:
            # У пустого ответа причина своя — она объясняет, куда ушёл бюджет,
            # и формулировка проверки её только затрёт.
            pass
        elif check.reason:
            res.reason = check.reason

        # --- LLM-as-judge (этап 8) ---
        # Судья работает только если набор так сконфигурирован и ответ не пуст.
        if self._judge_wanted(scoring) and not res.empty and not res.stand_error:
            jr = self._run_judge(case, comp.text, scoring)
            res.judge_score = jr.score
            res.judge_response = jr.raw
            res.judge_error = jr.error
            if jr.ok:
                res.check_type = "llm_judge"
                res.passed = jr.score >= self._judge.min_score
                res.reason = "судья: %.1f/%.0f (порог %.0f)" % (
                    jr.score,
                    self._judge.scale_max,
                    self._judge.min_score,
                )
            elif jr.error:
                # Судья не сработал — оставляем keyword-вердикт, но помечаем,
                # что основной критерий недоступен. Пустой судья не должен
                # превращать провал модели в зачёт и наоборот.
                note = "; судья недоступен: %s" % jr.error
                res.reason = (res.reason + note)[:400]

        return res

    # ------------------------------------------------------------------
    # LLM-as-judge

    def _warmup(self) -> bool:
        """Прогреть сервер перед измерениями. False — не удалось (не провал)."""
        if not bool(getattr(self.cfg, "warmup", True)):
            return False
        warm = getattr(self.agent, "warmup", None)
        if warm is None:
            return False
        try:
            ok = bool(warm())
        except Exception as exc:  # noqa: BLE001 — прогрев вспомогателен
            log.warning("прогрев не удался: %s", exc)
            return False
        log.info("прогрев модели: %s", "выполнен" if ok else "не выполнен")
        return ok

    def _server_busy(self) -> str | None:
        """Занят ли сервер чужим запросом. `None` — свободен или не узнать.

        Проверка вспомогательная: если она не удалась, прогон идёт дальше без
        пометки. Ронять замеры из-за того, что не спросили `/slots`, нельзя.
        """
        probe = getattr(self.agent, "server_busy", None)
        if probe is None:
            return None
        try:
            return probe()
        except Exception as exc:  # noqa: BLE001 — проверка вспомогательна
            log.warning("не удалось проверить занятость сервера: %s", exc)
            return None

    def _server_info(self) -> dict[str, Any]:
        """Чем поднят сервер. Источник вспомогательный, отказ не роняет прогон.

        Нет источника — возвращается та же запись, что и при неизвестной
        команде. Молчать нельзя: пустая запись тоже сведение, она говорит
        «флагов запуска у нас нет», тогда как отсутствие ключа не говорит
        ничего и читается как «а зачем это вообще».
        """
        if self.server_provider is None:
            return describe_server()
        try:
            info = self.server_provider()
        except Exception as exc:  # noqa: BLE001 — источник вспомогательный
            log.warning("не удалось описать сервер запуска: %s", exc)
            return describe_server()
        return dict(info) if info else describe_server()

    def _judge_wanted(self, scoring: Any) -> bool:
        """Нужен ли судья для этого прогона."""
        return isinstance(scoring, dict) and str(scoring.get("type") or "").strip() == "llm_judge"

    def judge_cache(self) -> JudgeCache:
        """Кэш вердиктов судьи. Создаётся лениво, один на прогон.

        Путь берётся из конфига (`cache_path`), поэтому проверки со своим
        временным конфигом не трогают рабочий `.cache` проекта.
        """
        if self._judge_cache is None:
            self._judge_cache = JudgeCache(
                self.cfg.cache_path / CACHE_FILE,
                enabled=bool(getattr(self.cfg, "judge_cache", True)),
            )
        return self._judge_cache

    def flush_judge_cache(self) -> None:
        """Сохранить кэш и сказать в лог, насколько он помог.

        Без этой строки кэш — чёрный ящик: непонятно, работает он или каждый
        прогон всё равно платит токенами. Числа попаданий и промахов отвечают
        на это прямо.
        """
        if self._judge_cache is None:
            return
        saved = self._judge_cache.save()
        log.info(
            "судья: из кэша %d, спрошено заново %d%s",
            self._judge_cache.hits,
            self._judge_cache.misses,
            "" if saved else " (сохранять нечего)",
        )

    def _run_judge(
        self,
        case: TestCase,
        answer: str,
        scoring: dict[str, Any],
    ) -> JudgeResult:
        """Вызвать судью для ответа кейса.

        `JudgeEvaluator` создаётся лениво и один раз на прогон — он хранит
        конфиг (порог, промпт) и агента. Если в scoring задан отдельный
        judge_model-адрес, runner уже подставил нужного агента в self.judge_agent.
        """
        if self._judge is None:
            self._judge = JudgeEvaluator(self.judge_agent, scoring, cache=self.judge_cache())
        golden = case.extra.get("golden") or (case.expected or {}).get("golden") or ""
        return self._judge.evaluate(str(case.prompt), answer, golden=golden)

    def run_set(
        self,
        test_set: TestSet,
        runs: int = 1,
        limit: int = 0,
        tags: list[str] | None = None,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[CaseResult, int, int], None] | None = None,
        batch_id: str | None = None,
    ) -> RunResult:
        """Прогнать набор. `limit` — сколько первых кейсов взять (0 — все).

        `batch_id` передаётся явно, когда наборы одной партии гоняет внешний
        код со своим представлением о том, что такое «один запуск». Если не
        передан — берётся общий на весь `TestRunner`.
        """
        cases = test_set.filtered(tags=tags, limit=limit)
        defaults = dict(test_set.default_params or {})
        now = datetime.now()
        model = self.agent.model_name
        run = RunResult(
            run_id="%s_%s_%s"
            % (
                model_stem(model),
                test_set.id,
                now.strftime("%Y%m%d_%H%M%S"),
            ),
            batch_id=batch_id or self.batch_id(),
            model=model,
            base_url=self.agent.base_url,
            set_id=test_set.id,
            set_name=test_set.name,
            set_version=test_set.version,
            model_path=self._model_path(),
            context_size=self.agent.context_size,
            started=now.isoformat(timespec="seconds"),
            params={
                "runs": runs,
                "limit": limit,
                "tags": list(tags or []),
                "reasoning_allowance": self.reasoning_allowance,
                "temperature": defaults.get("temperature", 0.0),
                "seed": defaults.get("seed"),
                "cases_in_set": len(test_set.cases),
            },
        )

        # Память модели спрашиваем один раз на прогон, а не на кейс: сервер уже
        # загружен, и лог больше не меняется. Ошибку разбора глотаем намеренно —
        # память вспомогательна, из-за неё прогон падать не должен.
        if self.memory_provider is not None:
            try:
                report = self.memory_provider()
            except Exception as exc:
                # Память вспомогательна: разбор чужого формата не должен ронять
                # прогон. Но и молчать нельзя — иначе «нет данных» в отчёте
                # неотличимо от сломанного разбора.
                log.warning("память модели не разобрана: %s", exc)
                report = None
            if report is not None and self._memory_matches(report, run):
                run.memory = report.as_dict()

        # Занят ли сервер посторонним запросом — до прогрева, иначе первым
        # слот займём мы сами. Факт пишется в прогон: недостоверные замеры
        # должны быть отличимы от достоверных хотя бы задним числом.
        busy = self._server_busy()
        if busy:
            log.warning(
                "сервер занят посторонним запросом (%s) — скорости этого прогона "
                "показывают очередь, а не модель",
                busy,
            )
        run.params["server_busy"] = busy

        # Чем поднят сервер, против которого идёт прогон. Пишется всегда, в том
        # числе когда неизвестно: скорости решают флаги запуска, и без записи
        # два прогона одного набора на одной модели в истории неотличимы.
        run.params["server"] = self._server_info()

        # Прогрев: первый запрос после загрузки модели считается дольше
        # остальных (захват CUDA-графов, аллокация KV-кэша), и без прогрева
        # этот выброс попадает в первый кейс — а в наборе `speed` первый кейс
        # как раз измеряемый. Результат прогрева никуда не идёт.
        warmed = self._warmup()
        run.params["warmup"] = warmed

        total = len(cases) * max(1, runs)
        done = 0
        t_start = time.monotonic()
        for run_index in range(1, max(1, runs) + 1):
            for case in cases:
                if should_stop is not None and should_stop():
                    run.status = "cancelled"
                    break
                result = self.run_case(
                    case,
                    defaults,
                    run_index,
                    scoring=test_set.scoring,
                )
                run.cases.append(result)
                done += 1
                if progress is not None:
                    progress(result, done, total)
                if result.stand_error and self._server_dead():
                    run.status = "failed"
                    break
            if run.status != "finished":
                break

        run.seconds = round(time.monotonic() - t_start, 2)
        run.finished = datetime.now().isoformat(timespec="seconds")
        run.compute_summary()
        self.flush_judge_cache()
        return run

    def _model_path(self) -> str:
        try:
            return str(self.agent.props().get("model_path") or "")
        except ServerUnavailable:
            return ""

    @staticmethod
    def _memory_matches(report: MemoryReport, run: RunResult) -> bool:
        """Относится ли разобранный лог загрузки к этой модели.

        Лог сервера дописывается между запусками, поэтому в нём может лежать
        загрузка **другой** модели: приложение поднимало сервер раньше, а сейчас
        тестирует тот, что уже поднят снаружи. Показать чужие цифры как память
        текущей модели хуже, чем не показать ничего.

        Если в логе нет пути к модели или у прогона нет её имени — сравнивать не
        с чем, и данные принимаются: отказ здесь был бы вреднее ошибки.
        """
        if not report.model_path:
            return True
        in_log = Path(report.model_path).name.lower()
        mine = {Path(p).name.lower() for p in (run.model_path, run.model) if p}
        return not mine or in_log in mine

    def _server_dead(self) -> bool:
        """Сервер перестал отвечать — продолжать прогон бессмысленно."""
        try:
            self.agent.health(timeout=5)
            return False
        except ServerUnavailable:
            return True

    # ------------------------------------------------------------------
    # сохранение

    def save(self, run: RunResult, results_dir: str | Path | None = None) -> Path:
        """Записать результат прогона: JSON (для отчёта) + SQLite (история).

        JSON оставляем ради обратной совместимости и генерации HTML-отчёта.
        SQLite — основное хранилище истории: быстрый фильтр по модели/набору/
        дате без перебора файлов.
        """
        # --- JSON ---
        folder = Path(results_dir or self.cfg.results_path)
        path = write_json_atomic(folder / f"{run.run_id}.json", run.as_dict())
        run.path = str(path)
        log.info("прогон %s записан: %s", run.run_id, path)

        # --- SQLite ---
        try:
            db = DatabaseManager(self.cfg.db_path)
            s = run.summary
            db_run = DBRun(
                run_id=run.run_id,
                model=run.model,
                set_id=run.set_id,
                set_name=run.set_name,
                started=run.started,
                finished=run.finished,
                status=run.status,
                total=s.get("total", 0),
                passed=s.get("passed", 0),
                failed=s.get("failed", 0),
                scored=s.get("score"),
                duration=run.seconds,
                params=run.params,
            )
            db_cases = [
                DBCaseResult(
                    run_id=run.run_id,
                    case_id=c.case_id,
                    name=c.name,
                    prompt=c.prompt,
                    answer=c.answer,
                    reasoning=c.reasoning,
                    prompt_tokens=c.prompt_tokens,
                    completion_tokens=c.completion_tokens,
                    ttft_ms=c.ttft_ms,
                    total_ms=c.total_ms,
                    passed=(c.passed if c.passed is not None else False),
                    reason=c.reason,
                    check_type=c.check_type,
                    judge_score=c.judge_score,
                    judge_response=c.judge_response,
                    judge_error=c.judge_error,
                    extra={
                        "tags": c.tags,
                        "weight": c.weight,
                        "empty": c.empty,
                        "stand_error": c.stand_error,
                        "stop_reason": c.stop_reason,
                        "tokens_per_sec": c.tokens_per_sec,
                        "prompt_tokens_per_sec": c.prompt_tokens_per_sec,
                        "prompt_ms": c.prompt_ms,
                        "speeds_estimated": c.speeds_estimated,
                    },
                )
                for c in run.cases
            ]
            db.save_run(db_run, db_cases)
        except Exception as exc:
            # История — второе хранилище: JSON уже записан, поэтому прогон не
            # теряется. Но пользователь должен знать, что история отстала.
            log.warning("не удалось записать прогон в SQLite: %s", exc)

        return path
