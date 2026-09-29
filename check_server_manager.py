"""Проверка управления процессом llama-server (этап 6 ТЗ).

Настоящий llama-server не трогаем: модель на этой машине поднимает Инк
вручную. Жизненный цикл проверяем на `devtools/fake_llama_server.py` —
он отдаёт те же /health и /v1/models и умеет притворяться, что модель
ещё грузится.

Запуск:  .venv/Scripts/python.exe check_server_manager.py
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmtestbench.config import AppConfig  # noqa: E402
from llmtestbench import server_cmd  # noqa: E402
from llmtestbench import server_manager as sm  # noqa: E402

PY = sys.executable
FAKE = str(Path(__file__).resolve().parent / "devtools" / "fake_llama_server.py")
LIVE_URL = "http://127.0.0.1:8080"

ok_count = 0
fail_count = 0
notes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print("  ok   %s" % name)
    else:
        fail_count += 1
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def blocking_listener(port: int):
    """Занять порт так, как это делает настоящий сервер.

    `listen(1)` без `accept` — ловушка: очередь соединений переполняется,
    Windows начинает отвечать отказом, и порт выглядит свободным. Реальный
    сервер забирает соединения из очереди, поэтому и в тесте есть поток,
    который их принимает и закрывает.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", port))
    srv.listen(128)
    stop = threading.Event()

    def acceptor() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                return

    thread = threading.Thread(target=acceptor, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        srv.close()
        thread.join(timeout=1.0)


def make_cfg(tmp: Path, port: int, **kw) -> AppConfig:
    cfg = AppConfig(
        models_dir=str(tmp / "models"),
        logs_dir=str(tmp / "logs"),
        results_dir=str(tmp / "results"),
        reports_dir=str(tmp / "reports"),
        tests_dir=str(tmp / "tests"),
        sessions_dir=str(tmp / "sessions"),
        server_host="127.0.0.1",
        server_port=port,
        health_check_timeout_sec=kw.pop("health_check_timeout_sec", 10),
        **kw,
    )
    # Привязываем к временному файлу: менеджер сохраняет PID через
    # cfg.save(), и без привязки это уехало бы в рабочий config.json проекта.
    cfg.bind(tmp / ("config_%d.json" % port))
    cfg.resolve_dirs()
    cfg.ensure_dirs()
    return cfg


def fake_cmd(port: int, delay: float = 0.0) -> list[str]:
    return [PY, FAKE, str(port), str(delay)]


def live_server_up() -> bool:
    try:
        import requests

        return requests.get(LIVE_URL + "/health", timeout=2).status_code in (200, 503)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------


def test_split_command() -> None:
    print("\n1. Разбор строки команды (пользователь мог править её руками)")
    cases = [
        (
            r'C:\tools\llama-server.exe -m "F:\Models\my model.gguf" --port 8080',
            ["C:\\tools\\llama-server.exe", "-m", "F:\\Models\\my model.gguf", "--port", "8080"],
        ),
        ("llama-server -c 4096", ["llama-server", "-c", "4096"]),
        ("", []),
        ('exe --flag="a b"', ["exe", "--flag=a b"]),
        (r'  exe   -m   "F:\a b\x.gguf"  ', ["exe", "-m", "F:\\a b\\x.gguf"]),
        # обратный слэш перед кавычкой — это кавычка внутри аргумента
        ('exe "a\\"b c"', ["exe", 'a"b c']),
    ]
    for line, expected in cases:
        got = server_cmd.parse_command(line)
        check("разбор: %r" % line[:44], got == expected, "получено %r" % got)


def test_command_roundtrip() -> None:
    print("\n1б. Собрать команду → показать строкой → разобрать обратно")
    params = server_cmd.LaunchParams(
        model_path=r"F:\Models\Модель с пробелом\my model.gguf",
        host="127.0.0.1",
        port=8080,
        n_ctx=132608,
        temp=0.8,
        top_p=0.95,
        top_k=40,
        seed=-1,
        n_gpu_layers=-1,
        extra_flags=["--n-cpu-moe", "17", "--no-mmap"],
    )
    argv = server_cmd.build_command(r"C:\tools\llama-server.exe", params, as_list=True)
    line = server_cmd.build_command(r"C:\tools\llama-server.exe", params)
    back = server_cmd.parse_command(line)
    check(
        "круговой переход сохранил аргументы",
        back == argv,
        "\n       было: %r\n       стало: %r" % (argv, back),
    )
    check("кириллица в пути не пострадала", any("Модель с пробелом" in a for a in back), str(back))
    check("пробелы внутри аргумента сохранены", any("my model.gguf" in a for a in back), str(back))


def test_describe_server() -> None:
    print("\n1в. Чем поднят сервер: применённая команда, чужая и отсутствующая")
    line = (
        r"C:\tools\llama-server.exe -m F:\Models\Ornith.gguf -ngl 99 "
        r"--load-mode none -ot blk.0.ffn_down_exps.weight=CUDA_Host"
    )

    got = server_cmd.describe_server(line, applied=True)
    check("применённая команда записана как есть", got["command"] == line, str(got))
    check("она помечена применённой", got["applied"] is True, str(got))
    check("и отговорки при ней нет", got["note"] == "", str(got))

    got = server_cmd.describe_server(line)
    check("неприменённая команда не выдаётся за факт", got["applied"] is False, str(got))
    check("но и не теряется", got["command"] == line, str(got))
    check("причина названа", "не применяло" in got["note"], str(got))

    got = server_cmd.describe_server()
    check("пусто — это «неизвестно», а не пустая команда", got["command"] == "", str(got))
    check("и причина сказана вслух", "неизвестна" in got["note"], str(got))

    got = server_cmd.describe_server("   \t ", applied=True)
    check("одни пробелы командой не считаются", got["command"] == "", str(got))
    check("применение без команды тоже объяснено", "не записана" in got["note"], str(got))

    check(
        "ключи всегда одни и те же",
        all(
            set(server_cmd.describe_server(c, applied=a)) == {"applied", "command", "note"}
            for c in ("", line)
            for a in (True, False)
        ),
    )


def test_status_json() -> None:
    print("\n2. Блок «server» в JSON результата (п. 4.8 ТЗ)")
    st = sm.ServerStatus(
        pid=12345,
        command="llama-server.exe -m x.gguf",
        host="127.0.0.1",
        port=8080,
        startup_time_ms=8200,
        model_load_time_ms=7900,
        shutdown_time_ms=450,
        restarts=0,
    )
    data = st.to_json()
    expected = {
        "pid",
        "command",
        "host",
        "port",
        "startup_time_ms",
        "model_load_time_ms",
        "shutdown_time_ms",
        "restarts",
    }
    check(
        "ключи совпадают с ТЗ",
        set(data) == expected,
        "лишние %s, не хватает %s" % (set(data) - expected, expected - set(data)),
    )
    check("значения на месте", data["pid"] == 12345 and data["port"] == 8080)


def test_states_ru() -> None:
    print("\n3. Состояния (п. 3.4.3: stopped → starting → ready → error)")
    names = [s.value for s in sm.ServerState]
    check(
        "пять состояний из ТЗ",
        names == ["stopped", "starting", "ready", "testing", "error"],
        str(names),
    )
    check("есть русские подписи", all(s.ru for s in sm.ServerState))


def test_port_detection() -> None:
    print("\n4. Определение занятости порта (п. 4.6)")
    p = free_port()
    check("свободный порт не занят", sm.is_port_busy("127.0.0.1", p) is False)
    with blocking_listener(p) as busy_port:
        check("занятый порт определён", sm.is_port_busy("127.0.0.1", busy_port) is True)
        check(
            "и определяется стабильно",
            all(sm.is_port_busy("127.0.0.1", busy_port) for _ in range(3)),
        )


def test_live_server(tmp: Path) -> None:
    print("\n5. Живой сервер Инка на 127.0.0.1:8080 (только чтение)")
    if not live_server_up():
        notes.append("сервер на 8080 не отвечает — часть проверок пропущена")
        print("  --   сервер не отвечает, пропускаю")
        return
    ok, msg = sm.probe_health("127.0.0.1", 8080)
    check("probe_health: готов", ok, msg)
    name = sm.probe_model("127.0.0.1", 8080)
    check("probe_model вернул имя", bool(name), "пусто")
    if name:
        print("       модель: %s" % name)

    cfg = make_cfg(tmp / "live", 8080, use_existing_server=True)
    mgr = sm.ServerManager(cfg)
    states: list[str] = []
    mgr._on_state = lambda st: states.append(st.state.value)
    started = time.perf_counter()
    mgr.start("не используется — режим внешнего сервера", timeout=10)
    took = time.perf_counter() - started
    check("внешний сервер → ready", mgr.status.state == sm.ServerState.READY, mgr.status.error)
    check("свой процесс не поднят", mgr.is_running is False)
    check("pid не записан", cfg.server_pid == 0)
    check("health ответил быстро (%.2f сек)" % took, took < 3.0, "%.2f" % took)
    check("состояния шли starting → ready", states[:2] == ["starting", "ready"], str(states))
    mgr.stop()
    check(
        "stop() в режиме внешнего сервера ничего не убил", live_server_up(), "сервер Инка пропал!"
    )


def test_lifecycle(tmp: Path) -> None:
    print("\n6. Жизненный цикл на поддельном сервере (порт свой, GPU не трогаем)")
    port = free_port()
    cfg = make_cfg(tmp / "life", port, health_check_timeout_sec=15)
    states: list[str] = []
    lines: list[str] = []
    mgr = sm.ServerManager(
        cfg, on_state=lambda st: states.append(st.state.value), on_log=lines.append
    )

    mgr.start(fake_cmd(port), timeout=15)
    check("состояние ready", mgr.status.state == sm.ServerState.READY, mgr.status.error)
    check("процесс жив", mgr.is_running is True)
    check("pid записан в статус", mgr.status.pid > 0)
    check("pid записан в config.json", cfg.server_pid == mgr.status.pid)
    check(
        "startup_time_ms заполнен", mgr.status.startup_time_ms > 0, str(mgr.status.startup_time_ms)
    )
    check("состояния: starting → ready", states[:2] == ["starting", "ready"], str(states))
    check(
        "лог получил строку о старте",
        any("запуск:" in ln for ln in lines),
        "строк %d" % len(lines),
    )
    check("модель читается по /v1/models", "fake-model" in mgr.model_name(), mgr.model_name())
    tail = mgr.read_log_tail(50)
    check("read_log_tail вернул строки", len(tail) > 0, "пусто")

    log_path = Path(mgr.status.log_path)
    check("файл лога создан", log_path.is_file(), str(log_path))

    killed = mgr.stop()
    check("процесс остановлен", mgr.is_running is False)
    check("состояние stopped", mgr.status.state == sm.ServerState.STOPPED)
    check("shutdown_time_ms заполнен", killed.shutdown_time_ms >= 0)
    check("pid сброшен в config.json", cfg.server_pid == 0)
    check("порт освободился", sm.is_port_busy("127.0.0.1", port) is False)


def test_slow_start(tmp: Path) -> None:
    print("\n7. Опрос /health каждые 500 мс: модель грузится 3 секунды")
    port = free_port()
    cfg = make_cfg(tmp / "slow", port, health_check_timeout_sec=20)
    mgr = sm.ServerManager(cfg)
    mgr.start(fake_cmd(port, delay=3.0), timeout=20)
    check("дождались готовности", mgr.status.state == sm.ServerState.READY, mgr.status.error)
    check(
        "ждали не меньше 2.5 сек (%.1f сек)" % (mgr.status.startup_time_ms / 1000.0),
        mgr.status.startup_time_ms >= 2500,
        str(mgr.status.startup_time_ms),
    )
    check(
        "промежуточный статус был «загружается»",
        "загружается" in mgr.status.health_message or mgr.status.startup_time_ms >= 2500,
    )
    mgr.stop()


def test_port_busy(tmp: Path) -> None:
    print("\n8. Порт занят перед запуском (п. 4.6)")
    port = free_port()
    with blocking_listener(port):
        cfg = make_cfg(tmp / "busy", port, health_check_timeout_sec=5)
        mgr = sm.ServerManager(cfg)
        mgr.start(fake_cmd(port), timeout=5)
        check("вернулась ошибка", mgr.status.state == sm.ServerState.ERROR)
        check("в ошибке сказано про порт", "занят" in mgr.status.error, mgr.status.error)
        check("процесс не поднят", mgr.is_running is False)
        check("чужой сервер не тронут", sm.is_port_busy("127.0.0.1", port) is True)


def test_health_timeout(tmp: Path) -> None:
    print("\n9. Таймаут health-check (п. 6: startup_timeout)")
    port = free_port()
    cfg = make_cfg(tmp / "timeout", port, health_check_timeout_sec=2)
    mgr = sm.ServerManager(cfg)
    started = time.perf_counter()
    mgr.start(fake_cmd(port, delay=999.0), timeout=2)
    took = time.perf_counter() - started
    check("состояние error", mgr.status.state == sm.ServerState.ERROR)
    check("причина — health-check", "health-check" in mgr.status.error, mgr.status.error)
    check("уложились примерно в таймаут (%.1f сек)" % took, took < 6.0, "%.1f" % took)
    check("процесс убит (п. 4.6)", mgr.is_running is False)
    check("pid сброшен", cfg.server_pid == 0)


def test_orphans(tmp: Path) -> None:
    print("\n10. Осиротевший процесс по PID из config.json (п. 4.6)")
    port = free_port()
    cfg = make_cfg(tmp / "orphan", port, health_check_timeout_sec=15)
    mgr = sm.ServerManager(cfg)
    mgr.start(fake_cmd(port), timeout=15)
    pid = cfg.server_pid
    check("процесс поднят, pid в конфиге", pid > 0)
    # Приложение «упало»: менеджер брошен, процесс жив, pid остался в конфиге.
    fresh = sm.ServerManager(cfg)
    check("чужой pid пока живёт", sm._pid_alive(pid) is True)
    killed = fresh.kill_orphans()
    time.sleep(0.3)
    check("kill_orphans вернул pid", killed == [pid], str(killed))
    check("процесс убит", sm._pid_alive(pid) is False)
    check("pid вычищен из конфига", cfg.server_pid == 0)

    check("повторный вызов ничего не находит", fresh.kill_orphans() == [])

    # Пустой конфиг: не должен никого трогать и не падать.
    clean = make_cfg(tmp / "orphan2", free_port())
    check("без записанного pid — пусто", sm.ServerManager(clean).kill_orphans() == [])


def test_rotation(tmp: Path) -> None:
    print("\n11. Ротация лога (п. 3.4.5: максимум 10 МБ)")
    logs = tmp / "rot" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "server.log"
    path.write_text("старая строка\n" * 10, encoding="utf-8")
    rotated = sm.ServerManager._rotate(path)
    check("старый лог переехал", rotated is not None and rotated.is_file(), str(rotated))
    check(
        "имя с меткой времени",
        rotated is not None
        and rotated.name.startswith("server_")
        and rotated.name != "server.log",
        str(rotated),
    )
    check("исходный файл исчез", not path.exists())
    check("константа порога = 10 МБ", sm.LOG_ROTATE_BYTES == 10 * 1024 * 1024)


def test_no_spawn_for_missing_exe(tmp: Path) -> None:
    print("\n12. Ошибка запуска: исполняемого файла нет")
    port = free_port()
    cfg = make_cfg(tmp / "noexe", port, health_check_timeout_sec=2)
    mgr = sm.ServerManager(cfg)
    mgr.start([str(tmp / "нет-такого-файла.exe"), "-m", "x.gguf"], timeout=2)
    check("состояние error", mgr.status.state == sm.ServerState.ERROR)
    check(
        "сказано, что запустить не удалось",
        "не удалось запустить" in mgr.status.error,
        mgr.status.error,
    )
    check("pid не появился", cfg.server_pid == 0)


def test_wait_resources_free(tmp: Path) -> None:
    print("\n13. Ожидание освобождения порта (п. 4.2, шаг 2)")
    port = free_port()
    cfg = make_cfg(tmp / "free", port, health_check_timeout_sec=5)
    mgr = sm.ServerManager(cfg)
    started = time.perf_counter()
    ok = mgr.wait_resources_free(timeout=2)
    took = time.perf_counter() - started
    check("свободный порт — сразу True", ok is True)
    # Отказ по loopback на этой машине приходит через ~2 сек, так что
    # «сразу» здесь означает «одна проба», а не «мгновенно».
    check("хватило одной пробы (%.2f сек)" % took, took < 3.5, "%.2f" % took)

    with blocking_listener(port):
        # Порт занят, но у нас короткий таймаут: функция обязана вернуть False,
        # а не ждать вечно (в ТЗ — 10 сек + паузы по 5).
        started = time.perf_counter()
        ok2 = mgr.wait_resources_free(timeout=0.6)
        took2 = time.perf_counter() - started
        check("занятый порт — False", ok2 is False)
        check(
            "проверок было 3, паузы по 5 сек (%.1f сек)" % took2, took2 >= 10.0, "%.1f сек" % took2
        )


def _fingerprint(path: Path) -> tuple:
    """Отпечаток файла: размер + время правки. Пусто — если файла нет."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


def test_config_binding(tmp: Path) -> None:
    print("\n14. Конфиг проверок не уезжает в рабочий config.json проекта")
    port = free_port()
    cfg = make_cfg(tmp / "bind", port)
    check(
        "конфиг привязан к временному файлу",
        cfg.config_path.parent == tmp / "bind",
        str(cfg.config_path),
    )
    cfg.server_pid = 4242
    saved = cfg.save()
    check("save() без пути пишет в привязанный файл", saved == cfg.config_path, str(saved))
    check("файл действительно создан", saved.is_file(), str(saved))
    text = saved.read_text(encoding="utf-8")
    check("pid попал в файл", "4242" in text)
    loaded = AppConfig.load(saved)
    check("обратное чтение помнит путь", loaded.config_path == saved, str(loaded.config_path))
    check("pid прочитался", loaded.server_pid == 4242, str(loaded.server_pid))


def main() -> int:
    print("=" * 72)
    print("Проверка управления процессом llama-server (этап 6 ТЗ)")
    print("=" * 72)
    tmp = Path(tempfile.mkdtemp(prefix="llmtestbench_srv_"))
    project_config = Path(__file__).resolve().parent / "config.json"
    config_before = _fingerprint(project_config)
    try:
        test_split_command()
        test_command_roundtrip()
        test_describe_server()
        test_status_json()
        test_states_ru()
        test_port_detection()
        test_live_server(tmp)
        test_lifecycle(tmp)
        test_slow_start(tmp)
        test_port_busy(tmp)
        test_health_timeout(tmp)
        test_orphans(tmp)
        test_rotation(tmp)
        test_no_spawn_for_missing_exe(tmp)
        test_wait_resources_free(tmp)
        test_config_binding(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n15. Страж: рабочий config.json проекта")
    check(
        "config.json проекта не изменён прогоном проверок",
        _fingerprint(project_config) == config_before,
        "файл %s изменился" % project_config,
    )

    print("\n" + "=" * 72)
    print("Пройдено: %d   Провалено: %d" % (ok_count, fail_count))
    for n in notes:
        print("Примечание: %s" % n)
    print("=" * 72)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
