"""HTTP 服务入口：健康检查与共享账接口。"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import LedgerApp

_LEDGER_PATH = os.environ.get("LEDGER_FILE")


def build_app() -> LedgerApp:
    """创建共享账应用；设置 ``LEDGER_FILE`` 时事件持久化到 JSON 行文件。"""

    from .ledger.store import EventStore

    return LedgerApp(EventStore(_LEDGER_PATH))


APP = build_app()


class Handler(BaseHTTPRequestHandler):
    """把请求转交给 :class:`LedgerApp`。"""

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        app = getattr(self.server, "app", APP)
        status, body = app.handle(method, self.path, headers, raw)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str = "0.0.0.0", port: int = 0, app: LedgerApp | None = None) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app or APP  # type: ignore[attr-defined]
    return server


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    if _LEDGER_PATH:
        print(f"事件持久化文件：{_LEDGER_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
