"""森林碳益共享账的领域模型。

核心原则：

* 所有业务事实都是只追加（append-only）的事件，余额只能由事件重放得到；
* 每个结算期在确认时锁定方法版本、监测证据、可分配量与参与方份额；
* 更正、份额变化、重复回执、负调整与争议冻结都只能以"新事件"改变余额，
  已支付流水永远不会被悄悄改写。

金额单位：人民币分（整数）；碳量单位：千克二氧化碳当量（整数 tCO2e kg）；
份额单位：基点 bps（10000 bps = 100%）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class PartyType(str, Enum):
    """参与方类型。"""

    FOREST_FARMER = "forest_farmer"   # 林农
    OPERATOR = "operator"             # 运营方
    COLLECTIVE = "collective"         # 村集体


class Role(str, Enum):
    """访问系统的角色（与参与方身份相互独立）。"""

    ADMIN = "admin"
    AUDITOR = "auditor"
    MONITOR = "monitor"
    OPERATOR = "operator"
    FARMER = "forest_farmer"
    PUBLIC = "public"


class AllocationState(str, Enum):
    """某期分配的状态。"""

    PROVISIONAL = "provisional"  # 仅有监测证据，尚未确认
    CONFIRMED = "confirmed"      # 已确认并锁定版本
    STALE = "stale"              # 确认后证据被更正，等待追补版本


class FlowKind(str, Enum):
    """参与方账上的流水类型。amount_cents 均为对"应付余额"的带符号影响。"""

    CONFIRMATION = "confirmation"    # 首次确认应分（+）
    TRUE_UP = "true_up"              # 更正追补（可正可负）
    WITHHELD = "withheld"            # 暂缓（应付减少 -）
    PAYMENT = "payment"              # 支付（应付减少 -）
    RELEASED = "released"            # 暂缓解除并支付（应付减少 -）
    CLAIMBACK = "claimback"          # 追补收回（应付增加 +，冲抵负数责任）
    RECLAIMED = "reclaimed"          # 暂缓款被收回（应付增加 +）
    FROZEN = "frozen"                # 争议冻结（仅记录，不改变净额）
    UNFROZEN = "unfrozen"            # 争议解除（仅记录）


class DisputeDirection(str, Enum):
    """争议冻结的方向。"""

    OUTWARD = "outward"  # 冻结对外支付/暂缓
    INWARD = "inward"    # 冻结追补收回


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """链式哈希保护的不可变事件。"""

    seq: int
    event_id: str
    event_type: str
    event_time: str
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str
    idem_key: str | None = None


# ---------------------------------------------------------------------------
# 读模型（由事件重放得到，不是事实来源）
# ---------------------------------------------------------------------------


@dataclass
class MethodVersion:
    method_id: str
    version: int
    name: str
    parameters_hash: str
    note: str
    event_seq: int


@dataclass
class Party:
    party_id: str
    party_type: PartyType
    name: str
    contact: str
    id_number: str
    account_ref: str
    event_seq: int


@dataclass
class Plot:
    plot_id: str
    name: str
    event_seq: int


@dataclass
class BoundaryState:
    plot_id: str
    included: bool
    area_ha: str
    effective_from: str
    note: str
    event_seq: int


@dataclass
class ShareRule:
    plot_id: str
    party_id: str
    share_bps: int
    effective_from: str
    event_seq: int


@dataclass
class Period:
    period_id: str
    name: str
    start_date: str
    end_date: str
    price_cents_per_t: int
    event_seq: int


@dataclass
class VerificationReceipt:
    receipt_id: str
    verifier: str
    issued_at: str
    content_hash: str
    event_seq: int
    consumed_by: str | None = None  # 占用该回执的证据事件 id


@dataclass
class Evidence:
    evidence_id: str
    period_id: str
    plot_id: str
    version: int
    method_id: str
    method_version: int
    tco2_kg: int
    source_uri: str
    submitted_by: str
    receipt_id: str | None
    corrects_version: int | None
    recorded_event_seq: int
    recorded_at: str


@dataclass
class AllocationLine:
    """一个参与方在某期某版本下的应分金额（跨地块汇总）。"""

    party_id: str
    share_bps: int
    tco2_kg: int
    amount_cents: int
    delta_cents: int = 0  # 相对上一版本的差额（追补额，可负）
    plots: list[dict[str, Any]] = field(default_factory=list)
    # 锁定的依据：方法、证据版本、回执、份额规则、边界
    basis: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Allocation:
    period_id: str
    version: int
    state: AllocationState
    distributable_cents: int
    tco2_kg: int
    lines: dict[str, AllocationLine]
    price_cents_per_t: int
    confirmed_event_seq: int | None
    allocation_hash: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StatementFlow:
    """参与方对账单上的一条流水。"""

    flow_seq: int
    event_seq: int
    period_id: str
    party_id: str
    kind: FlowKind
    amount_cents: int          # 对应付余额的带符号影响（冻结类为 0）
    balance_after_cents: int   # 该方该期净额（应付正/责任负），冻结类记录冻结后
    frozen_after_cents: int
    reason: str
    ref: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class Dispute:
    dispute_id: str
    period_id: str
    party_id: str
    amount_cents: int
    direction: DisputeDirection
    reason: str
    opened_event_seq: int
    opened_at: str
    resolved: bool = False
    resolution: str = ""
    resolved_event_seq: int | None = None
