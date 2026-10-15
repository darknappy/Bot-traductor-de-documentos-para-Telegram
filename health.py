"""Servidor HTTP mínimo para plataformas como Render.

Render solo mantiene vivo un *Web Service* si el proceso escucha en el puerto
que indica la variable ``PORT`` (y usa ``/health`` para el chequeo de estado).
Este servidor no sirve contenido: solo responde salud y estado del bot.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

logger = logging.getLogger(__name__)

StatusProvider = Callable[[], dict | None]


def start_health_server(port: int, status_provider: StatusProvider | None = None) -> ThreadingHTTPServer:
    """Arranca el servidor de salud en un hilo y devuelve el objeto servidor."""
    started_at = time.time()

    class Handler(BaseHTTPRequestHandler):
        server_version = "TraductorBot/1.0"
        protocol_version = "HTTP/1.1"

        def _payload(self) -> tuple[int, dict]:
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path in ("/", "/health", "/healthz", "/status"):
                body: dict = {
                    "ok": True,
                    "uptime_seconds": round(time.time() - started_at, 1),
                }
                if status_provider is not None:
                    try:
                        extra = status_provider()
                        if extra:
                            body.update(extra)
                    except Exception:  # noqa: BLE001 - el estado nunca debe tumbar la salud
                        logger.debug("No se pudo obtener el estado del bot", exc_info=True)
                return 200, body
            return 404, {"ok": False, "error": "not found"}

        def do_GET(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
            self._respond()

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(body=False)

        def _respond(self, body: bool = True) -> None:
            status, payload = self._payload()
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.debug("health: " + fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Servidor de salud escuchando en el puerto %d (/, /health, /status)", port)
    return server
