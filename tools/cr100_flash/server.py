"""Serve immutable flash inputs only to the verified controller IP."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


class PayloadServer(AbstractContextManager):
    def __init__(self, *, bind_ip: str, allow_ip: str, port: int,
                 log_path: Path, payloads: dict[str, bytes], duration: int = 3600):
        if not 1 <= duration <= 3600:
            raise ValueError("duration must be 1..3600 seconds")
        if not all(path.startswith("/") and "?" not in path for path in payloads):
            raise ValueError("payload paths must be absolute paths without queries")
        self.payloads = dict(payloads)
        self.allow_ip = allow_ip
        self.lock = threading.Lock()
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_file = self.log_path.open("x", encoding="utf-8")
        self.log_path.chmod(0o600)
        self._timer = None
        self._thread = None
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._deadline = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _serve(self, body: bool):
                source = self.client_address[0]
                if source != outer.allow_ip:
                    self.send_error(403)
                    outer.log("denied", source=source, path=self.path)
                    return
                with outer.lock:
                    content = outer.payloads.get(self.path)
                if content is None:
                    self.send_error(404)
                    outer.log("missing", source=source, path=self.path)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(content)
                    except (BrokenPipeError, ConnectionResetError):
                        outer.log("interrupted", source=source, path=self.path)
                        return
                outer.log("served", source=source, path=self.path,
                          bytes=len(content), sha256=sha256(content).hexdigest())

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

        self.httpd = ThreadingHTTPServer((bind_ip, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.duration = duration
        self.bind_ip = bind_ip

    def log(self, event: str, **fields):
        with self.lock:
            self.log_file.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                            "event": event, **fields}) + "\n")
            self.log_file.flush()

    def add(self, path: str, content: bytes):
        if not path.startswith("/") or "?" in path:
            raise ValueError("invalid payload path")
        with self.lock:
            if path in self.payloads:
                raise ValueError("payload path is immutable")
            self.payloads[path] = bytes(content)

    def __enter__(self):
        self._deadline = time.monotonic() + self.duration
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self._timer = threading.Timer(self.duration, self.stop)
        self._timer.daemon = True
        self._timer.start()
        self.log("ready", bind=self.bind_ip, port=self.port, allow=self.allow_ip,
                 expires_in=self.duration)
        return self

    def remaining_seconds(self) -> float:
        with self._stop_lock:
            if self._stopped or self._deadline is None or not self._thread.is_alive():
                return 0.0
            return max(0.0, self._deadline - time.monotonic())

    def stop(self):
        # Safe to call from the expiry timer or the main thread.
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            if self._thread and self._thread.is_alive():
                self.httpd.shutdown()
                self._thread.join(timeout=3)
            self.httpd.server_close()
            self.log("stopped")
            self.log_file.close()

    def __exit__(self, *_args):
        if self._timer:
            self._timer.cancel()
        self.stop()
