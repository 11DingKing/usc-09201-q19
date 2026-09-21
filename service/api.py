"""共享账 HTTP 接口。

演示级身份通过请求头传递（``X-Role`` / ``X-Participant-Id``），
生产部署应替换为网关鉴权。所有写接口都是“提交事件”，服务端不提供
任何修改/删除历史的入口——更正只能通过新事件完成。
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from .ledger.engine import LedgerEngine, LedgerError
from .ledger.store import AuthorizationError, EventStore, event_to_dict
from .ledger.visibility import (
    INSTITUTIONAL_ROLES,
    can_view_period_bundle,
    redact_event,
    view_participant_pack,
    view_period_bundle,
)


class JsonResponse(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error", "error"))
        self.status = status
        self.body = body


class LedgerApp:
    """无状态路由对象，引擎与存储在进程内共享。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()
        self.engine = LedgerEngine(self.store)

    # -------------------------------------------------------------- 调度

    def handle(self, method: str, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict[str, Any]]:
        # 同一存储可能被多个引擎实例写入（演示/脚本直连），读取前先增量折叠。
        self.engine.sync()
        parsed = urlparse(path)
        route, query = parsed.path, parse_qs(parsed.query)
        role = headers.get("x-role", "office")
        pid = headers.get("x-participant-id")
        try:
            if method == "GET" and route == "/health":
                return 200, {"status": "ok"}
            if method == "GET" and route == "/ledger/root":
                return 200, {"root_hash": self.store.root_hash, "event_count": len(self.store.events())}
            if method == "POST" and route == "/events":
                try:
                    parsed_body = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    raise JsonResponse(HTTPStatus.BAD_REQUEST, {"error": "bad_json", "detail": str(exc)}) from exc
                return self._post_event(parsed_body, actor_role=role)
            if method == "GET" and route == "/events":
                return self._list_events(query, role)
            if route.startswith("/periods/") and route.endswith("/bundle"):
                return self._period_bundle(route.split("/")[2], role)
            if route.startswith("/periods/") and route.endswith("/flows"):
                return self._period_flows(route.split("/")[2], role)
            if route.startswith("/participants/") and route.endswith("/evidence-pack"):
                return self._evidence_pack(route.split("/")[2], role, pid)
            if route.startswith("/accounts/"):
                return self._account(route.split("/")[2], role, pid)
            raise JsonResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except JsonResponse as resp:
            return resp.status, resp.body

    # -------------------------------------------------------------- 写入

    def _post_event(self, body: dict[str, Any], *, actor_role: str) -> tuple[int, dict[str, Any]]:
        for key in ("event_id", "type", "payload"):
            if not body.get(key):
                raise JsonResponse(HTTPStatus.BAD_REQUEST, {"error": f"缺少字段: {key}"})
        # 显式 actor 可被服务端信任的前提是网关注入；演示中以头角色覆盖，防伪造。
        try:
            event, created = self.engine.append(
                body["type"],
                body["event_id"],
                body["payload"],
                occurred_at=body.get("occurred_at"),
                actor=actor_role,
            )
        except AuthorizationError as exc:
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": str(exc)}) from exc
        except LedgerError as exc:
            raise JsonResponse(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "validation_failed", "detail": str(exc)}) from exc
        return (HTTPStatus.CREATED if created else HTTPStatus.OK), {
            "created": created,
            "event": event_to_dict(event),
            "ledger_root_hash": self.store.root_hash,
        }

    # -------------------------------------------------------------- 读取

    def _list_events(self, query: dict[str, list[str]], role: str) -> tuple[int, dict[str, Any]]:
        if role not in INSTITUTIONAL_ROLES:
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": "机构角色才可浏览事件流"})
        after = int(query.get("after_seq", ["0"])[0])
        events = [redact_event(event_to_dict(e), role) for e in self.store.events(after_seq=after)]
        return 200, {"root_hash": self.store.root_hash, "events": events}

    def _period_bundle(self, period_id: str, role: str) -> tuple[int, dict[str, Any]]:
        if not can_view_period_bundle(role):
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        try:
            bundle = self.engine.period_bundle(period_id)
        except LedgerError as exc:
            raise JsonResponse(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": str(exc)}) from exc
        return 200, view_period_bundle(bundle, role)

    def _period_flows(self, period_id: str, role: str) -> tuple[int, dict[str, Any]]:
        if not can_view_period_bundle(role):
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        if period_id not in self.engine.confirmations:
            raise JsonResponse(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": "该期尚未确认"})
        return 200, {
            "ledger_root_hash": self.store.root_hash,
            "period_id": period_id,
            "flows": self.engine.period_flows(period_id),
        }

    def _evidence_pack(self, period_target: str, role: str, pid_header: str | None) -> tuple[int, dict[str, Any]]:
        # 路径形如 /participants/{id}/evidence-pack，period_target 即参与方 id。
        if role != "participant" or pid_header != period_target:
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": "参与方仅可下载本人依据包"})
        if period_target not in self.engine.participants:
            raise JsonResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return 200, view_participant_pack(
            self.engine.participant_evidence_pack(period_target), requester_role=role
        )

    def _account(self, pid: str, role: str, pid_header: str | None) -> tuple[int, dict[str, Any]]:
        if role == "participant":
            if pid_header != pid:
                raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": "参与方仅可查本人账户"})
        elif role not in INSTITUTIONAL_ROLES:
            raise JsonResponse(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        if pid not in self.engine.participants:
            raise JsonResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        account = self.engine.participant_account(pid)
        return 200, {"ledger_root_hash": self.store.root_hash, "account": account}
