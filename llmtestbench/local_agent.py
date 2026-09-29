"""Локальный субагент: разовые задания для модели на llama-server.

Главное правило — **одна задача = одна сессия**. История между заданиями не
переносится намеренно: контекст локальной модели конечен (сейчас 132352
токена), и накопленная переписка рано или поздно вытеснит саму задачу —
модель начнёт отвечать на предыдущий разговор вместо текущего. Поэтому
каждый вызов `ask()` собирает запрос с нуля: системная подсказка плюс ровно
одно сообщение пользователя.

Второе правило — **бюджет считается по факту, а не на глаз**. У сервера есть
эндпоинт `/tokenize`, он возвращает точное число токенов тем же токенизатором,
которым модель будет считать вход. Промпт, не влезающий в бюджет, не
отправляется вовсе: лучше внятная ошибка с числами, чем молча обрезанный
контекст и непонятный результат.

Третье правило — **каждая сессия остаётся на диске**. В `sessions/` пишутся
и машиночитаемый JSON, и читаемый MD: что спросили, что приложили, сколько
токенов ушло, сколько заняло, что ответили.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .atomic_io import write_json_atomic, write_text_atomic
from .config import app_root
from .logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TOKEN_CAP = 100_000
DEFAULT_TIMEOUT = 900.0

#: Служебные токены шаблона чата (BOS, роли, разделители). Точному подсчёту
#: через /tokenize они не видны — считаем их отдельно и с запасом.
TEMPLATE_OVERHEAD = 64

#: Сколько байт одного файла вообще имеет смысл прикладывать.
MAX_FILE_BYTES = 96 * 1024

#: Признаки того, что виноват стенд, а не задача. На таком имеет смысл
#: подождать и повторить; на содержательной ошибке — нет.
INFRA_MARKERS = (
    "503",
    "loading model",
    "connection refused",
    "connection reset",
    "connection aborted",
    "no route to host",
    "unavailable_error",
)

#: Сколько подряд неудачных обращений к `/tokenize` считать отказом эндпоинта.
#: Одного мало: сервер в этот момент может быть занят загрузкой модели или
#: чужим запросом. Раньше единственный сбой навсегда переводил прогон на
#: грубую оценку — молча и до конца набора, из-за чего промпт собирался
#: наполовину точным счётом, наполовину оценкой и перелетал через цель.
TOKENIZE_FAIL_LIMIT = 3

DEFAULT_SYSTEM = (
    "Ты исполнитель отдельных небольших задач. Отвечай по-русски. "
    "В ответе — только готовый результат. Никогда не выводи план, черновик, "
    "самопроверку, подсчёт слов и рассуждения о том, как ты выполняешь задачу: "
    "всё это остаётся за кадром. Не задавай вопросов и не извиняйся. "
    "Если в задаче сказано вернуть JSON — верни только JSON, без "
    "markdown-обёртки и пояснений."
)

#: Расширение → метка языка для блока кода в промпте.
_LANG_BY_SUFFIX = {
    ".py": "python",
    ".json": "json",
    ".md": "markdown",
    ".txt": "text",
    ".js": "javascript",
    ".ts": "typescript",
    ".html": "html",
    ".css": "css",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".sql": "sql",
    ".sh": "bash",
    ".ps1": "powershell",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".cs": "csharp",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".xml": "xml",
    ".csv": "csv",
    ".log": "text",
}


class ServerUnavailable(RuntimeError):
    """Сервер не отвечает или отвечает отказом — задача не доехала."""


class BudgetExceeded(RuntimeError):
    """Промпт не влезает в отведённый бюджет токенов."""


def is_infra_error(message: str | None) -> bool:
    """Похоже ли сообщение на сбой стенда, а не на содержательную ошибку."""
    s = str(message or "").lower()
    return any(m in s for m in INFRA_MARKERS)


def slugify(text: str, limit: int = 40) -> str:
    """Короткое имя для файла. Кириллицу не трогаем — Windows её умеет."""
    s = re.sub(r"[^\w\s-]", "", str(text or ""), flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return (s[:limit] or "task").lower()


@dataclass
class Session:
    """Одна сессия субагента: одна задача, один запрос, один ответ."""

    id: str
    task: str
    system: str
    model: str
    base_url: str
    started: str
    files: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    token_cap: int = DEFAULT_TOKEN_CAP
    finished: str = ""
    seconds: float = 0.0
    ttft_ms: float = 0.0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    tps: float = 0.0
    stop_reason: str = ""
    reply: str = ""
    reasoning: str = ""
    prompt: str = ""
    error: str = ""
    log_json: str = ""
    log_md: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.reply)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self) -> str:
        """Одна строка для отчёта в консоли."""
        if self.error:
            return "%s — ОШИБКА: %s" % (self.id, self.error[:120])
        head = "%s — вход %d ток. (бюджет %d), выход %d ток." % (
            self.id,
            self.prompt_tokens,
            self.token_cap,
            self.completion_tokens,
        )
        if self.reasoning_tokens:
            head += " (из них рассуждение %d)" % self.reasoning_tokens
        if not self.reply:
            head += ", ОТВЕТ ПУСТ"
        return "%s, %.1f с, TTFT %.0f мс, %.1f t/s, стоп: %s" % (
            head,
            self.seconds,
            self.ttft_ms,
            self.tps,
            self.stop_reason or "—",
        )


def estimate_generation_speed(tokens: int, seconds: float, ttft_ms: float) -> float:
    """Скорость генерации по своим замерам (когда сервер не прислал `timings`).

    Делить на всё время запроса нельзя: в нём сидит и обработка префилла, и на
    длинном промпте это занижает скорость в разы. Вычитаем время до первого
    токена — остаётся чистая генерация.
    """
    generation_s = seconds - ttft_ms / 1000.0
    if tokens <= 0 or generation_s <= 0:
        return 0.0
    return round(tokens / generation_s, 2)


def estimate_prompt_speed(tokens: int, prompt_ms: float) -> float:
    """Скорость префилла по своим замерам (когда сервер не прислал `timings`)."""
    if tokens <= 0 or prompt_ms <= 0:
        return 0.0
    return round(tokens / (prompt_ms / 1000.0), 2)


@dataclass
class Completion:
    """Ответ сервера на один запрос: текст, метрики, ошибка.

    Отличие от `Session` в том, что здесь нет ничего про задачу и журнал.
    Это низкий уровень, на котором стоит исполнитель тестов: там на один
    набор приходится много вопросов, и запись результата ведётся отдельно.
    """

    text: str = ""
    reasoning: str = ""
    stop_reason: str = ""
    seconds: float = 0.0
    ttft_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_ms: float = 0.0
    prompt_per_second: float = 0.0
    predicted_per_second: float = 0.0
    #: Скорости посчитаны по своим замерам, а не получены от сервера.
    #: Показывать их как точные нельзя — в интерфейсе они помечаются «≈».
    speeds_estimated: bool = False
    error: str = ""

    def estimate_speeds(self) -> None:
        """Достроить скорости по своим замерам, если сервер их не прислал.

        `timings` приходят только от сборок, понимающих `stream_options`
        (см. `LocalAgent.complete`). Старая сборка это поле игнорирует, и три
        метрики скорости молча остаются нулями: прогон выглядит так, будто
        модель не работала, хотя ответ получен и время замерено. Токены в этом
        случае уже посчитаны через `/tokenize`, так что скорости есть из чего
        вывести. Пропустить это и оставить нули — значит показать в сравнении
        «0 t/s» там, где модель выдавала сотню.

        Заполняются только пустые поля: если сервер прислал точные `timings`,
        ничего не пересчитывается.
        """
        if not self.predicted_per_second:
            value = estimate_generation_speed(self.completion_tokens, self.seconds, self.ttft_ms)
            if value:
                self.predicted_per_second = value
                self.speeds_estimated = True
        if not self.prompt_ms and self.ttft_ms:
            # Точного времени префилла без `timings` взять негде, а TTFT —
            # ближайшая к нему величина: это время до первого токена, то есть
            # префилл плюс сеть. Оценка грубее точной, но не ноль.
            self.prompt_ms = round(self.ttft_ms, 1)
            self.speeds_estimated = True
        if not self.prompt_per_second:
            value = estimate_prompt_speed(self.prompt_tokens, self.prompt_ms)
            if value:
                self.prompt_per_second = value
                self.speeds_estimated = True

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def empty(self) -> bool:
        """Ответ пуст, хотя запрос прошёл. Отдельный случай, не ошибка."""
        return not self.error and not self.text

    @property
    def reasoning_tokens_guess(self) -> int:
        """Во сколько токенов обошлось рассуждение — по длине текста."""
        return len(self.reasoning) // 2 + 1 if self.reasoning else 0


class LocalAgent:
    """Клиент llama-server для разовых заданий.

    Не хранит состояние между вызовами: у объекта есть только настройки
    подключения. Всё, что накопилось бы в «переписке», живёт в логе сессии
    на диске, а не в контексте модели.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token_cap: int = DEFAULT_TOKEN_CAP,
        timeout: float = DEFAULT_TIMEOUT,
        sessions_dir: str | Path | None = None,
        retries: int = 3,
        retry_delay: float = 5.0,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token_cap = int(token_cap)
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.retry_delay = float(retry_delay)
        self.sessions_dir = Path(sessions_dir) if sessions_dir else (app_root() / "sessions")
        self._props: dict[str, Any] | None = None
        self._tokenize_ok = True
        self._tokenize_failures = 0
        self._session_counter = 0

    # ------------------------------------------------------------------
    # сервер

    def health(self, timeout: float = 5.0) -> dict[str, Any]:
        """Проверить, поднят ли сервер."""
        try:
            r = requests.get(self.base_url + "/health", timeout=timeout)
            return r.json()
        except Exception as exc:  # noqa: BLE001 — сеть бывает любой
            raise ServerUnavailable("сервер %s недоступен: %s" % (self.base_url, exc)) from exc

    def warmup(self, *, timeout: float = 30.0) -> bool:
        """Прогреть сервер одним минимальным запросом.

        Первый запрос после загрузки модели считается дольше остальных: на нём
        захват CUDA-графов, аллокация страниц KV-кэша, прогрев весов. Для
        метрик скорости это прямой выброс, и он попадает в первый же кейс
        набора. Запрос намеренно крошечный (один токен ответа), и его
        результат никуда не идёт — нужен только сам факт прохода по всему
        пути от payload до токена.
        """
        try:
            comp = self.complete(
                [{"role": "user", "content": "ok"}],
                temperature=0.0,
                max_tokens=1,
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 — прогрев вспомогателен, не провал
            return False
        return not comp.error

    def props(self, refresh: bool = False) -> dict[str, Any]:
        """Метаданные сервера. Читаем один раз — они не меняются на ходу."""
        if self._props is None or refresh:
            try:
                r = requests.get(self.base_url + "/props", timeout=15)
                r.raise_for_status()
                self._props = r.json()
            except Exception as exc:  # noqa: BLE001
                raise ServerUnavailable("не удалось прочитать /props: %s" % exc) from exc
        return self._props or {}

    @property
    def model_name(self) -> str:
        """Имя модели из /props (по нему видно, что реально загружено)."""
        try:
            p = self.props()
        except ServerUnavailable:
            return "?"
        path = str(p.get("model_path") or p.get("model_alias") or "")
        return Path(path).name or "?"

    @property
    def context_size(self) -> int:
        """Полный контекст сервера — верхняя граница, выше бюджета задачи."""
        try:
            p = self.props()
        except ServerUnavailable:
            return 0
        gen = p.get("default_generation_settings") or {}
        return int(gen.get("n_ctx") or 0)

    def _slots(self, timeout: float = 3.0) -> list[dict[str, Any]] | None:
        """Состояние слотов сервера. `None` — спросить не удалось.

        Отдельным методом, потому что «не удалось спросить» и «свободен» —
        разные вещи, а вызывающему нужен именно ответ на вопрос «занят?».
        """
        try:
            r = requests.get(self.base_url + "/slots", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — сеть бывает любой
            log.warning("slots недоступен: %s", exc)
            return None
        if r.status_code != 200:
            # Сборка может быть собрана без `--slots` — это не ошибка.
            log.info("slots: код %s — состояние слотов неизвестно", r.status_code)
            return None
        try:
            slots = r.json()
        except ValueError as exc:
            log.warning("slots: ответ не разобран: %s", exc)
            return None
        if not isinstance(slots, list):
            return None
        return [s for s in slots if isinstance(s, dict)]

    def server_busy(
        self, *, samples: int = 1, pause: float = 0.3, timeout: float = 3.0
    ) -> str | None:
        """Занят ли сервер посторонним запросом. `None` — свободен или не узнать.

        Зачем это нужно. У сборки llama.cpp один слот (`total_slots: 1`),
        поэтому чужой запрос встаёт впереди наших: пока он считается, замеры
        показывают скорость очереди, а не модели. Первым врёт префилл — он
        измеряется по времени до первого токена, а туда как раз и попадает
        ожидание чужого запроса.

        Смотрим на `/slots`: признак `is_processing` и номер текущей задачи
        (`id_task`). При `samples > 1` номер сравнивается между пробами —
        так ловится запрос, начавшийся между ними, без ожидания внутри одной
        пробы. Если `/slots` недоступен (сборка без `--slots`, сеть), вернём
        `None`: предупреждать не о чем, и прогон из-за незнания падать не должен.
        """
        seen: set[Any] = set()
        for index in range(max(1, int(samples))):
            if index:
                time.sleep(max(0.0, float(pause)))
            slots = self._slots(timeout)
            if slots is None:
                return None
            busy = [s for s in slots if s.get("is_processing")]
            if busy:
                return "идёт обработка запроса, занят слот %s" % busy[0].get("id")
            current = {s.get("id_task") for s in slots}
            if seen and current != seen:
                return "между проверками сменилась задача: %s → %s" % (
                    sorted(seen, key=str),
                    sorted(current, key=str),
                )
            seen = current
        return None

    # ------------------------------------------------------------------
    # токены

    def count_tokens(self, text: str) -> int:
        """Точное число токенов через /tokenize.

        Если эндпоинт недоступен (старая сборка), считаем по символам с
        завышением — лучше перестраховаться, чем обрезать контекст молча.

        Отказом считается не первый сбой, а `TOKENIZE_FAIL_LIMIT` подряд:
        одиночный сбой — это состояние момента (сервер занят загрузкой), а не
        свойство сборки. Раньше флаг снимался навсегда, и одиночный сбой
        переводил весь оставшийся прогон на оценку — незаметно для отчёта.
        """
        if not text:
            return 0
        if self._tokenize_ok:
            try:
                r = requests.post(
                    self.base_url + "/tokenize",
                    json={"content": text},
                    timeout=60,
                )
                if r.status_code == 200:
                    self._tokenize_failures = 0
                    return len(r.json().get("tokens") or [])
                log.warning("tokenize: код %s, длину считаю оценочно", r.status_code)
            except Exception as exc:  # noqa: BLE001 — сеть бывает любой
                log.warning("tokenize недоступен: %s", exc)
            self._tokenize_failures += 1
            if self._tokenize_failures >= TOKENIZE_FAIL_LIMIT:
                self._tokenize_ok = False
                log.warning(
                    "tokenize: %d сбоя подряд — до конца прогона длина считается "
                    "оценочно (около двух символов на токен)",
                    self._tokenize_failures,
                )
        return self.estimate_tokens(text)

    @property
    def tokenize_exact(self) -> bool:
        """Считается ли длина точно (`/tokenize`) или оценочно.

        Нужен проверкам: если счёт переключился посреди сборки промпта, длина
        такого промпта ничего не говорит о генераторе.
        """
        return self._tokenize_ok

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Грубая оценка с завышением: ~2 символа на токен для русского."""
        return len(text) // 2 + 1

    # ------------------------------------------------------------------
    # сборка промпта

    @staticmethod
    def _lang_for(path: Path) -> str:
        return _LANG_BY_SUFFIX.get(path.suffix.lower(), "text")

    def read_attachment(self, path: str | Path) -> tuple[str, str]:
        """Прочитать файл для вложения. Возвращает (заголовок, содержимое)."""
        p = Path(path)
        if not p.is_file():
            return str(p), "(файл не найден)"
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp1251")
            except UnicodeDecodeError:
                return str(p), "(двоичный файл, не приложен)"
        note = ""
        if len(raw) > MAX_FILE_BYTES:
            text = text[:MAX_FILE_BYTES]
            note = "\n… (файл обрезан до %d КБ)" % (MAX_FILE_BYTES // 1024)
        return str(p), text + note

    def compose(
        self,
        task: str,
        system: str | None = None,
        files: list[str] | None = None,
    ) -> tuple[str, str, list[str]]:
        """Собрать системную подсказку и сообщение пользователя.

        Файлы подмешиваются в то же единственное сообщение: отдельная
        «переписка» тут не нужна, а лишние ходы только съедают контекст.
        """
        sys_text = system if system is not None else DEFAULT_SYSTEM
        blocks: list[str] = [str(task).strip()]
        attached: list[str] = []
        for f in files or []:
            head, body = self.read_attachment(f)
            attached.append(head)
            lang = self._lang_for(Path(head))
            blocks.append("### Файл: %s\n```%s\n%s\n```" % (head, lang, body))
        return sys_text, "\n\n".join(blocks), attached

    # ------------------------------------------------------------------
    # запрос

    def _post_chat(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any], float]:
        """Отправить запрос потоком.

        Возвращает (ответ, рассуждение, служебные данные, TTFT мс).

        Потоком, а не одним куском, по двум причинам: видно время до первого
        токена, и длинная генерация не упирается в таймаут чтения.

        Рассуждение собирается отдельно намеренно. У gemma-4 (и других
        «думающих» моделей) цепочка размышлений приходит в поле
        `reasoning_content`, а `content` остаётся пустым, пока она не
        закончится. Если это поле не читать, ответ выглядит пустым при
        `finish_reason=length` — и легко решить, что модель сломалась.
        """
        url = self.base_url + "/v1/chat/completions"
        payload = dict(payload)
        payload["stream"] = True
        started = time.monotonic()
        ttft_ms = 0.0
        chunks: list[str] = []
        think: list[str] = []
        meta: dict[str, Any] = {}

        with requests.post(url, json=payload, timeout=self.timeout, stream=True) as r:
            if r.status_code != 200:
                body = r.text[:400]
                raise RuntimeError("HTTP %d: %s" % (r.status_code, body))
            # Поток читаем как байты и декодируем сами. `iter_lines(decode_unicode=True)`
            # здесь нельзя: у ответа `text/event-stream` не указан charset, и
            # requests берёт ISO-8859-1 — русский текст приезжает кракозябрами
            # («Ð³Ð¾ÑÐ¾Ð²» вместо «готов»).
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("timings"):
                    meta["timings"] = obj["timings"]
                if obj.get("usage"):
                    meta["usage"] = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    thought = delta.get("reasoning_content")
                    if piece or thought:
                        if not chunks and not think:
                            ttft_ms = (time.monotonic() - started) * 1000.0
                    if thought:
                        think.append(thought)
                    if piece:
                        chunks.append(piece)
                    if choice.get("finish_reason"):
                        meta["finish_reason"] = choice["finish_reason"]
        return "".join(chunks), "".join(think), meta, ttft_ms

    def complete(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        top_p: float = 0.95,
        max_tokens: int = 512,
        seed: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Completion:
        """Один запрос к серверу с метриками.

        Повторы — только на сбоях стенда (503, «Loading model», обрыв связи).
        Содержательная ошибка повторяется бессмысленно: тот же вход даст тот
        же выход, а время прогона утроится.
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "cache_prompt": False,
            # Без этого в потоке нет ни usage, ни timings: сервер присылает
            # последним блоком только текст, и метрики приходится угадывать
            # по длине строки. С включённым — приходят точные числа и скорость
            # префилла отдельно от скорости генерации.
            "stream_options": {"include_usage": True},
        }
        if seed is not None and seed >= 0:
            payload["seed"] = seed
        if extra:
            payload.update(extra)

        result = Completion()
        last_error = ""
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            try:
                text, think, meta, ttft_ms = self._post_chat(payload)
            except Exception as exc:  # noqa: BLE001 — сеть, таймаут, HTTP
                last_error = str(exc)
                result.error = last_error
                if attempt < self.retries and is_infra_error(last_error):
                    time.sleep(self.retry_delay)
                    continue
                return result

            result.seconds = round(time.monotonic() - t0, 3)
            result.ttft_ms = round(ttft_ms, 1)
            result.text = text.strip()
            result.reasoning = think.strip()
            result.stop_reason = str(meta.get("finish_reason") or "")
            usage = meta.get("usage") or {}
            result.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            result.completion_tokens = int(usage.get("completion_tokens") or 0)
            if not result.completion_tokens:
                # Старая сборка без stream_options: считаем сами, иначе метрика
                # молча останется нулевой и прогон будет выглядеть странно.
                result.completion_tokens = self.count_tokens(result.text) + self.count_tokens(
                    result.reasoning
                )
            timings = meta.get("timings") or {}
            result.predicted_per_second = float(timings.get("predicted_per_second") or 0.0)
            result.prompt_ms = float(timings.get("prompt_ms") or 0.0)
            result.prompt_per_second = float(timings.get("prompt_per_second") or 0.0)
            # Старая сборка `timings` не присылает вовсе — тогда скорости
            # считаем сами. Оставить нули значило бы показать в отчёте и в
            # сравнении «0 t/s» у модели, которая выдавала сотню.
            result.estimate_speeds()
            result.error = ""
            return result

        result.error = last_error or "не удалось выполнить запрос"
        return result

    def ask(
        self,
        task: str,
        system: str | None = None,
        files: list[str] | None = None,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 8192,
        tag: str = "",
        dry_run: bool = False,
        save: bool = True,
    ) -> Session:
        """Выполнить одну задачу в новой сессии.

        `dry_run=True` — посчитать токены и вернуться, ничего не отправляя.
        Это удобно, чтобы заранее прикинуть, влезает ли задача в бюджет.
        """
        sys_text, user_text, attached = self.compose(task, system, files)

        n_sys = self.count_tokens(sys_text)
        n_user = self.count_tokens(user_text)
        prompt_tokens = n_sys + n_user + TEMPLATE_OVERHEAD

        self._session_counter += 1
        now = datetime.now()
        sid = "%s-%02d-%s" % (
            now.strftime("%Y%m%d-%H%M%S"),
            self._session_counter,
            slugify(tag or task),
        )
        session = Session(
            id=sid,
            task=str(task),
            system=sys_text,
            model=self.model_name,
            base_url=self.base_url,
            started=now.isoformat(timespec="seconds"),
            files=attached,
            prompt_tokens=prompt_tokens,
            token_cap=self.token_cap,
            prompt=user_text,
        )

        if prompt_tokens > self.token_cap:
            session.error = (
                "промпт %d токенов не влезает в бюджет %d (системная часть %d, "
                "задача %d, шаблон %d). Сократи задачу или разбей её на части."
                % (prompt_tokens, self.token_cap, n_sys, n_user, TEMPLATE_OVERHEAD)
            )
            if save:
                self.save_session(session)
            raise BudgetExceeded(session.error)

        if dry_run:
            session.stop_reason = "dry-run"
            if save:
                self.save_session(session)
            return session

        comp = self.complete(
            [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user_text},
            ],
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        session.seconds = comp.seconds
        session.ttft_ms = comp.ttft_ms
        session.reply = comp.text
        session.reasoning = comp.reasoning
        session.stop_reason = comp.stop_reason
        session.completion_tokens = comp.completion_tokens or (
            self.count_tokens(comp.text) + self.count_tokens(comp.reasoning)
        )
        session.reasoning_tokens = self.count_tokens(comp.reasoning)
        if comp.predicted_per_second:
            session.tps = round(comp.predicted_per_second, 2)
        elif comp.seconds > 0 and session.completion_tokens:
            session.tps = round(session.completion_tokens / comp.seconds, 2)
        session.error = comp.error

        session.finished = datetime.now().isoformat(timespec="seconds")
        if save:
            self.save_session(session)
        return session

    # ------------------------------------------------------------------
    # журнал сессий

    def save_session(self, session: Session) -> Session:
        """Записать сессию в sessions/<дата>/ двумя файлами: JSON и MD."""
        try:
            day = session.id[:8]
            folder = self.sessions_dir / ("%s-%s-%s" % (day[:4], day[4:6], day[6:8]))
            folder.mkdir(parents=True, exist_ok=True)
            stem = session.id[9:] if len(session.id) > 9 else session.id

            json_path = folder / ("%s.json" % stem)
            write_json_atomic(json_path, session.to_dict())
            md_path = folder / ("%s.md" % stem)
            write_text_atomic(md_path, self.render_markdown(session))

            session.log_json = str(json_path)
            session.log_md = str(md_path)
        except OSError as exc:
            # Журнал — удобство, а не условие работы: не смогли записать,
            # результат всё равно возвращаем. Но причину пишем в лог.
            log.warning("сессия %s не записана: %s", session.id, exc)
        return session

    @staticmethod
    def render_markdown(s: Session) -> str:
        """Человекочитаемый отчёт по сессии."""
        lines = [
            "# Сессия %s" % s.id,
            "",
            "- модель: `%s`" % s.model,
            "- сервер: %s" % s.base_url,
            "- начата: %s" % s.started,
            "- закончена: %s" % (s.finished or "—"),
            "- токенов на входе: **%d** (бюджет %d)" % (s.prompt_tokens, s.token_cap),
            "- токенов на выходе: %d" % s.completion_tokens,
        ]
        if s.reasoning_tokens:
            lines.append(
                "    - из них на рассуждение: %d, на ответ: %d"
                % (s.reasoning_tokens, max(0, s.completion_tokens - s.reasoning_tokens))
            )
        lines += [
            "- время: %.2f с, до первого токена %.0f мс, %.1f t/s" % (s.seconds, s.ttft_ms, s.tps),
            "- причина остановки: %s" % (s.stop_reason or "—"),
        ]
        if s.files:
            lines.append("- приложено файлов: %d" % len(s.files))
            for f in s.files:
                lines.append("    - `%s`" % f)
        if s.error:
            lines += ["", "## Ошибка", "", "```", s.error, "```"]
        lines += ["", "## Задача", "", s.task, ""]
        if s.reasoning:
            lines += ["## Рассуждение", "", s.reasoning, ""]
        lines += ["## Ответ", "", s.reply or "_(пусто)_", ""]
        return "\n".join(lines)

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Последние сессии — по журналу на диске."""
        if not self.sessions_dir.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.rglob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            data["_path"] = str(path)
            found.append(data)
            if len(found) >= limit:
                break
        return found
