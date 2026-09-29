"""Управление процессом llama-server (п. 3.4.3 и раздел 4 ТЗ).

Жизненный цикл: stopped → starting → ready → error / stopped.
Одна модель в памяти, смена модели = смена процесса (п. 4.1).
Модуль не знает про Qt: о смене состояния и новых строках лога сообщает
через колбэки, поэтому его одинаково используют и интерфейс, и консольный
прогон (`run_tests.py`).

Важное для этой машины: Инк поднимает модель сам, в лаунчере. Поэтому
`kill_orphans()` убивает ТОЛЬКО те процессы, чей PID приложение само
записало в config.json. Чужой сервер лаунчера не трогаем — иначе можно
выдернуть модель посреди чужого прогона.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import requests

from .config import AppConfig, app_root
from .server_cmd import EXE_NAME, build_command, parse_command, resolve_params

# --- константы из ТЗ ---
LOG_ROTATE_BYTES = 10 * 1024 * 1024  # п. 3.4.5: максимум 10 МБ
HEALTH_INTERVAL_SEC = 0.5  # п. 3.4.3: опрос каждые 500 мс
DEFAULT_HEALTH_TIMEOUT = 60  # п. 3.4.3: таймаут 60 сек
STOP_GRACE_SEC = 5.0  # п. 3.4.3: terminate → 5 сек → kill
PORT_FREE_TIMEOUT = 10  # п. 4.3: ожидание освобождения порта
RESOURCE_RETRIES = 3  # п. 4.6: до 3 проверок освобождения VRAM

# Флаги Windows: своя группа процессов (чтобы послать CTRL_BREAK) и без окна.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    TESTING = "testing"
    ERROR = "error"

    @property
    def ru(self) -> str:
        return {
            "stopped": "остановлен",
            "starting": "запускается",
            "ready": "готов",
            "testing": "идёт тестирование",
            "error": "ошибка",
        }[self.value]


@dataclass
class ServerStatus:
    """Состояние сервера. `to_json()` — ровно то, что просит п. 4.8 ТЗ."""

    state: ServerState = ServerState.STOPPED
    pid: int = 0
    command: str = ""
    host: str = ""
    port: int = 0
    startup_time_ms: int = 0
    model_load_time_ms: int = 0
    shutdown_time_ms: int = 0
    restarts: int = 0
    error: str = ""
    log_path: str = ""
    health_message: str = ""

    def to_json(self) -> dict:
        return {
            "pid": self.pid,
            "command": self.command,
            "host": self.host,
            "port": self.port,
            "startup_time_ms": self.startup_time_ms,
            "model_load_time_ms": self.model_load_time_ms,
            "shutdown_time_ms": self.shutdown_time_ms,
            "restarts": self.restarts,
        }


# ---------------------------------------------------------------------------
# проверки без процесса


# Отказ по loopback на этой машине приходит не сразу, а примерно через
# 2 секунды (замерено: 2.01–2.03 сек, стабильно, на разных портах — похоже
# на фильтр соединений в системе). Таймаут меньше двух секунд объявит
# свободный порт занятым, поэтому берём с запасом.
PORT_PROBE_TIMEOUT_SEC = 3.0


def is_port_busy(host: str, port: int, timeout: float = PORT_PROBE_TIMEOUT_SEC) -> bool:
    """Занят ли порт (п. 4.6: «порт занят перед запуском»).

    Самая капризная проверка в модуле. Что выяснилось на практике:

    1. `connect_ex` на сокете с таймаутом (то есть неблокирующем) почти
       всегда возвращает не результат, а `WSAEWOULDBLOCK` (10035) —
       «соединение ещё устанавливается». Причём и для свободного порта.
       Поэтому `connect_ex(...) == 0` проверять нельзя.
    2. Доиграть соединение через `select` + `SO_ERROR` тоже не выходит:
       свободный порт так и не становится «готовым к записи», и проверка
       объявляет занятым всё подряд.
    3. Блокирующий `connect` с таймаутом работает правильно, но отказ по
       loopback приходит только через ~2 секунды. С коротким таймаутом
       свободный порт выглядит как «молчащий занятый».

    Поэтому: блокирующий connect, таймаут 3 секунды. Живой слушатель
    отвечает за миллисекунды, свободный порт — отказом через 2 секунды.

    Через `bind` проверять нельзя. Во-первых, на Windows `SO_REUSEADDR`
    разрешает встать рядом с чужим слушателем. Во-вторых, сразу после
    остановки своего сервера порт ещё держат соединения в TIME_WAIT, и
    `bind` несколько минут отвечал бы «занято» — ровно тогда, когда
    `wait_resources_free` обязан увидеть свободу.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host or "127.0.0.1", int(port)))
        return True
    except ConnectionRefusedError:
        return False
    except (TimeoutError, socket.timeout):
        # Никто не отозвался: на localhost так ведёт себя только тот, кто
        # есть, но не отвечает. Считаем занятым — второй сервер туда не лезет.
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe_health(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """GET /health. Возвращает (готов, сообщение).

    Три исхода, и их важно различать:
      * 200 + status=ok   → готов;
      * 503 «Loading model» → процесс жив, модель ещё грузится, ждём;
      * нет ответа         → процесса нет либо он ещё не поднял сокет.
    """
    url = "http://%s:%d/health" % (host, port)
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return False, "нет ответа: %s" % exc.__class__.__name__
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200:
        status = str(data.get("status", "")).lower()
        if status in ("", "ok"):
            return True, "готов"
        return False, "статус: %s" % status
    if resp.status_code == 503:
        # Тело у 503 бывает разным: {"error":{"message":"Loading model"}}
        msg = ""
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", ""))
        elif isinstance(err, str):
            msg = err
        msg = msg or str(data.get("status", "")) or "загружается"
        return False, "загружается: %s" % msg[:120]
    return False, "HTTP %d" % resp.status_code


def probe_model(host: str, port: int, timeout: float = 3.0) -> str:
    """Имя модели по GET /v1/models — для записи в результат."""
    try:
        resp = requests.get("http://%s:%d/v1/models" % (host, port), timeout=timeout)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return str(ids[0]) if ids else ""
    except (requests.RequestException, ValueError, AttributeError):
        return ""


# ---------------------------------------------------------------------------


class ServerManager:
    """Один процесс llama-server на сессию тестирования."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_state=None,
        on_log=None,
    ):
        self.cfg = cfg
        self.status = ServerStatus(host=cfg.server_host, port=int(cfg.server_port))
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._log_handle = None
        self._log_written = 0
        self._log_path: Path | None = None
        self._lock = threading.Lock()
        self._stop_reader = threading.Event()
        self._on_state = on_state
        self._on_log = on_log

    # ------------------------------------------------------------------
    # состояние и лог

    def _set_state(self, state: ServerState, error: str = "") -> None:
        if self.status.state != state or error:
            self.status.state = state
            if error:
                self.status.error = error
            if self._on_state:
                try:
                    self._on_state(self.status)
                except Exception:  # noqa: BLE001 — колбэк не должен ронять менеджер
                    pass

    def _emit_log(self, line: str) -> None:
        if self._on_log:
            try:
                self._on_log(line)
            except Exception:  # noqa: BLE001
                pass

    def _open_log(self) -> Path:
        """Открыть файл лога, при необходимости провернув ротацию."""
        logs_dir = self.cfg.logs_path
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "server.log"
        if path.is_file() and path.stat().st_size >= LOG_ROTATE_BYTES:
            self._rotate(path)
        self._log_handle = path.open("a", encoding="utf-8", errors="replace")
        self._log_written = path.stat().st_size if path.is_file() else 0
        self._log_path = path
        self.status.log_path = str(path)
        return path

    @staticmethod
    def _rotate(path: Path) -> Path | None:
        """Старый лог — рядом с меткой времени (п. 3.4.5)."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = path.with_name("server_%s.log" % stamp)
        n = 1
        while target.exists():
            target = path.with_name("server_%s_%d.log" % (stamp, n))
            n += 1
        try:
            path.replace(target)
            return target
        except OSError:
            return None

    def _append_log(self, line: str) -> None:
        """Записать строку в файл; ротация — по ходу прогона, а не только при старте."""
        if self._log_handle is None:
            return
        try:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()
            self._log_written += len(line.encode("utf-8", errors="replace")) + 1
        except OSError:
            return
        if self._log_written >= LOG_ROTATE_BYTES:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
            self._rotate(self._log_path)
            self._log_handle = self._log_path.open("a", encoding="utf-8", errors="replace")
            self._log_written = 0
            self._emit_log("[лог провернут: файл достиг 10 МБ]")

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                if self._stop_reader.is_set():
                    break
                line = raw.rstrip("\r\n")
                self._append_log(line)
                self._emit_log(line)
        except (OSError, ValueError):
            pass

    def read_log_tail(self, lines: int = 200) -> list[str]:
        """Последние строки лога — для вкладки «Лог сервера»."""
        path = self._log_path or (self.cfg.logs_path / "server.log")
        if not path.is_file():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return [ln.rstrip("\n") for ln in fh.readlines()[-lines:]]
        except OSError:
            return []

    # ------------------------------------------------------------------
    # запуск

    def start(
        self,
        command: str | list[str],
        *,
        timeout: int | None = None,
        force: bool = False,
    ) -> ServerStatus:
        """Поднять процесс и дождаться готовности.

        `command` — строка (как её видит пользователь в поле) или список
        аргументов. Строка разбирается с учётом кавычек: пользователь мог
        отредактировать её руками (п. 3.4.2).
        """
        timeout = int(timeout or self.cfg.health_check_timeout_sec)
        host = self.cfg.server_host
        port = int(self.cfg.server_port)

        if self._proc is not None and self._proc.poll() is None:
            self.stop()

        if self.cfg.use_existing_server:
            # Свой процесс не поднимаем — только проверяем чужой (п. 3.4.4).
            self.status.command = str(command)
            self._set_state(ServerState.STARTING)
            started = time.perf_counter()
            ok = self.wait_ready(timeout)
            self.status.startup_time_ms = int((time.perf_counter() - started) * 1000)
            if ok:
                self._set_state(ServerState.READY)
            else:
                self._set_state(ServerState.ERROR, "внешний сервер не ответил на /health")
            return self.status

        if is_port_busy(host, port) and not force:
            self._set_state(
                ServerState.ERROR,
                "порт %d занят — освободите его или выберите другой" % port,
            )
            return self.status

        argv = list(command) if isinstance(command, list) else parse_command(command)
        if not argv:
            self._set_state(ServerState.ERROR, "пустая команда запуска")
            return self.status

        self.status.command = " ".join(argv) if isinstance(command, list) else str(command)
        self._set_state(ServerState.STARTING)
        self._open_log()
        self._emit_log("[%s] запуск: %s" % (_now(), self.status.command))

        creationflags = 0
        if os.name == "nt":
            creationflags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(Path(argv[0]).parent),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._set_state(ServerState.ERROR, "не удалось запустить процесс: %s" % exc)
            return self.status

        self.status.pid = self._proc.pid
        self.cfg.server_pid = int(self._proc.pid)
        self._save_cfg()

        self._stop_reader.clear()
        self._reader = threading.Thread(
            target=self._reader_loop, name="llama-server-log", daemon=True
        )
        self._reader.start()

        started = time.perf_counter()
        ok = self.wait_ready(timeout, proc=self._proc)
        self.status.startup_time_ms = int((time.perf_counter() - started) * 1000)

        if ok:
            self.status.model_load_time_ms = self.status.startup_time_ms
            self.status.health_message = "готов"
            self._set_state(ServerState.READY)
        else:
            code = self._proc.poll()
            if code is not None:
                reason = "процесс завершился с кодом %s до готовности" % code
                self.stop()
                self._set_state(ServerState.ERROR, reason)
            else:
                reason = "health-check не дождался готовности за %d сек" % timeout
                # п. 4.6: не отвечает на health — убить процесс. Порядок важен:
                # stop() выставляет состояние stopped, поэтому причину ставим
                # после него, иначе ошибка затрётся.
                self.stop()
                self._set_state(ServerState.ERROR, reason)
        return self.status

    def start_for_model(
        self,
        model,
        *,
        overrides: dict | None = None,
        extra_flags: list[str] | None = None,
        timeout: int | None = None,
    ) -> ServerStatus:
        """Собрать команду под модель и запустить (шаги 3–5 алгоритма п. 4.2)."""
        exe = self.cfg.llama_server_path or str((app_root() / EXE_NAME))
        params = resolve_params(
            model,
            self.cfg,
            overrides={
                "host": self.cfg.server_host,
                "port": self.cfg.server_port,
                **(overrides or {}),
            },
            extra_flags=extra_flags,
        )
        argv = build_command(exe, params, as_list=True)
        return self.start(argv, timeout=timeout)

    def wait_ready(
        self,
        timeout: int | None = None,
        interval: float = HEALTH_INTERVAL_SEC,
        proc: subprocess.Popen | None = None,
    ) -> bool:
        """Опрос /health каждые 500 мс (п. 3.4.3). True — сервер готов."""
        timeout = int(timeout or self.cfg.health_check_timeout_sec)
        host, port = self.cfg.server_host, int(self.cfg.server_port)
        deadline = time.perf_counter() + timeout
        last = ""
        while time.perf_counter() < deadline:
            ok, msg = probe_health(host, port)
            if ok:
                return True
            last = msg
            self.status.health_message = msg
            p = proc if proc is not None else self._proc
            if p is not None and p.poll() is not None:
                # Процесс умер, ждать больше нечего.
                self.status.health_message = "процесс завершился"
                return False
            time.sleep(interval)
        self.status.health_message = last or "таймаут"
        return False

    # ------------------------------------------------------------------
    # остановка

    def stop(self, grace: float = STOP_GRACE_SEC) -> ServerStatus:
        """terminate → 5 сек → kill (п. 3.4.3). Возвращает время остановки.

        Порядок важен и вот почему. «Вежливая» остановка на Windows — это
        `CTRL_BREAK_EVENT` в свою группу процессов. Но мы запускаем сервер
        без окна консоли (`CREATE_NO_WINDOW`), а без консоли CTRL_BREAK
        доставить некуда: сигнал уходит в никуда, и ожидание истекает
        впустую. Поэтому ждём на него коротко, а дальше идём по ТЗ —
        terminate, затем kill.
        """
        started = time.perf_counter()
        proc = self._proc
        method = ""
        if proc is not None and proc.poll() is None:
            self._emit_log("[%s] остановка процесса pid=%d" % (_now(), proc.pid))
            if os.name == "nt":
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                    proc.wait(timeout=1.5)
                    method = "CTRL_BREAK"
                except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    proc.terminate()
                    proc.wait(timeout=grace)
                    method = "terminate"
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=grace)
                    method = "terminate"
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                    method = "kill"
                except (OSError, subprocess.TimeoutExpired):
                    method = "не удалось"
            self._emit_log("[%s] процесс остановлен (%s)" % (_now(), method or "уже был мёртв"))

        self._stop_reader.set()
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        self._proc = None

        self.status.shutdown_time_ms = int((time.perf_counter() - started) * 1000)
        self.status.pid = 0
        self.status.health_message = ""
        self.cfg.server_pid = 0
        self._save_cfg()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
        self._set_state(ServerState.STOPPED)
        return self.status

    def wait_resources_free(self, timeout: float = PORT_FREE_TIMEOUT) -> bool:
        """Дождаться освобождения порта (шаг 2 алгоритма п. 4.2).

        Проверка VRAM требует nvidia-smi, который в песочнице не работает,
        поэтому здесь честно ждём только порт и повторяем проверку до
        `RESOURCE_RETRIES` раз, как просит п. 4.6.
        """
        host, port = self.cfg.server_host, int(self.cfg.server_port)
        deadline = time.perf_counter() + timeout
        for attempt in range(RESOURCE_RETRIES):
            while time.perf_counter() < deadline:
                if not is_port_busy(host, port):
                    return True
                time.sleep(0.25)
            self._emit_log(
                "[%s] порт %d всё ещё занят, проверка %d из %d"
                % (_now(), port, attempt + 1, RESOURCE_RETRIES)
            )
            time.sleep(5)  # п. 4.6: пауза 5 сек + повторная проверка
        return not is_port_busy(host, port)

    def cleanup(self) -> None:
        """Конец сессии: остановить свой процесс (п. 4.2, шаг 7)."""
        if self.cfg.use_existing_server:
            return
        if self._proc is not None:
            self.stop()

    # ------------------------------------------------------------------
    # осиротевшие процессы

    def kill_orphans(self) -> list[int]:
        """Убить свой осиротевший llama-server.exe по PID из config.json.

        Только свой: PID записывается в конфиг при запуске и обнуляется при
        остановке. Чужой сервер (лаунчер Инка) не трогаем никогда.
        """
        pid = int(getattr(self.cfg, "server_pid", 0) or 0)
        if not pid:
            return []
        if pid == os.getpid():
            self.cfg.server_pid = 0
            self._save_cfg()
            return []
        alive = _pid_alive(pid)
        if not alive:
            self.cfg.server_pid = 0
            self._save_cfg()
            return []
        killed = _kill_pid(pid)
        self._emit_log(
            "[%s] осиротевший процесс pid=%d %s"
            % (_now(), pid, "остановлен" if killed else "не удалось остановить")
        )
        self.cfg.server_pid = 0
        self._save_cfg()
        return [pid] if killed else []

    def _save_cfg(self) -> None:
        try:
            self.cfg.save()
        except OSError:
            pass

    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def health(self) -> tuple[bool, str]:
        return probe_health(self.cfg.server_host, int(self.cfg.server_port))

    def model_name(self) -> str:
        return probe_model(self.cfg.server_host, int(self.cfg.server_port))


# ---------------------------------------------------------------------------
# вспомогательное


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _win_pid_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _kill_pid(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def process_name(pid: int) -> str:
    """Имя процесса по PID — чтобы не убить одноимённый чужой процесс."""
    if os.name != "nt":
        return ""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if not text or "INFO:" in text:
            return ""
        first = text.splitlines()[0]
        parts = [p.strip('"') for p in first.split('","')]
        return parts[0] if parts else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
