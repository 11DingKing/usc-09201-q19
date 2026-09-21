"""共享账领域引擎。

职责
----
1. 写入校验：每期结算 *确认* 时锁定方法版本、基线版本、监测证据、
   可分配量、边界版本与共有林地份额规则；校验份额金额、核证回执等。
2. 只追加投影：所有余额由事件折叠得到。跨期监测更正、边界变化、
   份额变化、重复回执、负调整、争议冻结都不修改历史流水，只产生
   新事件（更正后由办公室发起 ``trueup.raised`` 追补/追回）。
3. 派生账户：剩余应付、暂缓金额、追补责任（超付应返还）、冻结状态。

金额单位内部采用人民币分（整数），事件载荷中保留两位小数字符串，
避免浮点误差。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .events import EVENT_AUTHORIZED_ROLES, Event
from .store import AuthorizationError, EventStore, event_to_dict

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class LedgerError(ValueError):
    """业务规则校验失败。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_cents(value: Any, *, field: str) -> int:
    """把数字/字符串金额规整为分；超过两位小数直接拒绝。"""

    try:
        dec = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal 自身异常类型很窄
        raise LedgerError(f"{field} 不是合法金额: {value!r}") from exc
    if dec != dec.quantize(CENT, rounding=ROUND_HALF_UP):
        raise LedgerError(f"{field} 超过分位精度: {value!r}")
    return int(dec * 100)


def cents_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def share_to_decimal(value: Any, *, field: str) -> Decimal:
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # pragma: no cover
        raise LedgerError(f"{field} 不是合法比例: {value!r}") from exc
    if dec < 0 or dec > 1:
        raise LedgerError(f"{field} 比例越界: {value!r}")
    return dec


# ---------------------------------------------------------------------------
# 投影状态
# ---------------------------------------------------------------------------


@dataclass
class Hold:
    hold_id: str
    period_id: str
    participant_id: str
    amount: int
    active: bool
    event_seq: int


@dataclass
class TrueUp:
    trueup_id: str
    period_id: str
    participant_id: str
    delta: int  # 有符号：正=补付，负=追回
    payable_offset: int  # 被剩余应付吸收的部分（有符号）
    clawback: int  # 已支付超出新 entitlement、应返还的部分（>=0）
    reason: str
    event_seq: int
    occurred_at: str
    event_hash: str


