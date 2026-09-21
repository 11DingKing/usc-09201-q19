"""森林碳益共享账 HTTP 服务。

身份通过请求头模拟（生产环境须替换为签名令牌）：

* ``X-Role``: admin / auditor / monitor / operator / forest_farmer / public
* ``X-Party-Id``: 参与方角色绑定的本方参与方编号

敏感交易信息按角色可见；所有响应都带链头哈希 ``chain_head_hash``，
参与方对账依据可凭 ``allocation_hash`` 交叉核验。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .access import AccessDenied
from .app import AppService, actor_from_headers
from .ledger import LedgerError
from .store import EventStore, IdempotencyError

_STORE_PATH = os.environ.get("LEDGER_PATH", "ledger-events.jsonl")


def create_app_service(path: str | None = None) -> AppService:
    """创建应用服务；path=None 时使用内存存储（测试用）。"""

    return AppService(EventStore(path))


class Handler(BaseHTTPRequestHandler):
    """处理共享账 API 请求。"""

    app_service: AppService | None = None

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        actor = actor_from_headers(
            self.headers.get("X-Role"), self.headers.get("X-Party-Id")
        )

        try:
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            assert self.app_service is not None
            if path == "/api/parties":
                self._send(200, self.app_service.list_parties(actor))
            elif path == "/api/evidence":
                period = query.get("period", [None])[0]
                self._send(200, self.app_service.list_evidence(actor, period))
            elif path == "/api/events":
                self._send(200, self.app_service.audit_events(actor))
            elif path.startswith("/api/anchor/"):
                anchor = path.rsplit("/", 1)[-1]
                self._send(200, self.app_service.verify_anchor(actor, anchor))
            else:
                match = _match(
                    path,
                    [
                        ("/api/periods/{}/summary", "period_summary"),
                        ("/api/periods/{}/versions", "period_versions"),
                        (
                            "/api/periods/{}/statements/{}",
                            "statement",
                        ),
                    ],
                )
                if match is None:
                    self._send(404, {"error": "not_found"})
                    return
                kind, args = match
                if kind == "period_summary":
                    self._send(200, self.app_service.period_summary(actor, args[0]))
                elif kind == "period_versions":
                    self._send(200, self.app_service.allocation_versions(actor, args[0]))
                else:
                    self._send(
                        200, self.app_service.statement(actor, args[0], args[1])
                    )
        except AccessDenied as exc:
            self._send(403, {"error": "forbidden", "detail": str(exc)})
        except LedgerError as exc:
            self._send(409, {"error": "ledger_rule_violation", "detail": str(exc)})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802, C901
        parsed = urlparse(self.path)
        path = parsed.path
        actor = actor_from_headers(
            self.headers.get("X-Role"), self.headers.get("X-Party-Id")
        )
        body = self._read_body()
        if body is None:
            return
        assert self.app_service is not None
        app = self.app_service

        try:
            if path == "/api/methods":
                self._send(201, app.register_method(actor, body))
            elif path == "/api/parties":
                self._send(201, app.register_party(actor, body))
            elif path == "/api/plots":
                self._send(201, app.register_plot(actor, body))
            elif path == "/api/boundaries":
                self._send(201, app.set_boundary(actor, body))
            elif path == "/api/shares":
                self._send(201, app.set_share(actor, body))
            elif path == "/api/periods":
                self._send(201, app.open_period(actor, body))
            elif path == "/api/receipts":
                self._send(201, app.register_receipt(actor, body))
            elif path == "/api/evidence":
                self._send(201, app.record_evidence(actor, body))
            elif path == "/api/disputes":
                self._send(201, app.open_dispute(actor, body))
            elif path.startswith("/api/disputes/") and path.endswith("/resolve"):
                dispute_id = path.split("/")[3]
                self._send(201, app.resolve_dispute(actor, dispute_id, body))
            elif path.startswith("/api/periods/"):
                parts = path.strip("/").split("/")
                # /api/periods/{pid}/<action>
                if len(parts) != 4:
                    self._send(404, {"error": "not_found"})
                    return
                _, _, period_id, action = parts
                if action == "confirm":
                    self._send(201, app.confirm_allocation(actor, period_id, body))
                elif action == "withhold":
                    self._send(201, app.withhold(actor, period_id, body))
                elif action == "pay":
                    self._send(201, app.pay(actor, period_id, body))
                elif action == "release-withheld":
                    self._send(201, app.release_withheld(actor, period_id, body))
                elif action == "claim-back":
                    self._send(201, app.claim_back(actor, period_id, body))
                else:
                    self._send(404, {"error": "not_found"})
            else:
                self._send(404, {"error": "not_found"})
        except AccessDenied as exc:
            self._send(403, {"error": "forbidden", "detail": str(exc)})
        except (LedgerError, IdempotencyError) as exc:
            self._send(409, {"error": "ledger_rule_violation", "detail": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": "missing_field", "detail": str(exc)})
        except ValueError as exc:
            self._send(400, {"error": "bad_request", "detail": str(exc)})

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid_json"})
            return None
        if not isinstance(data, dict):
            self._send(400, {"error": "body_must_be_object"})
            return None
        return data

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def _match(path: str, templates: list[tuple[str, str]]) -> tuple[str, list[str]] | None:
    """极简路由匹配：模板里的 {} 捕获一段路径。"""

    for template, kind in templates:
        t_parts = template.strip("/").split("/")
        p_parts = path.strip("/").split("/")
        if len(t_parts) != len(p_parts):
            continue
        args: list[str] = []
        ok = True
        for t, p in zip(t_parts, p_parts):
            if t == "{}":
                args.append(p)
            elif t != p:
                ok = False
                break
        if ok:
            return kind, args
    return None


def create_server(host: str = "0.0.0.0", port: int = 0, *, path: str | None = "default"):
    """创建服务实例。

    path="default" 使用 LEDGER_PATH 环境变量指定的 JSONL（默认持久化）；
    path=None 使用纯内存存储（测试）。
    """

    store_path = _STORE_PATH if path == "default" else path
    service = create_app_service(store_path)

    class _BoundHandler(Handler):
        app_service = service

    return ThreadingHTTPServer((host, port), _BoundHandler), service


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server, _ = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}（事件账本：{_STORE_PATH}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
