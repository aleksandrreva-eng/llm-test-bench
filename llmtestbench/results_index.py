"""Индекс прогонов по JSON-файлам папки results/.

Источник — JSON, а не SQLite. Причина простая: у прогонов, перенесённых в
базу из папки, счёт пустой (нули ставит `migrate_json_to_db`, потому что
разбирать `cases[]` ради двух чисел она не умеет), и история показывала
«0 из 0» там, где в JSON честно лежит «13 из 15». JSON — первичная запись
прогона: его пишет `TestRunner.save`, он же идёт в HTML-отчёт.

Модуль общий для двух экранов, которым нужны одни и те же данные:

* «История» — список прогонов с фильтрами и счётом;
* «Наборы тестов» — счёт последнего прогона выбранной модели по каждому набору.

Раньше разбор лежал внутри `history_tab.py`; вынести пришлось потому, что
тянуть в панель наборов импорт вкладки истории (а с ней — базу, корзину и
диалоги) ради одной функции неправильно.

Здесь же лежит **группировка** файлов в «прогон целиком» (`RunGroup`). Файл —
это один набор одного запуска, а одно нажатие «Запустить» пишет столько
файлов, сколько отмечено наборов. История показывает именно прогон, и
склейка — часть разбора данных, а не оформления.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Ключи, под которыми имя набора лежит в результатах разных версий.
_SET_KEYS = ("set_id", "test_set", "test_type", "type")
_MODEL_KEYS = ("model", "model_name", "model_path")

#: Промежуток между прогонами, внутри которого файлы без `batch_id` считаются
#: одной партией. Наборы в партии идут встык — пауза между ними секунды,
#: поэтому окно взято с запасом, но заведомо меньше перерыва между отдельными
#: запусками, которые разделяют минуты.
INFER_BATCH_WINDOW_SEC = 120

#: Хвост `batch_id` — метка времени. Имя модели перед ней содержит дефисы и
#: подчёркивания, поэтому режем не по разделителю, а по образцу.
_BATCH_STAMP_RE = re.compile(r"(\d{8}_\d{6})$")


@dataclass
class RunRecord:
    """Один прогон, как он виден в истории."""

    run_id: str = ""
    #: Идентификатор запуска целиком. Пусто у прогонов, сделанных до его
    #: появления, — такие файлы история собирает в группы по времени.
    batch_id: str = ""
    path: Path = Path()
    file_name: str = ""
    model: str = ""
    model_file: str = ""
    set_id: str = ""
    set_name: str = ""
    set_version: str = ""
    status: str = "?"
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    runs: int = 0
    limit: int = 0
    tags: list[str] = field(default_factory=list)
    context_size: int = 0
    # --- счёт прогона (из summary) ---
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    stand_errors: int = 0
    empty_answers: int = 0
    counted: int = 0
    score: float | None = None
    # --- усреднённые метрики ---
    tokens_per_sec: float = 0.0
    latency_ms: float = 0.0
    raw: dict = field(default_factory=dict)
    error: str = ""

    # ------------------------------------------------------------------

    @property
    def short_id(self) -> str:
        """Имя прогона без модели — модель и так стоит в соседней колонке.

        `run_id` собирается как `{модель}_{набор}_{метка времени}`, поэтому
        имя модели в первой колонке таблицы дублировало вторую колонку и
        съедало половину ширины. Режем по позиции набора.
        """
        rid = self.run_id
        if self.set_id and self.set_id in rid:
            return rid[rid.find(self.set_id) :]
        return rid

    @property
    def scored(self) -> bool:
        """Есть ли у прогона счёт.

        Наборы без проверок (`speed`, чистые метрики) дают `counted = 0` и
        `score = None`: показывать «0 из 0» как провал нельзя, поэтому такие
        прогоны помечаются отдельно.
        """
        return self.score is not None and self.counted > 0

    @property
    def status_ru(self) -> str:
        """Статус словами. «Без проверок» важнее «готово».

        У наборов без проверок (`speed`, чистые метрики) прогон завершается
        успешно, но счёта у него нет. Написать «готово» — значит намекнуть,
        что кейсы пройдены; правильнее сказать, что проверять было нечего.
        """
        state = (self.status or "").lower()
        if state == "cancelled":
            return "остановлен"
        if state == "failed":
            return "сбой"
        if not self.scored:
            return "без проверок"
        if state == "finished":
            return "готово"
        return self.status or "—"

    @property
    def date(self):
        """Дата прогона как `QDate` — для фильтра по периоду."""
        return _date_for(self.started, self.finished, self.path)

    @property
    def date_text(self) -> str:
        """«24.09 03:33» — коротко, но с временем: прогоны идут пачками."""
        return _date_text(self.started or self.finished)

    @property
    def seconds_text(self) -> str:
        return seconds_text(self.seconds)


def _date_for(started: str, finished: str, path: Path | None = None):
    """Дата прогона как `QDate`.

    Порядок попыток: время старта, время окончания, метка из имени файла.
    Последнее — для прогонов, у которых времени в JSON нет вовсе.
    """
    from PySide6.QtCore import QDate

    for value in (started, finished):
        if not value:
            continue
        dt = _parse_datetime(value)
        if dt is not None:
            return QDate(dt.year, dt.month, dt.day)
    if path is not None:
        # Имя файла по ТЗ: {модель}_{набор}_{yyyymmdd_HHMMSS}
        for chunk in reversed(Path(path).stem.split("_")):
            if len(chunk) >= 8 and chunk[:8].isdigit():
                y, m, d = int(chunk[:4]), int(chunk[4:6]), int(chunk[6:8])
                if 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                    return QDate(y, m, d)
    return QDate()


def _date_text(value: str) -> str:
    """«24.09 03:33» — коротко, но с временем: прогоны идут пачками."""
    dt = _parse_datetime(value)
    return dt.strftime("%d.%m %H:%M") if dt else "—"


def seconds_text(seconds: float) -> str:
    """Длительность словами: секунды до минуты, дальше «мин с».

    Публичная: её же показывает время кейса в дереве истории, и вторая такая
    функция разошлась бы с первой форматом.
    """
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return "%.1f с" % seconds
    minutes = int(seconds // 60)
    return "%d мин %02d с" % (minutes, int(seconds % 60))


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _first(data: dict, keys, default=None):
    """Первое непустое значение по списку возможных ключей."""
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def model_key(name: str) -> str:
    """Ключ сравнения моделей: без пути и без расширения, в нижнем регистре.

    В JSON лежит `model` = имя файла с `.gguf`, в интерфейсе — имя файла без
    пути. Сравнивать их «в лоб» нельзя, иначе счёт прошлого прогона не
    найдётся и панель наборов скажет «не гонялся» про уже пройденный набор.
    """
    if not name:
        return ""
    return Path(str(name)).stem.lower()


def parse_run_file(path: Path) -> RunRecord:
    """Прочитать файл прогона. Ошибку кладём в запись, а не бросаем."""
    rec = RunRecord(path=path, file_name=path.name, run_id=path.stem)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rec.error = "не прочитать: %s" % exc
        rec.status = "failed"
        return rec
    if not isinstance(data, dict):
        rec.error = "не JSON-объект"
        rec.status = "failed"
        return rec

    rec.raw = data
    rec.run_id = str(data.get("run_id") or path.stem)
    rec.batch_id = str(data.get("batch_id") or "")
    rec.model_file = str(_first(data, _MODEL_KEYS, "") or "")
    rec.model = model_key(rec.model_file) or rec.model_file
    rec.set_id = str(_first(data, _SET_KEYS, "") or "")
    rec.set_name = str(data.get("set_name") or "")
    rec.set_version = str(data.get("set_version") or "")
    rec.status = str(data.get("status") or "")
    rec.started = str(data.get("started") or data.get("started_at") or "")
    rec.finished = str(data.get("finished") or "")
    rec.seconds = float(data.get("seconds") or 0.0)
    rec.context_size = int(data.get("context_size") or 0)

    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    rec.runs = int(params.get("runs") or data.get("runs") or 0)
    rec.limit = int(params.get("limit") or 0)
    tags = params.get("tags")
    rec.tags = [str(t) for t in tags] if isinstance(tags, list) else []

    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    rec.total = int(summary.get("total") or 0)
    rec.passed = int(summary.get("passed") or 0)
    rec.failed = int(summary.get("failed") or 0)
    rec.skipped = int(summary.get("skipped") or 0)
    rec.stand_errors = int(summary.get("stand_errors") or 0)
    rec.empty_answers = int(summary.get("empty_answers") or 0)
    rec.counted = int(summary.get("counted") or 0)
    score = summary.get("score")
    rec.score = float(score) if isinstance(score, (int, float)) else None

    # Метрики: средние по кейсам. Верхнего уровня в файле нет, а по кейсам
    # они есть всегда — иначе колонки Tok/s и «Время» стояли бы пустыми.
    cases = data.get("cases") if isinstance(data.get("cases"), list) else []
    tps = [
        c["tokens_per_sec"]
        for c in cases
        if isinstance(c, dict) and isinstance(c.get("tokens_per_sec"), (int, float))
    ]
    lat = [
        c["total_ms"]
        for c in cases
        if isinstance(c, dict) and isinstance(c.get("total_ms"), (int, float))
    ]
    if tps:
        rec.tokens_per_sec = round(sum(tps) / len(tps), 1)
    if lat:
        rec.latency_ms = round(sum(lat) / len(lat), 0)
    return rec


def load_runs(cfg) -> list[RunRecord]:
    """Все читаемые прогоны из папки результатов, свежие первыми.

    Битые файлы пропускаем молча: один недописанный JSON (например, прогон
    оборвали) не должен прятать от пользователя остальные девять.
    """
    directory = Path(cfg.results_path)
    if not directory.is_dir():
        return []
    out: list[RunRecord] = []
    for path in sorted(directory.glob("*.json")):
        rec = parse_run_file(path)
        if rec.error:
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.started or r.finished or "", r.file_name), reverse=True)
    return out


def latest_by_set(records: list[RunRecord], model: str = "") -> dict[str, RunRecord]:
    """Последний прогон каждого набора.

    `records` ожидается отсортированным «свежие первыми» (как отдаёт
    `load_runs`), поэтому первое встреченное значение по набору — и есть
    последнее. Если задана модель, берутся только её прогоны: счёт чужой
    модели в строке набора вводил бы в заблуждение.
    """
    out: dict[str, RunRecord] = {}
    wanted = model_key(model)
    for rec in records:
        if not rec.set_id:
            continue
        if wanted and model_key(rec.model) != wanted:
            continue
        out.setdefault(rec.set_id, rec)
    return out


def available_models(records: list[RunRecord]) -> list[str]:
    """Имена моделей, по которым есть прогоны, — по убыванию свежести."""
    seen: list[str] = []
    for rec in records:
        if rec.model and rec.model not in seen:
            seen.append(rec.model)
    return seen


def available_sets(records: list[RunRecord]) -> list[str]:
    """Идентификаторы наборов, по которым есть прогоны, — по убыванию свежести."""
    seen: list[str] = []
    for rec in records:
        if rec.set_id and rec.set_id not in seen:
            seen.append(rec.set_id)
    return seen


def run_tooltip(group: RunGroup) -> str:
    """Подсказка о прогоне: чем он собран и из чего.

    Одна на шапку колонки сравнения и на строку «Прогон» в покейсовом диалоге:
    два экрана об одном прогоне не должны называть его по-разному.
    """
    stats = group.stats
    lines = [
        "Прогон: %s" % (group.batch_id or "точного идентификатора нет"),
        "Модель: %s" % (group.model or "—"),
        "Время: %s · %s" % (group.date_text, group.seconds_text),
        "Наборов: %d (%s)" % (len(group.set_ids), ", ".join(group.set_ids) or "—"),
        "Кейсов: %d, из них с проверкой: %d" % (stats.cases, stats.counted),
    ]
    if group.inferred:
        lines.append(
            "Группа собрана по времени: файлы записаны до появления идентификатора прогона."
        )
    return "\n".join(lines)


def column_labels(groups: list[RunGroup]) -> list[str]:
    """Подписи прогонов для колонок сравнения — имя модели.

    Только имя модели, без метки времени: в шапке сравнения колонка бывает
    шириной в треть экрана, а «gemma-4-26B-A4B-it-qat-UD-Q4_K_XL · 24.09 06:22»
    не влезает и в половину — Qt рисует такой заголовок поверх соседнего
    столбца. Дата и время стоят первой строкой таблицы («Прогон»), где место
    есть, а полное имя модели — в подсказке шапки.

    Живёт здесь, а не во вкладке: подписи колонок нужны и сводному сравнению,
    и покейсовому диалогу, а две копии разошлись бы при первой правке.
    """
    return [g.model or g.short_id for g in groups]


# ----------------------------------------------------------------------
# прогон целиком


@dataclass
class RunStats:
    """Счёт и средние метрики на одном уровне детализации.

    Одна структура обслуживает **два** уровня сравнения — прогон целиком и
    набор внутри прогона: числа считает одна функция, поэтому «13/15» в сводной
    строке и сумма по развёрнутым наборам разойтись не могут. Раньше каждый
    экран считал своё, и расхождение было вопросом времени.

    Средние берутся по кейсам (`tokens_per_sec`, `total_ms`, `ttft_ms`): на
    верхнем уровне файла этих полей нет вовсе, они есть только в `cases[]`.

    `RunGroup.total/passed/counted` (из `summary`) и `stats` считаются из
    разных источников, но обязаны совпадать — это стережёт `check_compare.py`.
    Оставлены оба: историю устраивает дешёвый счёт из `summary`, а сравнению
    нужна ещё и разбивка по наборам со средними.
    """

    set_id: str = ""
    set_name: str = ""
    set_version: str = ""
    cases: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    stand_errors: int = 0
    empty_answers: int = 0
    seconds: float = 0.0
    tokens_per_sec: float = 0.0
    #: Скорость обработки промпта. Отдельная метрика, а не «ещё одна
    #: скорость»: на длинных контекстах именно она различает модели — на
    #: наборе `speed` префилл расходится в разы, тогда как генерация держится
    #: примерно одинаково у всех.
    prompt_tokens_per_sec: float = 0.0
    latency_ms: float = 0.0
    ttft_ms: float = 0.0
    #: Хотя бы у одного кейса скорость посчитана по своим замерам, а не
    #: пришла от сервера в `timings`. В интерфейсе такие числа помечаются «≈».
    speeds_estimated: bool = False

    @property
    def total(self) -> int:
        """Сколько кейсов было — знаменатель для «13 из 15»."""
        return self.cases

    @property
    def counted(self) -> int:
        """Знаменатель счёта: пройденные и проваленные.

        Пропуск (`passed is None`) в него не идёт — иначе набор без проверок
        (`speed`) портил бы долю всем соседям по прогону.
        """
        return self.passed + self.failed

    @property
    def score(self) -> float | None:
        return round(self.passed / self.counted, 4) if self.counted else None

    @property
    def scored(self) -> bool:
        """Есть ли что показывать в колонке «Качество»."""
        return self.score is not None


def _mean(values: list[float]) -> float:
    """Среднее с округлением; пустой список — ноль, а не деление на ноль."""
    return round(sum(values) / len(values), 1) if values else 0.0


def _stats_of(rec: RunRecord) -> RunStats:
    """Метрики одного файла прогона — он же один набор.

    Флаги берутся покейсово, а не из `summary`: только так получается разбивка
    по наборам, и только она совпадает с суммой по развёрнутым строкам. Для
    файла без разобранных кейсов (прогон оборван на записи) остаётся `summary`
    — показать нули там, где в файле честные «13 из 15», было бы враньём.
    """
    stats = RunStats(
        set_id=rec.set_id,
        set_name=rec.set_name,
        set_version=rec.set_version,
        seconds=rec.seconds,
    )
    raw = rec.raw if isinstance(rec.raw, dict) else {}
    cases = raw.get("cases") if isinstance(raw.get("cases"), list) else []
    cases = [c for c in cases if isinstance(c, dict)]

    if not cases:
        stats.cases = rec.total
        stats.passed, stats.failed = rec.passed, rec.failed
        stats.skipped = rec.skipped
        stats.stand_errors = rec.stand_errors
        stats.empty_answers = rec.empty_answers
        return stats

    stats.cases = len(cases)
    for case in cases:
        if case.get("passed") is True:
            stats.passed += 1
        elif case.get("passed") is False:
            stats.failed += 1
        else:
            stats.skipped += 1
        if case.get("stand_error"):
            stats.stand_errors += 1
        if case.get("empty"):
            stats.empty_answers += 1

    stats.tokens_per_sec = _mean(
        [
            float(c["tokens_per_sec"])
            for c in cases
            if isinstance(c.get("tokens_per_sec"), (int, float))
        ]
    )
    stats.prompt_tokens_per_sec = _mean(
        [
            float(c["prompt_tokens_per_sec"])
            for c in cases
            if isinstance(c.get("prompt_tokens_per_sec"), (int, float))
        ]
    )
    stats.speeds_estimated = any(c.get("speeds_estimated") for c in cases)
    stats.latency_ms = _mean(
        [float(c["total_ms"]) for c in cases if isinstance(c.get("total_ms"), (int, float))]
    )
    stats.ttft_ms = _mean(
        [float(c["ttft_ms"]) for c in cases if isinstance(c.get("ttft_ms"), (int, float))]
    )
    return stats


def _merge_stats(parts: list[RunStats]) -> RunStats:
    """Слить метрики одного набора из нескольких файлов.

    Сливать приходится потому, что партия, собранная по времени, может
    содержать один набор дважды (оборванный и перезапущенный прогон). Две
    строки «chat_single» в развороте читались бы как разные наборы.

    Средние сливаются **взвешенно по числу кейсов**: у файлов разное число
    кейсов, и среднее двух средних перекосило бы результат в сторону короткого.
    """
    if not parts:
        return RunStats()
    if len(parts) == 1:
        return parts[0]

    out = RunStats(
        set_id=parts[0].set_id, set_name=parts[0].set_name, set_version=parts[0].set_version
    )
    for part in parts:
        out.cases += part.cases
        out.passed += part.passed
        out.failed += part.failed
        out.skipped += part.skipped
        out.stand_errors += part.stand_errors
        out.empty_answers += part.empty_answers
        out.seconds = round(out.seconds + part.seconds, 2)
    weight = sum(p.cases for p in parts)
    for field_name in ("tokens_per_sec", "prompt_tokens_per_sec", "latency_ms", "ttft_ms"):
        total = sum(getattr(p, field_name) * p.cases for p in parts)
        setattr(out, field_name, round(total / weight, 1) if weight else 0.0)
    out.speeds_estimated = any(p.speeds_estimated for p in parts)
    return out


@dataclass
class RunGroup:
    """Прогон целиком: все файлы, записанные одним нажатием «Запустить».

    Файл — это один набор одного запуска. Нажатие по семи наборам писало семь
    файлов, и история показывала семь строк, между которыми не было видно
    связи. Группа собирает их обратно: одна строка — один прогон, а кейсы
    раскрываются внутри неё.
    """

    key: str = ""
    batch_id: str = ""
    #: Группа собрана по времени, а не по идентификатору. У старых файлов
    #: принадлежность задним числом не восстановить, и показывать догадку как
    #: факт нельзя — такие группы помечаются знаком «≈».
    inferred: bool = False
    runs: list[RunRecord] = field(default_factory=list)

    model: str = ""
    set_ids: list[str] = field(default_factory=list)
    set_names: list[str] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    seconds: float = 0.0

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    stand_errors: int = 0
    empty_answers: int = 0
    counted: int = 0
    score: float | None = None

    # ------------------------------------------------------------------

    @property
    def run_ids(self) -> list[str]:
        return [r.run_id for r in self.runs]

    @property
    def paths(self) -> list[Path]:
        return [r.path for r in self.runs]

    @property
    def scored(self) -> bool:
        """Есть ли у партии счёт. Наборы без проверок дают `counted = 0`."""
        return self.score is not None and self.counted > 0

    @property
    def cases(self) -> list[tuple[RunRecord, dict]]:
        """Кейсы всех файлов партии: `(запись, словарь кейса из JSON)`.

        Порядок — как в файлах, а файлы идут по времени старта: сначала
        первый набор партии, внутри — его кейсы по порядку прогона.
        """
        out: list[tuple[RunRecord, dict]] = []
        for rec in self.runs:
            raw = rec.raw.get("cases") if isinstance(rec.raw, dict) else None
            for case in raw or []:
                if isinstance(case, dict):
                    out.append((rec, case))
        return out

    @property
    def cases_count(self) -> int:
        """Сколько кейсов в партии — для подвала и заголовка отчёта."""
        return sum(
            len(rec.raw.get("cases") or []) for rec in self.runs if isinstance(rec.raw, dict)
        )

    @property
    def stats(self) -> RunStats:
        """Счёт и средние метрики прогона целиком.

        Это то, что показывает колонка сравнения: у прогона из семи наборов
        нет одной «своей» latency, есть сумма по всем его кейсам.
        """
        return _merge_stats([_stats_of(rec) for rec in self.runs])

    @property
    def set_stats(self) -> list[RunStats]:
        """Метрики по каждому набору прогона, в порядке прогона.

        Уровень детализации между прогоном и кейсом: по сводному «13/15» не
        видно, какой набор провален, а по строке набора — видно.
        """
        order: list[str] = []
        buckets: dict[str, list[RunStats]] = {}
        for rec in self.runs:
            key = rec.set_id or rec.run_id
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(_stats_of(rec))
        return [_merge_stats(buckets[key]) for key in order]

    @property
    def context_size(self) -> int:
        """Контекст прогона. У файлов партии он один, берём наибольший.

        Наибольший, а не первый: если наборы грузились с разным контекстом,
        сравнивать прогоны надо по верхней границе — она и объясняет расход
        памяти.
        """
        return max((rec.context_size for rec in self.runs), default=0)

    @property
    def set_versions(self) -> dict[str, str]:
        """Версия каждого набора в прогоне — ключ к предупреждению по п. 11.7 ТЗ.

        У наборов версии меняются, и прогоны по разным версиям сравнивать
        некорректно. Без этой карты такое расхождение в интерфейсе не видно.
        """
        out: dict[str, str] = {}
        for rec in self.runs:
            if rec.set_id:
                out.setdefault(rec.set_id, rec.set_version or "")
        return out

    @property
    def set_label(self) -> str:
        """Один набор — его имя; несколько — «N наборов»."""
        if len(self.set_ids) == 1:
            return self.set_names[0] or self.set_ids[0]
        from .ui.theme import plural

        n = len(self.set_ids)
        return "%d %s" % (n, plural(n, "набор", "набора", "наборов"))

    @property
    def short_id(self) -> str:
        """Короткая метка партии — метка времени без имени модели.

        Модель стоит в соседней колонке, повторять её в идентификаторе незачем.
        У выведенной группы идентификатора нет: берём время старта и
        помечаем строку знаком «≈».
        """
        match = _BATCH_STAMP_RE.search(self.batch_id)
        stamp = match.group(1) if match else ""
        if not stamp:
            dt = _parse_datetime(self.started)
            stamp = (
                dt.strftime("%Y%m%d_%H%M%S")
                if dt
                else (self.runs[0].short_id if self.runs else "—")
            )
        return ("≈ " + stamp) if self.inferred else stamp

    @property
    def status_ru(self) -> str:
        """Статус партии. Старшинство — по худшему из файлов.

        «Сбой» важнее «остановлен»: если один набор упал, партия не удалась,
        и написать по остальным шести «готово» — значит спрятать проблему.
        """
        states = [(r.status or "").lower() for r in self.runs]
        if "failed" in states:
            return "сбой"
        if "cancelled" in states:
            return "остановлен"
        if not self.scored:
            return "без проверок"
        if states and all(state == "finished" for state in states):
            return "готово"
        return "частично"

    @property
    def status_hint(self) -> str:
        """Тултип к статусу: что с каждым файлом партии.

        У партии статус один, а файлов семь. «Сбой» без объяснения не говорит,
        какой набор упал.
        """
        return "\n".join(
            "%s — %s" % (rec.set_id or rec.short_id, rec.status_ru) for rec in self.runs
        )

    @property
    def date(self):
        """Дата старта партии как `QDate` — для фильтра по периоду."""
        return _date_for(self.started, self.finished, self.runs[0].path if self.runs else None)

    @property
    def date_text(self) -> str:
        return _date_text(self.started or self.finished)

    @property
    def seconds_text(self) -> str:
        return seconds_text(self.seconds)


def _aggregate(key: str, batch_id: str, runs: list[RunRecord], inferred: bool) -> RunGroup:
    """Свести файлы одной партии в одну строку истории."""
    ordered = sorted(runs, key=lambda r: (r.started or r.finished or "", r.file_name))
    group = RunGroup(key=key, batch_id=batch_id, inferred=inferred, runs=ordered)
    if not ordered:
        return group

    group.model = ordered[0].model
    for rec in ordered:
        if rec.set_id and rec.set_id not in group.set_ids:
            group.set_ids.append(rec.set_id)
            group.set_names.append(rec.set_name or "")

    group.started = ordered[0].started
    group.finished = ordered[-1].finished
    group.seconds = round(sum(r.seconds for r in ordered), 2)
    group.total = sum(r.total for r in ordered)
    group.passed = sum(r.passed for r in ordered)
    group.failed = sum(r.failed for r in ordered)
    group.skipped = sum(r.skipped for r in ordered)
    group.stand_errors = sum(r.stand_errors for r in ordered)
    group.empty_answers = sum(r.empty_answers for r in ordered)
    # Считаем сами, а не суммируем `rec.counted`: в исполнителе это ровно
    # `passed + failed`, а у файла, собранного не им, поле может быть пустым —
    # тогда партия показывала бы «без проверок» при живых вердиктах.
    group.counted = group.passed + group.failed
    group.score = round(group.passed / group.counted, 4) if group.counted else None
    return group


def _infer_legacy(records: list[RunRecord], window: float) -> list[RunGroup]:
    """Разбить файлы без `batch_id` на партии по времени старта.

    Правило строгое, и оба условия обязательны:

    * новая партия, если пауза от окончания предыдущего прогона до начала
      текущего больше `window` — наборы в партии идут встык;
    * новая партия, если набор текущего файла уже встречался в текущей партии.
      Без этого правила два прогона одного набора подряд (в реальных данных
      есть `chat_single` в 03:33:35 и 03:36:41) слиплись бы в одну партию, и
      история показала бы один прогон вместо двух.

    Точной принадлежности в таких файлах нет — группа помечается `inferred`.
    """
    if not records:
        return []

    ordered = sorted(records, key=lambda r: (r.started or r.finished or "", r.file_name))
    chunks: list[list[RunRecord]] = []
    current: list[RunRecord] = []
    prev_end: datetime | None = None

    for rec in ordered:
        start = _parse_datetime(rec.started or rec.finished)
        end = _parse_datetime(rec.finished) or start
        gap = None
        if prev_end is not None and start is not None:
            gap = (start - prev_end).total_seconds()
        repeated = bool(rec.set_id) and any(r.set_id == rec.set_id for r in current)
        if current and (repeated or (gap is not None and gap > window)):
            chunks.append(current)
            current = []
        current.append(rec)
        prev_end = end or prev_end

    if current:
        chunks.append(current)

    return [_aggregate("legacy:" + chunk[0].run_id, "", chunk, inferred=True) for chunk in chunks]


def group_records(
    records: list[RunRecord], infer_window: float = INFER_BATCH_WINDOW_SEC
) -> list[RunGroup]:
    """Собрать прогоны целиком из отдельных файлов, свежие первыми.

    Файлы с `batch_id` группируются точно. Оставшиеся (записанные до появления
    идентификатора) делятся по времени — см. `_infer_legacy`.
    """
    by_batch: dict[str, list[RunRecord]] = {}
    legacy: list[RunRecord] = []
    for rec in records:
        if rec.batch_id:
            by_batch.setdefault(rec.batch_id, []).append(rec)
        else:
            legacy.append(rec)

    out = [_aggregate(bid, bid, runs, inferred=False) for bid, runs in by_batch.items()]
    out.extend(_infer_legacy(legacy, infer_window))
    out.sort(key=lambda g: (g.started or g.finished or "", g.key), reverse=True)
    return out


def load_groups(cfg, infer_window: float = INFER_BATCH_WINDOW_SEC) -> list[RunGroup]:
    """Прочитать папку результатов и собрать прогоны целиком."""
    return group_records(load_runs(cfg), infer_window)


__all__ = [
    "INFER_BATCH_WINDOW_SEC",
    "RunGroup",
    "RunRecord",
    "RunStats",
    "available_models",
    "available_sets",
    "column_labels",
    "group_records",
    "latest_by_set",
    "load_groups",
    "load_runs",
    "model_key",
    "parse_run_file",
    "run_tooltip",
    "seconds_text",
]
