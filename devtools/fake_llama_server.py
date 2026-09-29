"""Поддельный llama-server для проверки управления процессом.

Настоящий llama-server занимает видеопамять и трогать его нельзя — модель
на этой машине поднимает Инк вручную. Поэтому жизненный цикл (запуск,
health-check, ротация лога, остановка) проверяется на этом скрипте: он
отдаёт те же эндпоинты /health и /v1/models и умеет притворяться, что
модель ещё грузится.

Запуск:  python devtools/fake_llama_server.py <порт> [задержка_готовности_сек]
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
READY_AFTER = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
START = time.monotonic()
MODEL = "fake-model-for-tests-Q4_K_M.gguf"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — глушим стандартный лог
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        elapsed = time.monotonic() - START
        if self.path == "/health":
            if elapsed < READY_AFTER:
                self._send(503, {"error": {"message": "Loading model"}})
            else:
                self._send(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        elif self.path == "/props":
            self._send(200, {"default_generation_settings": {"n_ctx": 4096}})
        else:
            self._send(404, {"error": "not found"})


def main() -> int:
    print(
        "fake llama-server: старт, порт %d, готовность через %.1f сек" % (PORT, READY_AFTER),
        flush=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    tick = 0
    try:
        while True:
            server.handle_request()
            tick += 1
            if tick % 5 == 0:
                print("fake llama-server: обработано запросов %d" % tick, flush=True)
    except KeyboardInterrupt:
        print("fake llama-server: остановлен по сигналу", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
