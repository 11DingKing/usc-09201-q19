"""应用服务：把 HTTP/脚本请求编排为鉴权 + 账本命令 + 脱敏输出。"""

from __future__ import annotations

import threading
from typing import Any

from .access import (
    Actor,
    AccessDenied,
    authorize,
    redact_party,
    redact_statement,
)
from .ledger import Ledger
from .models import DisputeDirection, PartyType, Role
from .store import EventStore

# 需要在审计事件列表中对非办公室角色脱敏的字段路径
_SECRET_KEYS = {"id_number"}


class AppService:
    """单实例应用服务，线程安全（粗粒度锁，演示规模足够）。"""

    def __init__(self, store: EventStore) -> None:
        self.ledger = Ledger(store)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 主数据
    # ------------------------------------------------------------------

    def register_method(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "register_method")
        with self._lock:
            eid = self.ledger.register_method(
                body["method_id"],
                int(body["version"]),
                body["name"],
                body["parameters_hash"],
                body.get("note", ""),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def register_party(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "register_party")
        with self._lock:
            eid = self.ledger.register_party(
                body["party_id"],
                PartyType(body["party_type"]),
                body["name"],
                body.get("contact", ""),
                body.get("id_number", ""),
                body.get("account_ref", ""),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def list_parties(self, actor: Actor) -> dict[str, Any]:
        authorize(actor, "list_parties")
        parties = []
        for party in sorted(self.ledger.parties.values(), key=lambda p: p.party_id):
            parties.append(
                redact_party(
                    {
                        "party_id": party.party_id,
                        "party_type": party.party_type.value,
                        "name": party.name,
                        "contact": party.contact,
                        "id_number": party.id_number,
                        "account_ref": party.account_ref,
                    },
                    actor,
                )
            )
        return {"parties": parties}

    def register_plot(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "register_plot")
        with self._lock:
            eid = self.ledger.register_plot(
                body["plot_id"], body["name"], idem_key=body.get("idem_key")
            )
        return {"event_id": eid}

    def set_boundary(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "set_boundary")
        with self._lock:
            eid = self.ledger.set_boundary(
                body["plot_id"],
                bool(body["included"]),
                str(body["area_ha"]),
                body["effective_from"],
                body.get("note", ""),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def set_share(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "set_share")
        with self._lock:
            eid = self.ledger.set_share(
                body["plot_id"],
                body["party_id"],
                int(body["share_bps"]),
                body["effective_from"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def open_period(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "open_period")
        with self._lock:
            eid = self.ledger.open_period(
                body["period_id"],
                body["name"],
                body["start_date"],
                body["end_date"],
                int(body["price_cents_per_t"]),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def register_receipt(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "register_receipt")
        with self._lock:
            eid = self.ledger.register_receipt(
                body["receipt_id"],
                body["verifier"],
                body["issued_at"],
                body["content_hash"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    # ------------------------------------------------------------------
    # 监测与结算
    # ------------------------------------------------------------------

    def record_evidence(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "record_evidence")
        with self._lock:
            eid = self.ledger.record_evidence(
                body["evidence_id"],
                body["period_id"],
                body["plot_id"],
                body["method_id"],
                body.get("method_version"),
                int(body["tco2_kg"]),
                body["source_uri"],
                body["submitted_by"],
                version=body.get("version"),
                receipt_id=body.get("receipt_id"),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def list_evidence(self, actor: Actor, period_id: str | None = None) -> dict[str, Any]:
        authorize(actor, "list_evidence")
        result = []
        for ev in self.ledger.evidence:
            if period_id and ev.period_id != period_id:
                continue
            item = {
                "evidence_id": ev.evidence_id,
                "period_id": ev.period_id,
                "plot_id": ev.plot_id,
                "version": ev.version,
                "corrects_version": ev.corrects_version,
                "method_id": ev.method_id,
                "method_version": ev.method_version,
                "tco2_kg": ev.tco2_kg,
                "source_uri": ev.source_uri,
                "submitted_by": ev.submitted_by,
                "receipt_id": ev.receipt_id,
                "recorded_at": ev.recorded_at,
            }
            if actor.role == Role.MONITOR:
                pass  # 监测机构可见完整证据链
            result.append(item)
        receipts = {
            r.receipt_id: {
                "receipt_id": r.receipt_id,
                "verifier": r.verifier,
                "issued_at": r.issued_at,
                "consumed_by": r.consumed_by,
            }
            for r in self.ledger.receipts.values()
        }
        return {"evidence": result, "receipts": receipts}

    def confirm_allocation(
        self, actor: Actor, period_id: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        authorize(actor, "confirm_allocation")
        body = body or {}
        with self._lock:
            self.ledger.confirm_allocation(
                period_id, as_of=body.get("as_of"), idem_key=body.get("idem_key")
            )
            return self.ledger.period_summary(period_id)

    # ------------------------------------------------------------------
    # 支付流水
    # ------------------------------------------------------------------

    def withhold(self, actor: Actor, period_id: str, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "withhold")
        with self._lock:
            eid = self.ledger.withhold(
                period_id,
                body["party_id"],
                int(body["amount_cents"]),
                body["reason"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def pay(self, actor: Actor, period_id: str, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "pay")
        with self._lock:
            eid = self.ledger.pay(
                period_id,
                body["party_id"],
                int(body["amount_cents"]),
                body["reference"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def release_withheld(
        self, actor: Actor, period_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = Role.ADMIN
        actor.require({allowed})
        with self._lock:
            eid = self.ledger.release_withheld(
                period_id,
                body["party_id"],
                int(body["amount_cents"]),
                body["reference"],
                reclaim=bool(body.get("reclaim", False)),
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def claim_back(self, actor: Actor, period_id: str, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "claim_back")
        with self._lock:
            eid = self.ledger.claim_back(
                period_id,
                body["party_id"],
                int(body["amount_cents"]),
                body["reference"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def open_dispute(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "withhold")
        with self._lock:
            eid = self.ledger.open_dispute(
                body["dispute_id"],
                body["period_id"],
                body["party_id"],
                int(body["amount_cents"]),
                DisputeDirection(body["direction"]),
                body["reason"],
                idem_key=body.get("idem_key"),
            )
        return {"event_id": eid}

    def resolve_dispute(self, actor: Actor, dispute_id: str, body: dict[str, Any]) -> dict[str, Any]:
        authorize(actor, "resolve_dispute")
        with self._lock:
            eid = self.ledger.resolve_dispute(
                dispute_id, body["resolution"], idem_key=body.get("idem_key")
            )
        return {"event_id": eid}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def period_summary(self, actor: Actor, period_id: str) -> dict[str, Any]:
        authorize(actor, "period_summary")
        with self._lock:
            summary = self.ledger.period_summary(period_id)
        if actor.role == Role.AUDITOR:
            summary = dict(summary)
            # 审计不需要逐方敏感依据，汇总数字即可
        return summary

    def allocation_versions(self, actor: Actor, period_id: str) -> dict[str, Any]:
        authorize(actor, "allocation_versions")
        with self._lock:
            return {
                "period_id": period_id,
                "versions": self.ledger.allocation_versions(period_id),
            }

    def statement(
        self, actor: Actor, period_id: str, party_id: str
    ) -> dict[str, Any]:
        """参与方下载本方对账依据；办公室/审计可查任意方。"""

        if actor.role in (Role.ADMIN, Role.AUDITOR):
            pass
        elif actor.role in (Role.FARMER, Role.OPERATOR):
            if actor.party_id != party_id:
                raise AccessDenied("参与方只能下载本方对账依据")
        else:
            raise AccessDenied("当前角色无权下载对账依据")
        with self._lock:
            result = self.ledger.statement(period_id, party_id)
        return redact_statement(result, actor)

    def verify_anchor(self, actor: Actor, allocation_hash: str) -> dict[str, Any]:
        """公众存证核验：仅凭分配哈希确认其存在于链上，不含任何敏感信息。"""

        authorize(actor, "verify_anchor")
        for period_id, versions in self.ledger.allocations.items():
            for alloc in versions:
                if alloc.allocation_hash == allocation_hash:
                    return {
                        "found": True,
                        "period_id": period_id,
                        "version": alloc.version,
                        "allocation_hash": alloc.allocation_hash,
                        "tco2_kg": alloc.tco2_kg,
                        "distributable_cents": alloc.distributable_cents,
                        "state": alloc.state.value,
                        "chain_head_hash": self.ledger.store.head_hash(),
                    }
        return {"found": False, "allocation_hash": allocation_hash}

    def audit_events(self, actor: Actor) -> dict[str, Any]:
        """审计事件链：办公室可见原文，审计角色隐去证件号。"""

        authorize(actor, "period_summary")
        events = []
        for event in self.ledger.store.all():
            payload = event.payload
            if actor.role != Role.ADMIN:
                payload = _redact_payload(payload)
            events.append(
                {
                    "seq": event.seq,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "event_time": event.event_time,
                    "payload": payload,
                    "prev_hash": event.prev_hash,
                    "event_hash": event.event_hash,
                }
            )
        return {
            "chain_head_hash": self.ledger.store.head_hash(),
            "problems": self.ledger.store.verify_chain(),
            "events": events,
        }


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in payload.items():
        if key in _SECRET_KEYS and isinstance(value, str) and value:
            result[key] = value[:2] + "***"
        else:
            result[key] = value
    return result


def actor_from_headers(role_header: str | None, party_header: str | None) -> Actor:
    """从请求头构造请求方。

    注意：演示用请求头模拟身份，生产环境必须替换为带签名的令牌认证。
    """

    role = Role(role_header) if role_header else Role.PUBLIC
    return Actor(role, party_header)