class LedgerEngine:
    """在 :class:`EventStore` 之上做校验与只读投影。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.methods: dict[str, dict[str, Any]] = {}
        self.baselines: dict[str, dict[str, Any]] = {}
        self.boundaries: dict[str, dict[str, Any]] = {}
        self.participants: dict[str, dict[str, Any]] = {}
        self.periods: dict[str, dict[str, Any]] = {}
        # period_id -> 按时间顺序的份额规则版本
        self.share_rules: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # period_id -> [submitted, corrected, ...]
        self.monitoring: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.receipts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # period_id -> 确认事件投影
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.holds: dict[str, Hold] = {}
        self.hold_releases: list[dict[str, Any]] = []
        self.trueups: list[TrueUp] = []
        self.payments: list[dict[str, Any]] = []
        self.adjustments: list[dict[str, Any]] = []
        self.disputes: dict[str, dict[str, Any]] = {}
        self._applied_seq = 0
        self._rebuild()

    # ================================================================== 写入

    def append(
        self,
        type_: str,
        event_id: str,
        payload: dict[str, Any],
        *,
        occurred_at: str | None = None,
        actor: str,
    ) -> tuple[Event, bool]:
        """校验并追加事件；校验失败不落任何账。返回 ``(事件, 是否新建)``。"""

        payload = dict(payload)
        # 角色鉴权先于业务校验：无权角色不应通过返回信息探测业务状态。
        allowed = EVENT_AUTHORIZED_ROLES.get(type_)
        if allowed is None:
            raise LedgerError(f"未知事件类型: {type_}")
        if actor not in allowed:
            raise AuthorizationError(f"角色 {actor} 无权提交 {type_}")
        self._validate(type_, event_id, payload)
        event, created = self.store.append(
            event_id=event_id,
            type_=type_,
            payload=payload,
            occurred_at=occurred_at or now_iso(),
            actor=actor,
        )
        if created:
            self._apply(event)
            self._applied_seq = event.seq
        return event, created

    def sync(self) -> int:
        """折叠存储中尚未投影的新事件，返回新增条数。

        多个引擎实例共享同一存储（如 HTTP 层与测试夹具直接写入）时，
        读取前调用本方法即可得到最新余额，无需整表重建。
        """

        applied = 0
        for event in self.store.events():
            if event.seq <= self._applied_seq:
                continue
            self._apply(event)
            self._applied_seq = event.seq
            applied += 1
        return applied

    # ---------------------------------------------------------- 校验分发表

    def _validate(self, type_: str, event_id: str, p: dict[str, Any]) -> None:
        if not event_id:
            raise LedgerError("event_id 不能为空")
        existing = self.store.get(event_id)
        if existing is not None:
            # 幂等重放只接受同类型同载荷；编号撞车必须显式失败。
            if existing.type != type_ or existing.payload != p:
                raise LedgerError(f"event_id 已用于不同事件: {event_id}")
            return
        handler = getattr(self, f"_validate_{type_.replace('.', '_')}", None)
        if handler is None:  # pragma: no cover - 事件类型注册即有 handler
            raise LedgerError(f"未实现校验: {type_}")
        handler(p)

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise LedgerError(message)

    # ---------------------------------------------------------- 基础档案

    def _validate_method_registered(self, p: dict[str, Any]) -> None:
        version = p.get("version")
        self._require(version and p.get("doc_hash"), "method 缺少 version/doc_hash")
        self._require(version not in self.methods, f"方法版本已存在: {version}")

    def _validate_baseline_registered(self, p: dict[str, Any]) -> None:
        version = p.get("version")
        self._require(version and p.get("doc_hash"), "baseline 缺少 version/doc_hash")
        self._require(version not in self.baselines, f"基线版本已存在: {version}")

    def _validate_boundary_changed(self, p: dict[str, Any]) -> None:
        version = p.get("version")
        self._require(version and p.get("area_mu") is not None, "boundary 缺少 version/area_mu")
        self._require(version not in self.boundaries, f"边界版本已存在: {version}")

    def _validate_participant_registered(self, p: dict[str, Any]) -> None:
        pid = p.get("participant_id")
        kind = p.get("kind")
        self._require(pid and kind, "participant 缺少 participant_id/kind")
        self._require(
            kind in {"forest_farmer", "operator", "village_collective"},
            f"未知参与方类型: {kind}",
        )
        self._require(pid not in self.participants, f"参与方已存在: {pid}")

    def _validate_share_rule_locked(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        version = p.get("version")
        lines = p.get("lines")
        self._require(period_id in self.periods, f"结算期不存在: {period_id}")
        self._require(period_id not in self.confirmations, "该期已确认，份额规则不可再加版本")
        self._require(version and isinstance(lines, list) and lines, "份额规则缺少 version/lines")
        existing_versions = {r["version"] for r in self.share_rules[period_id]}
        self._require(version not in existing_versions, f"份额规则版本已存在: {version}")
        total = ZERO
        seen: set[str] = set()
        for line in lines:
            pid = line.get("participant_id")
            self._require(pid in self.participants, f"份额规则含未注册参与方: {pid}")
            self._require(pid not in seen, f"份额规则中参与方重复: {pid}")
            seen.add(pid)
            total += share_to_decimal(line.get("share"), field=f"share:{pid}")
        self._require(total == Decimal("1"), f"份额之和必须为 1，实际 {total}")

    def _validate_period_opened(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        self._require(period_id, "period 缺少 period_id")
        self._require(period_id not in self.periods, f"结算期已存在: {period_id}")
        boundary = p.get("boundary_version")
        self._require(boundary in self.boundaries, f"初始边界版本未登记: {boundary}")

    # ---------------------------------------------------------- 监测与核证

    def _latest_monitoring(self, period_id: str) -> dict[str, Any] | None:
        rows = self.monitoring.get(period_id)
        return rows[-1] if rows else None

    def _validate_monitoring_submitted(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        self._require(period_id in self.periods, f"结算期不存在: {period_id}")
        self._require(period_id not in self.confirmations, "该期已确认，不能再提交监测")
        self._require(not self.monitoring[period_id], "该期已有监测结果，应走更正事件")
        self._validate_monitoring_payload(p)

    def _validate_monitoring_corrected(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        self._require(period_id in self.periods, f"结算期不存在: {period_id}")
        self._require(self.monitoring[period_id], "该期尚无监测结果，不能更正")
        self._require(p.get("reason"), "监测更正必须说明原因")
        self._validate_monitoring_payload(p)

    def _validate_monitoring_payload(self, p: dict[str, Any]) -> None:
        amount = p.get("allocatable_amount")
        self._require(amount is not None, "监测结果缺少 allocatable_amount")
        to_cents(amount, field="allocatable_amount")
        self._require(p.get("method_version") in self.methods, "监测引用了未登记方法版本")
        self._require(p.get("baseline_version") in self.baselines, "监测引用了未登记基线版本")
        self._require(p.get("boundary_version") in self.boundaries, "监测引用了未登记边界版本")
        evidence = p.get("evidence")
        self._require(isinstance(evidence, list) and evidence, "监测证据至少一条")
        for item in evidence:
            self._require(
                item.get("ref") and item.get("sha256"),
                "监测证据需包含 ref 与 sha256",
            )

    def _validate_verification_receipt_recorded(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        self._require(period_id in self.periods, f"结算期不存在: {period_id}")
        self._require(p.get("receipt_no") and p.get("doc_hash"), "核证回执缺少 receipt_no/doc_hash")
        dup = any(r["receipt_no"] == p["receipt_no"] for r in self.receipts[period_id])
        self._require(not dup, f"核证回执编号重复: {p['receipt_no']}")

    # ---------------------------------------------------------- 确认分配

    def _validate_distribution_confirmed(self, p: dict[str, Any]) -> None:
        period_id = p.get("period_id")
        period = self.periods.get(period_id)
        self._require(period is not None, f"结算期不存在: {period_id}")
        self._require(period_id not in self.confirmations, f"该期已确认: {period_id}")

        mon = self._latest_monitoring(period_id)
        self._require(mon is not None, "该期尚无监测结果，不能确认分配")
        self._require(
            p.get("monitoring_ref") == mon["event_id"],
            "确认必须锁定当期最新监测事件",
        )
        alloc = to_cents(mon["allocatable_amount"], field="allocatable_amount")
        self._require(
            p.get("method_version") == mon["method_version"]
            and p.get("baseline_version") == mon["baseline_version"]
            and p.get("boundary_version") == mon["boundary_version"],
            "确认锁定的方法/基线/边界版本必须与监测一致",
        )
        self._require(
            p.get("allocatable_amount") == mon["allocatable_amount"],
            "确认的可分配量必须与最新监测结果一致",
        )
        rules = self.share_rules.get(period_id)
        self._require(rules, "该期尚未锁定份额规则")
        rule = rules[-1]
        self._require(p.get("share_rule_version") == rule["version"], "确认必须锁定最新份额规则版本")

        if period.get("requires_receipt", True):
            self._require(self.receipts[period_id], "缺少核证回执，不能确认分配")

        lines = p.get("lines")
        self._require(isinstance(lines, list) and len(lines) == len(rule["lines"]), "分配行数与份额规则不符")
        expected = self._allocate(alloc, rule["lines"])
        actual: dict[str, int] = {}
        for line in lines:
            pid = line.get("participant_id")
            self._require(pid in expected, f"分配行含规则外参与方: {pid}")
            self._require(pid not in actual, f"分配行参与方重复: {pid}")
            rule_share = next(r["share"] for r in rule["lines"] if r["participant_id"] == pid)
            self._require(
                share_to_decimal(line.get("share"), field=f"share:{pid}") == share_to_decimal(rule_share, field="rule"),
                f"{pid} 分配行份额与锁定规则不符",
            )
            amount = to_cents(line.get("amount"), field=f"amount:{pid}")
            self._require(amount == expected[pid], f"{pid} 分配金额与份额不符")
            actual[pid] = amount
        self._require(sum(actual.values()) == alloc, "分配合计不等于可分配量")

    @staticmethod
    def _allocate(alloc_cents: int, share_lines: list[dict[str, Any]]) -> dict[str, int]:
        """按份额分配到分，尾差按最大小数余数补给，确保合计严格相等。"""

        exact = [
            (line["participant_id"], Decimal(alloc_cents) * Decimal(str(line["share"])))
            for line in share_lines
        ]
        floors = {pid: int(value // 1) for pid, value in exact}  # value 单位是“分”
        remainder = alloc_cents - sum(floors.values())
        order = sorted(exact, key=lambda kv: kv[1] - (kv[1] // 1), reverse=True)
        for i in range(remainder):
            floors[order[i % len(order)][0]] += 1
        return floors

    # ---------------------------------------------------------- 暂缓/冻结

    def _line_ctx(self, p: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        period_id, pid = p.get("period_id"), p.get("participant_id")
        conf = self.confirmations.get(period_id)
        self._require(conf is not None, f"该期尚未确认: {period_id}")
        self._require(pid in self.participants, f"参与方不存在: {pid}")
        return period_id, pid, conf

    def _validate_hold_placed(self, p: dict[str, Any]) -> None:
        period_id, pid, _ = self._line_ctx(p)
        self._require(p.get("hold_id"), "hold 缺少 hold_id")
        self._require(p["hold_id"] not in self.holds, f"暂缓单已存在: {p['hold_id']}")
        amount = to_cents(p.get("amount"), field="amount")
        self._require(amount > 0, "暂缓金额必须为正")
        self._require(
            amount <= self._payable_available(period_id, pid),
            "暂缓金额超过该参与方当期剩余可付",
        )

    def _validate_hold_released(self, p: dict[str, Any]) -> None:
        hold = self.holds.get(p.get("hold_id"))
        self._require(hold is not None, "暂缓单不存在")
        self._require(hold.active, "暂缓单已解除")
        amount = to_cents(p.get("amount", cents_str(hold.amount)), field="amount")
        self._require(0 < amount <= hold.amount, "解除金额应在 (0, 暂缓额] 区间")

    def _validate_dispute_frozen(self, p: dict[str, Any]) -> None:
        pid = p.get("participant_id")
        self._require(pid in self.participants, f"参与方不存在: {pid}")
        active = self.disputes.get(pid)
        self._require(not (active and active["active"]), f"参与方已在争议冻结中: {pid}")
        self._require(p.get("case_ref") and p.get("reason"), "争议冻结缺少 case_ref/reason")

    def _validate_dispute_resolved(self, p: dict[str, Any]) -> None:
        pid = p.get("participant_id")
        active = self.disputes.get(pid)
        self._require(active and active["active"], f"参与方未处于冻结: {pid}")
        self._require(p.get("case_ref") == active["case_ref"], "案件编号不一致")

    # ---------------------------------------------------------- 支付/调整

    def _validate_payment_made(self, p: dict[str, Any]) -> None:
        period_id, pid, _ = self._line_ctx(p)
        amount = to_cents(p.get("amount"), field="amount")
        self._require(amount > 0, "支付金额必须为正")
        direction = p.get("direction", "pay")
        self._require(direction in {"pay", "return"}, "direction 仅支持 pay/return")
        self._require(p.get("payment_id"), "payment 缺少 payment_id")
        self._require(
            not any(pay["payment_id"] == p["payment_id"] for pay in self.payments),
            f"支付单号重复: {p['payment_id']}",
        )
        self._require(not self._frozen(pid), f"参与方争议冻结中，停止资金动作: {pid}")
        if direction == "pay":
            self._require(
                amount <= self._payable_available(period_id, pid),
                "支付超过剩余应付（含暂缓扣减）",
            )
        else:
            self._require(
                amount <= self._overpaid(period_id, pid),
                "返还金额超过当前超付（追补责任）额度",
            )

    def _validate_negative_adjustment(self, p: dict[str, Any]) -> None:
        period_id, pid, _ = self._line_ctx(p)
        amount = to_cents(p.get("amount"), field="amount")
        self._require(amount < 0, "负调整金额必须为负")
        self._require(p.get("adjustment_id"), "adjustment 缺少 adjustment_id")
        self._require(
            not any(a["adjustment_id"] == p["adjustment_id"] for a in self.adjustments),
            f"负调整单号重复: {p['adjustment_id']}",
        )
        self._require(p.get("reason"), "负调整必须说明原因")

    def _validate_trueup_raised(self, p: dict[str, Any]) -> None:
        period_id, pid, _ = self._line_ctx(p)
        self._require(p.get("trueup_id"), "trueup 缺少 trueup_id")
        self._require(
            not any(t.trueup_id == p["trueup_id"] for t in self.trueups),
            f"追补单号重复: {p['trueup_id']}",
        )
        delta = to_cents(p.get("delta"), field="delta")
        self._require(delta != 0, "追补差额不能为 0")
        self._require(p.get("reason"), "追补必须说明触发原因（如监测更正）")
        # suggest 已扣除历史追补，故这里只校验方向与上限。
        suggested = self.suggest_trueups(period_id).get(pid, 0)
        self._require(
            (delta < 0 and suggested < 0 and -delta <= -suggested)
            or (delta > 0 and suggested > 0 and delta <= suggested),
            f"追补额与应补差额不符，建议差额: {cents_str(suggested)}",
        )

    # ================================================================== 投影

    def _rebuild(self) -> None:
        for event in self.store.events():
            self._apply(event)
            self._applied_seq = event.seq

    def _apply(self, e: Event) -> None:
        p = e.payload
        t = e.type
        if t == "method.registered":
            self.methods[p["version"]] = {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
        elif t == "baseline.registered":
            self.baselines[p["version"]] = {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
        elif t == "boundary.changed":
            self.boundaries[p["version"]] = {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
        elif t == "participant.registered":
            self.participants[p["participant_id"]] = {
                **p,
                "event_id": e.event_id,
                "event_hash": e.event_hash,
                "seq": e.seq,
            }
        elif t == "period.opened":
            self.periods[p["period_id"]] = {**p, "event_id": e.event_id, "seq": e.seq}
        elif t == "share.rule.locked":
            self.share_rules[p["period_id"]].append(
                {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
            )
        elif t in {"monitoring.submitted", "monitoring.corrected"}:
            self.monitoring[p["period_id"]].append(
                {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
            )
        elif t == "verification.receipt.recorded":
            self.receipts[p["period_id"]].append(
                {**p, "event_id": e.event_id, "event_hash": e.event_hash, "seq": e.seq}
            )
        elif t == "distribution.confirmed":
            self.confirmations[p["period_id"]] = self._confirmation_view(e)
        elif t == "hold.placed":
            self.holds[p["hold_id"]] = Hold(
                hold_id=p["hold_id"],
                period_id=p["period_id"],
                participant_id=p["participant_id"],
                amount=to_cents(p["amount"], field="amount"),
                active=True,
                event_seq=e.seq,
            )
        elif t == "hold.released":
            hold = self.holds[p["hold_id"]]
            released = to_cents(p.get("amount", cents_str(hold.amount)), field="amount")
            hold.amount -= released
            if hold.amount == 0:
                hold.active = False
            self.hold_releases.append(
                {
                    "hold_id": p["hold_id"],
                    "period_id": hold.period_id,
                    "participant_id": hold.participant_id,
                    "amount_cents": released,
                    "reason": p.get("reason"),
                    "event_seq": e.seq,
                    "event_hash": e.event_hash,
                    "occurred_at": e.occurred_at,
                }
            )
        elif t == "dispute.frozen":
            self.disputes[p["participant_id"]] = {
                **p,
                "active": True,
                "event_hash": e.event_hash,
                "seq": e.seq,
            }
        elif t == "dispute.resolved":
            if p["participant_id"] in self.disputes:
                self.disputes[p["participant_id"]]["active"] = False
        elif t == "payment.made":
            self.payments.append(
                {
                    **p,
                    "direction": p.get("direction", "pay"),
                    "event_id": e.event_id,
                    "event_hash": e.event_hash,
                    "seq": e.seq,
                    "occurred_at": e.occurred_at,
                    "amount_cents": to_cents(p["amount"], field="amount"),
                }
            )
        elif t == "negative.adjustment":
            self.adjustments.append(
                {
                    **p,
                    "event_id": e.event_id,
                    "event_hash": e.event_hash,
                    "seq": e.seq,
                    "occurred_at": e.occurred_at,
                    "amount_cents": to_cents(p["amount"], field="amount"),
                }
            )
        elif t == "trueup.raised":
            period_id, pid = p["period_id"], p["participant_id"]
            delta = to_cents(p["delta"], field="delta")
            before = self._net_position(period_id, pid)  # 未含本次追补；暂缓不影响追补责任判定
            if delta < 0:
                absorbed = min(-delta, max(before, 0))
                clawback = max(-delta - max(before, 0), 0)
                payable_offset = -absorbed
            else:
                absorbed, clawback, payable_offset = 0, 0, delta
            self.trueups.append(
                TrueUp(
                    trueup_id=p["trueup_id"],
                    period_id=period_id,
                    participant_id=pid,
                    delta=delta,
                    payable_offset=payable_offset,
                    clawback=clawback,
                    reason=p["reason"],
                    event_seq=e.seq,
                    occurred_at=e.occurred_at,
                    event_hash=e.event_hash,
                )
            )

    def _confirmation_view(self, e: Event) -> dict[str, Any]:
        p = e.payload
        return {
            "period_id": p["period_id"],
            "monitoring_ref": p["monitoring_ref"],
            "method_version": p["method_version"],
            "baseline_version": p["baseline_version"],
            "boundary_version": p["boundary_version"],
            "share_rule_version": p["share_rule_version"],
            "allocatable_cents": to_cents(p["allocatable_amount"], field="allocatable_amount"),
            "lines": [
                {
                    "participant_id": line["participant_id"],
                    "share": line["share"],
                    "gross_cents": to_cents(line["amount"], field="amount"),
                }
                for line in p["lines"]
            ],
            "event_id": e.event_id,
            "event_hash": e.event_hash,
            "seq": e.seq,
            "occurred_at": e.occurred_at,
        }

    # ================================================================== 账户

    def _frozen(self, pid: str) -> bool:
        d = self.disputes.get(pid)
        return bool(d and d["active"])

    def _gross(self, period_id: str, pid: str) -> int:
        conf = self.confirmations[period_id]
        for line in conf["lines"]:
            if line["participant_id"] == pid:
                return line["gross_cents"]
        return 0

    def _raised_delta(self, period_id: str, pid: str) -> int:
        return sum(t.delta for t in self.trueups if t.period_id == period_id and t.participant_id == pid)

    def _adjusted(self, period_id: str, pid: str) -> int:
        return sum(
            a["amount_cents"]
            for a in self.adjustments
            if a["period_id"] == period_id and a["participant_id"] == pid
        )

    def _paid_net(self, period_id: str, pid: str) -> int:
        out = 0
        for pay in self.payments:
            if pay["period_id"] != period_id or pay["participant_id"] != pid:
                continue
            out += pay["amount_cents"] if pay["direction"] == "pay" else -pay["amount_cents"]
        return out

    def _holds_active(self, period_id: str, pid: str) -> int:
        return sum(h.amount for h in self.holds.values() if h.active and h.period_id == period_id and h.participant_id == pid)

    def _net_position(self, period_id: str, pid: str) -> int:
        """权益（含追补/负调整）- 净支付；为负即实际超付。暂缓不在内（暂缓未出账）。"""

        entitlement = self._gross(period_id, pid) + self._raised_delta(period_id, pid) + self._adjusted(period_id, pid)
        return entitlement - self._paid_net(period_id, pid)

    def _payable_available(self, period_id: str, pid: str) -> int:
        """当前可支付额度：未付权益扣除生效暂缓。"""

        return max(self._net_position(period_id, pid) - self._holds_active(period_id, pid), 0)

    def _overpaid(self, period_id: str, pid: str) -> int:
        """追补责任：已实际支付（净）超出权益的部分。暂缓不算支付。"""

        return max(-self._net_position(period_id, pid), 0)

    # ---------------------------------------------------------- 追补建议

    def suggest_trueups(self, period_id: str) -> dict[str, int]:
        """监测更正/负调整后，每方尚待发起追补的有符号差额（分）。

        以 *确认时锁定的份额* 重算 entitlement 差异——份额变化只影响
        以后各期，不改历史；返回值为正表示应补付，为负表示应追回。
        """

        conf = self.confirmations.get(period_id)
        if conf is None:
            return {}
        mon = self._latest_monitoring(period_id)
        assert mon is not None
        latest_alloc = to_cents(mon["allocatable_amount"], field="allocatable_amount")
        share_lines = [
            {"participant_id": line["participant_id"], "share": line["share"]}
            for line in conf["lines"]
        ]
        new_allocation = self._allocate(latest_alloc, share_lines)
        result: dict[str, int] = {}
        for line in conf["lines"]:
            pid = line["participant_id"]
            # 与确认时相同的最大余数法整分分配，差额合计严格等于可分配量变化。
            remaining = new_allocation[pid] - line["gross_cents"] - self._raised_delta(period_id, pid)
            if remaining:
                result[pid] = remaining
        return result

    # ---------------------------------------------------------- 只读视图

    def line_account(self, period_id: str, pid: str) -> dict[str, Any]:
        conf = self.confirmations[period_id]
        gross = self._gross(period_id, pid)
        delta = self._raised_delta(period_id, pid)
        adjusted = self._adjusted(period_id, pid)
        holds = self._holds_active(period_id, pid)
        net = self._net_position(period_id, pid)
        share = next(line["share"] for line in conf["lines"] if line["participant_id"] == pid)
        return {
            "period_id": period_id,
            "participant_id": pid,
            "share": share,
            "gross": cents_str(gross),
            "trueup_delta": cents_str(delta),
            "negative_adjustments": cents_str(adjusted),
            "entitlement": cents_str(gross + delta + adjusted),
            "paid_net": cents_str(self._paid_net(period_id, pid)),
            "holds_active": cents_str(holds),
            "remaining_payable": cents_str(max(net - holds, 0)),
            "clawback_due": cents_str(max(-net, 0)),
            "frozen": self._frozen(pid),
            "confirmation_event_hash": conf["event_hash"],
        }

    def participant_account(self, pid: str) -> dict[str, Any]:
        lines = [
            self.line_account(period_id, pid)
            for period_id in self.confirmations
            if any(line["participant_id"] == pid for line in self.confirmations[period_id]["lines"])
        ]
        total = lambda key: sum(to_cents(line[key], field=key) for line in lines)  # noqa: E731
        return {
            "participant_id": pid,
            "frozen": self._frozen(pid),
            "lines": lines,
            "remaining_payable_total": cents_str(total("remaining_payable")),
            "holds_total": cents_str(total("holds_active")),
            "clawback_due_total": cents_str(total("clawback_due")),
        }

    def period_bundle(self, period_id: str) -> dict[str, Any]:
        """组装某期完整依据包（未做角色裁剪）。"""

        conf = self.confirmations.get(period_id)
        if conf is None:
            raise LedgerError(f"该期尚未确认: {period_id}")
        mon = self._latest_monitoring(period_id)
        rule = next(r for r in self.share_rules[period_id] if r["version"] == conf["share_rule_version"])
        period = self.periods[period_id]
        return {
            "ledger_root_hash": self.store.root_hash,
            "period": {
                "period_id": period_id,
                "title": period.get("title"),
                "starts_on": period.get("starts_on"),
                "ends_on": period.get("ends_on"),
            },
            "locked": {
                "method": self.methods[conf["method_version"]],
                "baseline": self.baselines[conf["baseline_version"]],
                "boundary": self.boundaries[conf["boundary_version"]],
                "share_rule": {
                    "version": rule["version"],
                    "lines": rule["lines"],
                    "event_hash": rule["event_hash"],
                },
                "monitoring": mon,
                "monitoring_history": self.monitoring[period_id],
                "receipts": self.receipts[period_id],
            },
            "allocatable_amount": cents_str(conf["allocatable_cents"]),
            "confirmation_event_hash": conf["event_hash"],
            "lines": [self.line_account(period_id, line["participant_id"]) for line in conf["lines"]],
            "flows": self.period_flows(period_id),
            "trueup_suggestions": {
                pid: cents_str(v) for pid, v in self.suggest_trueups(period_id).items()
            },
        }

    def period_flows(self, period_id: str) -> dict[str, list[dict[str, Any]]]:
        """四类流水：确认、暂缓、支付、追补（负调整单列附据）。"""

        conf = self.confirmations.get(period_id)
        confirmed: list[dict[str, Any]] = []
        if conf:
            confirmed.append(
                {
                    "kind": "confirmation",
                    "seq": conf["seq"],
                    "event_hash": conf["event_hash"],
                    "occurred_at": conf["occurred_at"],
                }
            )
        holds = [
            {
                "kind": "hold",
                "hold_id": h.hold_id,
                "participant_id": h.participant_id,
                "amount": cents_str(h.amount),
                "active": h.active,
                "seq": h.event_seq,
            }
            for h in sorted(
                (h for h in self.holds.values() if h.period_id == period_id),
                key=lambda h: h.event_seq,
            )
        ]
        holds += [
            {
                "kind": "hold_release",
                "hold_id": r["hold_id"],
                "participant_id": r["participant_id"],
                "released_amount": cents_str(r["amount_cents"]),
                "reason": r["reason"],
                "occurred_at": r["occurred_at"],
                "seq": r["event_seq"],
                "event_hash": r["event_hash"],
            }
            for r in self.hold_releases
            if r["period_id"] == period_id
        ]
        holds.sort(key=lambda row: row["seq"])
        payments = [
            {
                "kind": "payment",
                "payment_id": p["payment_id"],
                "participant_id": p["participant_id"],
                "direction": p["direction"],
                "amount": p["amount"],
                "bank_account": p.get("bank_account"),
                "payment_ref": p.get("payment_ref"),
                "occurred_at": p["occurred_at"],
                "seq": p["seq"],
                "event_hash": p["event_hash"],
            }
            for p in self.payments
            if p["period_id"] == period_id
        ]
        trueups = [
            {
                "kind": "trueup",
                "trueup_id": t.trueup_id,
                "participant_id": t.participant_id,
                "delta": cents_str(t.delta),
                "payable_offset": cents_str(t.payable_offset),
                "clawback": cents_str(t.clawback),
                "reason": t.reason,
                "occurred_at": t.occurred_at,
                "seq": t.event_seq,
                "event_hash": t.event_hash,
            }
            for t in self.trueups
            if t.period_id == period_id
        ]
        adjustments = [
            {
                "kind": "negative_adjustment",
                "adjustment_id": a["adjustment_id"],
                "participant_id": a["participant_id"],
                "amount": a["amount"],
                "reason": a["reason"],
                "occurred_at": a["occurred_at"],
                "seq": a["seq"],
                "event_hash": a["event_hash"],
            }
            for a in self.adjustments
            if a["period_id"] == period_id
        ]
        return {
            "confirmed": confirmed,
            "holds": holds,
            "payments": payments,
            "trueups": trueups,
            "negative_adjustments": adjustments,
            "disputes": [
                {
                    "participant_id": pid,
                    "case_ref": d["case_ref"],
                    "reason": d["reason"],
                    "active": d["active"],
                    "seq": d["seq"],
                    "event_hash": d["event_hash"],
                }
                for pid, d in sorted(self.disputes.items(), key=lambda kv: kv[1]["seq"])
                if conf
                and any(line["participant_id"] == pid for line in conf["lines"])
            ],
        }

    def participant_evidence_pack(self, pid: str) -> dict[str, Any]:
        """某参与方可下载的完整依据：只含其本方金额，但锁定版本与哈希根与各方一致。"""

        packs = []
        for period_id, conf in self.confirmations.items():
            if not any(line["participant_id"] == pid for line in conf["lines"]):
                continue
            bundle = self.period_bundle(period_id)
            mon = bundle["locked"]["monitoring"]
            packs.append(
                {
                    "period": bundle["period"],
                    "locked": {
                        "method_version": bundle["locked"]["method"]["version"],
                        "method_doc_hash": bundle["locked"]["method"]["doc_hash"],
                        "baseline_version": bundle["locked"]["baseline"]["version"],
                        "baseline_doc_hash": bundle["locked"]["baseline"]["doc_hash"],
                        "boundary_version": bundle["locked"]["boundary"]["version"],
                        "share_rule_version": bundle["locked"]["share_rule"]["version"],
                        "share_rule_event_hash": bundle["locked"]["share_rule"]["event_hash"],
                        "monitoring_event_id": mon["event_id"],
                        "monitoring_event_hash": mon["event_hash"],
                        "evidence": mon["evidence"],
                        "monitoring_history": [
                            {
                                "seq": row["seq"],
                                "event_id": row["event_id"],
                                "event_hash": row["event_hash"],
                                "allocatable_amount": row["allocatable_amount"],
                                "reason": row.get("reason"),
                                "evidence": row["evidence"],
                            }
                            for row in bundle["locked"]["monitoring_history"]
                        ],
                        "receipts": [
                            {"receipt_no": r["receipt_no"], "doc_hash": r["doc_hash"], "event_hash": r["event_hash"]}
                            for r in bundle["locked"]["receipts"]
                        ],
                    },
                    "allocatable_amount": bundle["allocatable_amount"],
                    "confirmation_event_hash": bundle["confirmation_event_hash"],
                    "own_line": self.line_account(period_id, pid),
                    "own_flows": {
                        key: [row for row in rows if row.get("participant_id") == pid or row["kind"] == "confirmation"]
                        for key, rows in bundle["flows"].items()
                        if key != "disputes"
                    },
                    "own_dispute": next(
                        (d for d in bundle["flows"]["disputes"] if d["participant_id"] == pid), None
                    ),
                }
            )
        participant = self.participants.get(pid, {})
        return {
            "ledger_root_hash": self.store.root_hash,
            "participant": {
                "participant_id": pid,
                "name": participant.get("name"),
                "kind": participant.get("kind"),
            },
            "account": self.participant_account(pid),
            "periods": packs,
            "generated_at": now_iso(),
        }

    # ================================================================== 调试

    def dump_event(self, event: Event) -> dict[str, Any]:
        return event_to_dict(event)
