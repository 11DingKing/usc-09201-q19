"""事件定义。

账本只追加（append-only）：跨期更正、共有林地份额变化、重复核证回执、
负调整与争议冻结都只能通过 *新事件* 改变余额，已确认与已支付的流水
永不被就地改写。

每个事件携带：
- ``seq``        账本内全局序号（由存储层分配）
- ``event_id``   幂等键，由提交方提供，重复提交同一编号得到同一事件
- ``occurred_at`` 业务发生时间（ISO 字符串，由提交方声明）
- ``actor``      提交角色
- ``payload``    事件参数
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EventType = Literal[
    "method.registered",
    "baseline.registered",
    "participant.registered",
    "share.rule.locked",
    "period.opened",
    "monitoring.submitted",
    "monitoring.corrected",
    "boundary.changed",
    "verification.receipt.recorded",
    "distribution.confirmed",
    "hold.placed",
    "hold.released",
    "dispute.frozen",
    "dispute.resolved",
    "payment.made",
    "negative.adjustment",
    "trueup.raised",
]

# 各事件允许的提交角色（提交权限与读取可见性是两件事）。
EVENT_AUTHORIZED_ROLES: dict[str, frozenset[str]] = {
    "method.registered": frozenset({"methodology_body", "office"}),
    "baseline.registered": frozenset({"baseline_body", "office"}),
    "participant.registered": frozenset({"office"}),
    "share.rule.locked": frozenset({"office"}),
    "period.opened": frozenset({"office"}),
    "monitoring.submitted": frozenset({"monitoring_body"}),
    "monitoring.corrected": frozenset({"monitoring_body"}),
    "boundary.changed": frozenset({"boundary_body", "office"}),
    "verification.receipt.recorded": frozenset({"verification_body", "office"}),
    "distribution.confirmed": frozenset({"office"}),
    "hold.placed": frozenset({"office"}),
    "hold.released": frozenset({"office"}),
    "dispute.frozen": frozenset({"office"}),
    "dispute.resolved": frozenset({"office"}),
    "payment.made": frozenset({"office"}),
    "negative.adjustment": frozenset({"office"}),
    "trueup.raised": frozenset({"office"}),
}

# 事件 payload 中属于敏感交易信息的字段（分角色裁剪）。
SENSITIVE_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "participant.registered": ("bank_account", "id_number", "contact"),
    "payment.made": ("bank_account", "payment_ref"),
    "distribution.confirmed": ("bank_account",),
    "trueup.raised": ("bank_account",),
    "negative.adjustment": ("bank_account",),
}


@dataclass(frozen=True, slots=True)
class Event:
    """一条不可变事件。"""

    seq: int
    event_id: str
    type: EventType  # type: ignore[valid-type]
    payload: dict[str, Any]
    occurred_at: str
    actor: str
    prev_hash: str
    event_hash: str
