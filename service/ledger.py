"""结算引擎：把事件重放成读模型，并执行只追加的业务规则。

任何更正都不会修改旧事件或旧流水——监测证据更正、份额变化、边界变化
都生成"新版本分配 + TRUE_UP 追补流水"，历史支付因此永远可解释。

余额口径（单位：分）：

* 净应付 outstanding = 确认/追补 − 已付/已解除暂缓/临时收回；
  正数表示项目尚欠参与方，负数表示参与方应向项目退回（负调整责任）。
* 暂缓 withheld 不改变净应付，只锁定其中暂不支付的部分；
  解除并支付时减少净应付。STALE 期间可"收回暂缓款"（retained），
  它临时减少净应付；下一追补版本确认时按"差额 + 收回款"一次结清：
  更正幅度小于收回款的部分必须退回参与方，大于的部分形成追补收回责任。
"""

from __future__ import annotations

from collections import defaultdict

from .models import (
    Allocation,
    AllocationLine,
    AllocationState,
    BoundaryState,
    Dispute,
    DisputeDirection,
    Evidence,
    FlowKind,
    MethodVersion,
    Party,
    PartyType,
    Period,
    Plot,
    ShareRule,
    StatementFlow,
    VerificationReceipt,
)
from .store import EventStore, digest

BPS_TOTAL = 10_000
KG_PER_TONNE = 1_000


class LedgerError(ValueError):
    """业务规则冲突。"""


class Ledger:
    """绑定一个事件存储的领域服务。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._last_applied_seq = 0
        self._replay()

    def _commit(self, event_type: str, payload: dict[str, object], **kwargs):
        """追加事件；仅当事件为新建时应用（幂等命中不重复记账）。"""

        event = self.store.append(event_type, payload, **kwargs)
        if event.seq > self._last_applied_seq:
            self._apply(event)
            self._last_applied_seq = event.seq
        return event

    # ==================================================================
    # 命令：主数据
    # ==================================================================

    def register_method(
        self,
        method_id: str,
        version: int,
        name: str,
        parameters_hash: str,
        note: str = "",
        *,
        idem_key: str | None = None,
    ) -> str:
        if self._find_method(method_id, version):
            raise LedgerError(f"方法 {method_id} v{version} 已存在")
        event = self._commit(
            "method.registered",
            {
                "method_id": method_id,
                "version": version,
                "name": name,
                "parameters_hash": parameters_hash,
                "note": note,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def register_party(
        self,
        party_id: str,
        party_type: PartyType,
        name: str,
        contact: str = "",
        id_number: str = "",
        account_ref: str = "",
        *,
        idem_key: str | None = None,
    ) -> str:
        if party_id in self.parties:
            raise LedgerError(f"参与方 {party_id} 已存在")
        event = self._commit(
            "party.registered",
            {
                "party_id": party_id,
                "party_type": party_type.value,
                "name": name,
                "contact": contact,
                "id_number": id_number,
                "account_ref": account_ref,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def register_plot(self, plot_id: str, name: str, *, idem_key: str | None = None) -> str:
        if plot_id in self.plots:
            raise LedgerError(f"林地 {plot_id} 已存在")
        event = self._commit(
            "plot.registered", {"plot_id": plot_id, "name": name}, idem_key=idem_key
        )
        return event.event_id

    def set_boundary(
        self,
        plot_id: str,
        included: bool,
        area_ha: str,
        effective_from: str,
        note: str = "",
        *,
        idem_key: str | None = None,
    ) -> str:
        """项目边界变化：纳入/退出某地块，只对之后确认的分配版本生效。"""

        if plot_id not in self.plots:
            raise LedgerError(f"未知林地 {plot_id}")
        event = self._commit(
            "boundary.changed",
            {
                "plot_id": plot_id,
                "included": included,
                "area_ha": str(area_ha),
                "effective_from": effective_from,
                "note": note,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def set_share(
        self,
        plot_id: str,
        party_id: str,
        share_bps: int,
        effective_from: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        """登记/修改某地块上参与方的份额（基点）。

        同一参与方在同一地块的新规则按生效日保留历史；已确认的分配不受
        影响，下一版本确认时才采用新份额并产生追补流水。
        """

        if plot_id not in self.plots:
            raise LedgerError(f"未知林地 {plot_id}")
        if party_id not in self.parties:
            raise LedgerError(f"未知参与方 {party_id}")
        if not 0 <= share_bps <= BPS_TOTAL:
            raise LedgerError("份额必须在 0..10000 bps 之间")
        event = self._commit(
            "share.rule_set",
            {
                "plot_id": plot_id,
                "party_id": party_id,
                "share_bps": share_bps,
                "effective_from": effective_from,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def open_period(
        self,
        period_id: str,
        name: str,
        start_date: str,
        end_date: str,
        price_cents_per_t: int,
        *,
        idem_key: str | None = None,
    ) -> str:
        if period_id in self.periods:
            raise LedgerError(f"结算期 {period_id} 已存在")
        if price_cents_per_t <= 0:
            raise LedgerError("碳价必须为正")
        event = self._commit(
            "period.opened",
            {
                "period_id": period_id,
                "name": name,
                "start_date": start_date,
                "end_date": end_date,
                "price_cents_per_t": price_cents_per_t,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def register_receipt(
        self,
        receipt_id: str,
        verifier: str,
        issued_at: str,
        content_hash: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        """登记核证机构回执。同一回执只能被一份监测证据占用。"""

        if receipt_id in self.receipts:
            raise LedgerError(f"回执 {receipt_id} 已登记")
        event = self._commit(
            "receipt.registered",
            {
                "receipt_id": receipt_id,
                "verifier": verifier,
                "issued_at": issued_at,
                "content_hash": content_hash,
            },
            idem_key=idem_key,
        )
        return event.event_id

    # ==================================================================
    # 命令：监测证据（可更正，不删除旧版本）
    # ==================================================================

    def record_evidence(
        self,
        evidence_id: str,
        period_id: str,
        plot_id: str,
        method_id: str,
        method_version: int | None,
        tco2_kg: int,
        source_uri: str,
        submitted_by: str,
        version: int | None = None,
        receipt_id: str | None = None,
        *,
        event_time: str | None = None,
        idem_key: str | None = None,
    ) -> str:
        """提交监测证据。version 省略时自动取该期该地块下一版本号。

        新版本自动通过 corrects_version 指向被更正版本；旧证据保留，
        已确认分配因此转为 STALE，必须确认追补版本后才能继续支付。
        """

        if period_id not in self.periods:
            raise LedgerError(f"未知结算期 {period_id}")
        if plot_id not in self.plots:
            raise LedgerError(f"未知林地 {plot_id}")
        if tco2_kg < 0:
            raise LedgerError("监测碳量不能为负")
        if evidence_id in {e.evidence_id for e in self.evidence}:
            raise LedgerError(f"证据 {evidence_id} 已存在")

        if method_version is None:
            method = self._find_method_latest(method_id)
        else:
            method = self._find_method(method_id, method_version)
        if method is None:
            raise LedgerError(f"未知方法版本 {method_id} v{method_version}")

        existing = [
            e for e in self.evidence if e.period_id == period_id and e.plot_id == plot_id
        ]
        next_version = (max(e.version for e in existing) + 1) if existing else 1
        if version is None:
            version = next_version
        elif version != next_version:
            raise LedgerError(f"证据版本必须连续：下一版本应为 {next_version}")
        corrects = max((e.version for e in existing), default=None)

        receipt_event_seq: int | None = None
        if receipt_id is not None:
            receipt = self.receipts.get(receipt_id)
            if receipt is None:
                raise LedgerError(f"回执 {receipt_id} 未登记")
            if receipt.consumed_by is not None:
                raise LedgerError(
                    f"回执 {receipt_id} 已被证据 {receipt.consumed_by} 占用，禁止重复核证"
                )
            receipt_event_seq = receipt.event_seq

        event = self._commit(
            "evidence.recorded",
            {
                "evidence_id": evidence_id,
                "period_id": period_id,
                "plot_id": plot_id,
                "version": version,
                "method_id": method_id,
                "method_version": method.version,
                "tco2_kg": tco2_kg,
                "source_uri": source_uri,
                "submitted_by": submitted_by,
                "receipt_id": receipt_id,
                "receipt_event_seq": receipt_event_seq,
                "corrects_version": corrects,
            },
            idem_key=idem_key,
            event_time=event_time,
        )
        return event.event_id

    # ==================================================================
    # 命令：确认分配（锁定每期快照）
    # ==================================================================

    def confirm_allocation(
        self,
        period_id: str,
        *,
        as_of: str | None = None,
        idem_key: str | None = None,
    ) -> str:
        """确认该期当前数据形成的分配版本并锁定快照。

        首次确认生成 CONFIRMATION 流水；证据更正、份额或边界变化后再次
        确认，生成新版本与 TRUE_UP 追补流水（差额可正可负），旧版本保留。
        """

        period = self.periods.get(period_id)
        if period is None:
            raise LedgerError(f"未知结算期 {period_id}")
        cutoff = as_of or period.end_date

        included = [
            b
            for b in self.boundaries.values()
            if b.included and b.effective_from <= cutoff
        ]
        if not included:
            raise LedgerError("项目边界内没有任何林地，无法确认分配")

        lines: dict[str, AllocationLine] = {}
        evidence_refs: list[dict[str, object]] = []
        total_t = 0

        for boundary in sorted(included, key=lambda b: b.plot_id):
            plot_id = boundary.plot_id
            plot_evs = [
                e for e in self.evidence if e.period_id == period_id and e.plot_id == plot_id
            ]
            if not plot_evs:
                raise LedgerError(f"地块 {plot_id} 在 {period_id} 缺少监测证据，不能确认")
            ev = max(plot_evs, key=lambda e: e.version)
            method = self._find_method(ev.method_id, ev.method_version)
            if method is None:
                raise LedgerError(
                    f"方法 {ev.method_id} v{ev.method_version} 未登记"
                )

            plot_cents_num = ev.tco2_kg * period.price_cents_per_t
            if plot_cents_num % KG_PER_TONNE != 0:
                raise LedgerError(
                    f"地块 {plot_id} 碳量×单价不能整除到分，请调整单价或监测精度"
                )
            plot_cents = plot_cents_num // KG_PER_TONNE

            rules = self._active_shares(plot_id, cutoff)
            share_sum = sum(r.share_bps for r in rules)
            if share_sum != BPS_TOTAL:
                raise LedgerError(
                    f"地块 {plot_id} 份额合计为 {share_sum} bps，必须恰好为 10000"
                )

            total_t += ev.tco2_kg
            evidence_refs.append(
                {
                    "plot_id": plot_id,
                    "evidence_id": ev.evidence_id,
                    "version": ev.version,
                    "corrects_version": ev.corrects_version,
                    "tco2_kg": ev.tco2_kg,
                    "method_id": ev.method_id,
                    "method_version": method.version,
                    "receipt_id": ev.receipt_id,
                    "evidence_event_seq": ev.recorded_event_seq,
                }
            )

            for rule in rules:
                party_id = rule.party_id
                part_num = plot_cents * rule.share_bps
                if part_num % BPS_TOTAL != 0:
                    raise LedgerError(
                        f"地块 {plot_id} 参与方 {party_id} 的分成不能整除到分"
                    )
                part_cents = part_num // BPS_TOTAL

                line = lines.setdefault(
                    party_id,
                    AllocationLine(
                        party_id=party_id,
                        share_bps=0,
                        tco2_kg=0,
                        amount_cents=0,
                    ),
                )
                line.share_bps += rule.share_bps  # 跨地块仅作展示
                line.tco2_kg += ev.tco2_kg
                line.amount_cents += part_cents
                line.plots.append(
                    {
                        "plot_id": plot_id,
                        "share_bps": rule.share_bps,
                        "tco2_kg": ev.tco2_kg,
                        "amount_cents": part_cents,
                    }
                )
                line.basis.append(self._build_basis(boundary, rule, ev, method, period))

        prev = self._latest_allocation(period_id)
        version = (prev.version + 1) if prev else 1
        if prev:
            for line in lines.values():
                old = prev.lines.get(line.party_id)
                line.delta_cents = line.amount_cents - (old.amount_cents if old else 0)
            for party_id, old in prev.lines.items():
                if party_id not in lines:
                    lines[party_id] = AllocationLine(
                        party_id=party_id,
                        share_bps=0,
                        tco2_kg=0,
                        amount_cents=0,
                        delta_cents=-old.amount_cents,
                    )

        snapshot = {
            "period_id": period_id,
            "version": version,
            "price_cents_per_t": period.price_cents_per_t,
            "distributable_cents": sum(l.amount_cents for l in lines.values()),
            "tco2_kg": total_t,
            "evidence_refs": evidence_refs,
            "lines": [
                {
                    "party_id": l.party_id,
                    "amount_cents": l.amount_cents,
                    "delta_cents": l.delta_cents,
                    "plots": l.plots,
                }
                for l in sorted(lines.values(), key=lambda x: x.party_id)
            ],
            "basis": {l.party_id: l.basis for l in lines.values()},
        }
        allocation_hash = digest(snapshot)

        ordered = sorted(lines.values(), key=lambda x: x.party_id)
        # 把 STALE 期间预先收回的暂缓款并入本版本一次性结清
        retained_settled: dict[str, int] = {}
        if version > 1:
            for party_id in {l.party_id for l in ordered} | {
                p_party
                for (p_period, p_party) in self._retained
                if p_period == period_id
            }:
                amount = self._retained.get((period_id, party_id), 0)
                if amount:
                    retained_settled[party_id] = amount
        event = self._commit(
            "allocation.confirmed",
            {
                "period_id": period_id,
                "version": version,
                "as_of": cutoff,
                "price_cents_per_t": period.price_cents_per_t,
                "distributable_cents": snapshot["distributable_cents"],
                "tco2_kg": total_t,
                "lines": [
                    {
                        "party_id": l.party_id,
                        "amount_cents": l.amount_cents,
                        "delta_cents": l.delta_cents,
                    }
                    for l in ordered
                ],
                "retained_settled": retained_settled,
                "evidence_refs": evidence_refs,
                "basis": snapshot["basis"],
                "allocation_hash": allocation_hash,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def _build_basis(self, boundary, rule, ev: Evidence, method: MethodVersion, period: Period):
        """组装锁定到分配版本里的单笔依据，供各方下载核对。"""

        return {
            "plot_id": boundary.plot_id,
            "share_rule": {
                "share_bps": rule.share_bps,
                "effective_from": rule.effective_from,
                "event_seq": rule.event_seq,
            },
            "boundary": {
                "included": boundary.included,
                "area_ha": boundary.area_ha,
                "effective_from": boundary.effective_from,
                "event_seq": boundary.event_seq,
            },
            "evidence": {
                "evidence_id": ev.evidence_id,
                "version": ev.version,
                "corrects_version": ev.corrects_version,
                "tco2_kg": ev.tco2_kg,
                "source_uri": ev.source_uri,
                "submitted_by": ev.submitted_by,
                "receipt_id": ev.receipt_id,
                "event_seq": ev.recorded_event_seq,
                "recorded_at": ev.recorded_at,
            },
            "method": {
                "method_id": method.method_id,
                "version": method.version,
                "name": method.name,
                "parameters_hash": method.parameters_hash,
                "event_seq": method.event_seq,
            },
            "price_cents_per_t": period.price_cents_per_t,
            "period_event_seq": period.event_seq,
        }

    # ==================================================================
    # 命令：暂缓、支付、收回
    # ==================================================================

    def withhold(
        self,
        period_id: str,
        party_id: str,
        amount_cents: int,
        reason: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        """暂缓部分应付：净应付不变，该部分被锁定，不能支付或再次暂缓。"""

        self._require_party(period_id, party_id)
        if amount_cents <= 0:
            raise LedgerError("暂缓金额必须为正")
        available = self._available_outward(period_id, party_id)
        if amount_cents > available:
            raise LedgerError(
                f"暂缓超额：可暂缓 {available} 分（净应付扣除已支付/已暂缓/冻结）"
            )
        event = self._commit(
            "payment.withheld",
            {
                "period_id": period_id,
                "party_id": party_id,
                "amount_cents": amount_cents,
                "reason": reason,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def pay(
        self,
        period_id: str,
        party_id: str,
        amount_cents: int,
        reference: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        """支付应付。争议冻结金额不可支付；STALE 期间暂停支付。"""

        self._require_party(period_id, party_id)
        if amount_cents <= 0:
            raise LedgerError("支付金额必须为正")
        alloc = self._require_confirmed(period_id)
        if alloc.state == AllocationState.STALE:
            raise LedgerError("监测结果已更正但追补版本尚未确认，暂停支付")
        available = self._available_outward(period_id, party_id)
        if amount_cents > available:
            raise LedgerError(
                f"支付超额：可支付 {available} 分（净应付扣除已支付/已暂缓/冻结）"
            )
        event = self._commit(
            "payment.made",
            {
                "period_id": period_id,
                "party_id": party_id,
                "amount_cents": amount_cents,
                "reference": reference,
                "reason": f"支付凭证 {reference}",
                "allocation_version": alloc.version,
                "allocation_hash": alloc.allocation_hash,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def release_withheld(
        self,
        period_id: str,
        party_id: str,
        amount_cents: int,
        reference: str,
        *,
        reclaim: bool = False,
        idem_key: str | None = None,
    ) -> str:
        """解除暂缓：reclaim=False 解除并支付；reclaim=True 收回暂缓款。"""

        self._require_party(period_id, party_id)
        if amount_cents <= 0:
            raise LedgerError("金额必须为正")
        withheld_total = self._withheld_total(period_id, party_id)
        if amount_cents > withheld_total:
            raise LedgerError(f"暂缓余额只有 {withheld_total} 分")

        if reclaim:
            alloc = self._latest_allocation(period_id)
            if alloc is None or alloc.state != AllocationState.STALE:
                raise LedgerError(
                    "只有在监测更正后、追补版本确认前（STALE）才能收回暂缓款，"
                    "收回款将在下一追补版本确认时一次结清"
                )
            event = self._commit(
                "withheld.reclaimed",
                {
                    "period_id": period_id,
                    "party_id": party_id,
                    "amount_cents": amount_cents,
                    "reference": reference,
                    "reason": f"暂缓款收回：{reference}",
                },
                idem_key=idem_key,
            )
            return event.event_id

        alloc = self._require_confirmed(period_id)
        if alloc.state == AllocationState.STALE:
            raise LedgerError("监测结果已更正但追补版本尚未确认，暂缓款不能支付")
        if self._frozen_out(period_id, party_id) > 0:
            raise LedgerError("该期存在对外支付争议冻结，暂缓款不能支付")
        event = self._commit(
            "withheld.released",
            {
                "period_id": period_id,
                "party_id": party_id,
                "amount_cents": amount_cents,
                "reference": reference,
                "reason": f"暂缓解除并支付：{reference}",
                "allocation_version": alloc.version,
                "allocation_hash": alloc.allocation_hash,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def claim_back(
        self,
        period_id: str,
        party_id: str,
        amount_cents: int,
        reference: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        """追补收回：参与方退回多付的钱（负调整形成的责任）。"""

        self._require_party(period_id, party_id)
        if amount_cents <= 0:
            raise LedgerError("收回金额必须为正")
        owed_back = max(0, -self._net_outstanding(period_id, party_id))
        frozen_in = self._frozen_in(period_id, party_id)
        available = max(0, owed_back - frozen_in)
        if amount_cents > available:
            raise LedgerError(
                f"收回超额：应退回 {owed_back} 分，对内冻结 {frozen_in} 分，可收 {available} 分"
            )
        event = self._commit(
            "trueup.claimed_back",
            {
                "period_id": period_id,
                "party_id": party_id,
                "amount_cents": amount_cents,
                "reference": reference,
                "reason": f"追补收回（负调整退回）：{reference}",
            },
            idem_key=idem_key,
        )
        return event.event_id

    # ==================================================================
    # 命令：争议冻结
    # ==================================================================

    def open_dispute(
        self,
        dispute_id: str,
        period_id: str,
        party_id: str,
        amount_cents: int,
        direction: DisputeDirection,
        reason: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        self._require_party(period_id, party_id)
        if amount_cents <= 0:
            raise LedgerError("冻结金额必须为正")
        if dispute_id in self.disputes:
            raise LedgerError(f"争议 {dispute_id} 已存在")
        if direction == DisputeDirection.OUTWARD:
            cap = self._available_outward(period_id, party_id)
            if amount_cents > cap:
                raise LedgerError(f"对外冻结超额：最多可冻结 {cap} 分")
        else:
            cap = max(0, -self._net_outstanding(period_id, party_id))
            if amount_cents > cap:
                raise LedgerError(f"对内冻结超额：最多可冻结 {cap} 分")
        event = self._commit(
            "dispute.opened",
            {
                "dispute_id": dispute_id,
                "period_id": period_id,
                "party_id": party_id,
                "amount_cents": amount_cents,
                "direction": direction.value,
                "reason": reason,
            },
            idem_key=idem_key,
        )
        return event.event_id

    def resolve_dispute(
        self,
        dispute_id: str,
        resolution: str,
        *,
        idem_key: str | None = None,
    ) -> str:
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise LedgerError(f"未知争议 {dispute_id}")
        if dispute.resolved:
            raise LedgerError(f"争议 {dispute_id} 已解决")
        event = self._commit(
            "dispute.resolved",
            {"dispute_id": dispute_id, "resolution": resolution},
            idem_key=idem_key,
        )
        return event.event_id

    # ==================================================================
    # 查询
    # ==================================================================

    def statement(self, period_id: str, party_id: str) -> dict[str, object]:
        """生成某方在某期的对账依据（所有方下载到同一锁定快照哈希）。"""

        if period_id not in self.periods:
            raise LedgerError(f"未知结算期 {period_id}")
        if party_id not in self.parties:
            raise LedgerError(f"未知参与方 {party_id}")
        alloc = self._latest_allocation(period_id)
        flows = [
            f
            for f in self.flows
            if f.period_id == period_id and f.party_id == party_id
        ]
        line = alloc.lines.get(party_id) if alloc else None
        outstanding = self._net_outstanding(period_id, party_id)
        return {
            "period_id": period_id,
            "party_id": party_id,
            "chain_head_hash": self.store.head_hash(),
            "allocation_version": alloc.version if alloc else None,
            "allocation_state": alloc.state.value if alloc else "unconfirmed",
            "allocation_hash": alloc.allocation_hash if alloc else None,
            "confirmed_total_cents": line.amount_cents if line else 0,
            "outstanding_cents": outstanding,
            "withheld_cents": self._withheld_total(period_id, party_id),
            "frozen_outward_cents": self._frozen_out(period_id, party_id),
            "frozen_inward_cents": self._frozen_in(period_id, party_id),
            "payable_cents": self._available_outward(period_id, party_id),
            "claimback_due_cents": max(0, -outstanding),
            "retained_pending_cents": self._retained.get((period_id, party_id), 0),
            "basis": line.basis if line else [],
            "flows": [
                {
                    "flow_seq": f.flow_seq,
                    "event_seq": f.event_seq,
                    "kind": f.kind.value,
                    "amount_cents": f.amount_cents,
                    "balance_after_cents": f.balance_after_cents,
                    "frozen_after_cents": f.frozen_after_cents,
                    "reason": f.reason,
                    "ref": f.ref,
                    "created_at": f.created_at,
                }
                for f in flows
            ],
        }

    def period_summary(self, period_id: str) -> dict[str, object]:
        """项目办公室视角的某期全量汇总。"""

        alloc = self._latest_allocation(period_id)
        if alloc is None:
            raise LedgerError(f"{period_id} 尚未确认任何分配版本")
        return {
            "period_id": period_id,
            "chain_head_hash": self.store.head_hash(),
            "allocation_version": alloc.version,
            "allocation_state": alloc.state.value,
            "allocation_hash": alloc.allocation_hash,
            "distributable_cents": alloc.distributable_cents,
            "tco2_kg": alloc.tco2_kg,
            "evidence_refs": alloc.evidence_refs,
            "parties": [
                {
                    "party_id": pid,
                    **{
                        k: v
                        for k, v in self.statement(period_id, pid).items()
                        if k
                        in (
                            "confirmed_total_cents",
                            "outstanding_cents",
                            "withheld_cents",
                            "frozen_outward_cents",
                            "frozen_inward_cents",
                            "payable_cents",
                            "claimback_due_cents",
                            "retained_pending_cents",
                        )
                    },
                }
                for pid in sorted(alloc.lines)
            ],
        }

    def allocation_versions(self, period_id: str) -> list[dict[str, object]]:
        """列出某期所有历史锁定版本（审计用）。"""

        return [
            {
                "version": a.version,
                "state": a.state.value,
                "distributable_cents": a.distributable_cents,
                "tco2_kg": a.tco2_kg,
                "allocation_hash": a.allocation_hash,
                "confirmed_event_seq": a.confirmed_event_seq,
                "evidence_refs": a.evidence_refs,
            }
            for a in self.allocations.get(period_id, [])
        ]

    # ==================================================================
    # 内部：重放
    # ==================================================================

    def _replay(self) -> None:
        self.methods: list[MethodVersion] = []
        self.parties: dict[str, Party] = {}
        self.plots: dict[str, Plot] = {}
        self.boundaries: dict[str, BoundaryState] = {}
        self.share_rules: list[ShareRule] = []
        self.periods: dict[str, Period] = {}
        self.receipts: dict[str, VerificationReceipt] = {}
        self.evidence: list[Evidence] = []
        self.allocations: dict[str, list[Allocation]] = {}
        self.flows: list[StatementFlow] = []
        self.disputes: dict[str, Dispute] = {}
        self._balance: dict[tuple[str, str], int] = defaultdict(int)
        self._frozen: dict[tuple[str, str, str], int] = defaultdict(int)
        # STALE 期间预先收回的暂缓款，下一追补版本确认时结清
        self._retained: dict[tuple[str, str], int] = defaultdict(int)
        self._flow_seq = 0
        for event in self.store.all():
            self._apply(event)
            self._last_applied_seq = event.seq

    def _apply(self, event) -> None:  # noqa: C901 - 事件类型分发
        p = event.payload
        et = event.event_type

        if et == "method.registered":
            self.methods.append(
                MethodVersion(
                    method_id=p["method_id"],
                    version=p["version"],
                    name=p["name"],
                    parameters_hash=p["parameters_hash"],
                    note=p.get("note", ""),
                    event_seq=event.seq,
                )
            )
        elif et == "party.registered":
            self.parties[p["party_id"]] = Party(
                party_id=p["party_id"],
                party_type=PartyType(p["party_type"]),
                name=p["name"],
                contact=p.get("contact", ""),
                id_number=p.get("id_number", ""),
                account_ref=p.get("account_ref", ""),
                event_seq=event.seq,
            )
        elif et == "plot.registered":
            self.plots[p["plot_id"]] = Plot(
                plot_id=p["plot_id"], name=p["name"], event_seq=event.seq
            )
        elif et == "boundary.changed":
            self.boundaries[p["plot_id"]] = BoundaryState(
                plot_id=p["plot_id"],
                included=p["included"],
                area_ha=p["area_ha"],
                effective_from=p["effective_from"],
                note=p.get("note", ""),
                event_seq=event.seq,
            )
        elif et == "share.rule_set":
            self.share_rules.append(
                ShareRule(
                    plot_id=p["plot_id"],
                    party_id=p["party_id"],
                    share_bps=p["share_bps"],
                    effective_from=p["effective_from"],
                    event_seq=event.seq,
                )
            )
        elif et == "period.opened":
            self.periods[p["period_id"]] = Period(
                period_id=p["period_id"],
                name=p["name"],
                start_date=p["start_date"],
                end_date=p["end_date"],
                price_cents_per_t=p["price_cents_per_t"],
                event_seq=event.seq,
            )
        elif et == "receipt.registered":
            self.receipts[p["receipt_id"]] = VerificationReceipt(
                receipt_id=p["receipt_id"],
                verifier=p["verifier"],
                issued_at=p["issued_at"],
                content_hash=p["content_hash"],
                event_seq=event.seq,
            )
        elif et == "evidence.recorded":
            self.evidence.append(
                Evidence(
                    evidence_id=p["evidence_id"],
                    period_id=p["period_id"],
                    plot_id=p["plot_id"],
                    version=p["version"],
                    method_id=p["method_id"],
                    method_version=p.get("method_version", 1),
                    tco2_kg=p["tco2_kg"],
                    source_uri=p["source_uri"],
                    submitted_by=p["submitted_by"],
                    receipt_id=p.get("receipt_id"),
                    corrects_version=p.get("corrects_version"),
                    recorded_event_seq=event.seq,
                    recorded_at=event.event_time,
                )
            )
            if p.get("receipt_id"):
                self.receipts[p["receipt_id"]].consumed_by = p["evidence_id"]
            # 引用了该地块旧版本证据的已确认分配全部作废为 STALE
            for alloc in self.allocations.get(p["period_id"], []):
                refs = {r["plot_id"]: r for r in alloc.evidence_refs}
                if p["plot_id"] in refs and refs[p["plot_id"]]["version"] < p["version"]:
                    alloc.state = AllocationState.STALE
        elif et == "allocation.confirmed":
            basis_by_party = p.get("basis", {})
            lines = {
                lp["party_id"]: AllocationLine(
                    party_id=lp["party_id"],
                    share_bps=0,
                    tco2_kg=0,
                    amount_cents=lp["amount_cents"],
                    delta_cents=lp.get("delta_cents", 0),
                    basis=basis_by_party.get(lp["party_id"], []),
                )
                for lp in p["lines"]
            }
            version = p["version"]
            alloc = Allocation(
                period_id=p["period_id"],
                version=version,
                state=AllocationState.CONFIRMED,
                distributable_cents=p["distributable_cents"],
                tco2_kg=p["tco2_kg"],
                lines=lines,
                price_cents_per_t=p["price_cents_per_t"],
                confirmed_event_seq=event.seq,
                allocation_hash=p["allocation_hash"],
                evidence_refs=[dict(r) for r in p["evidence_refs"]],
            )
            self.allocations.setdefault(p["period_id"], []).append(alloc)
            # 确认/追补流水由锁定事件直接派生（重放结果唯一）
            settled = p.get("retained_settled", {})
            for lp in sorted(p["lines"], key=lambda x: x["party_id"]):
                if version == 1:
                    if lp["amount_cents"] > 0:
                        self._record_flow(
                            p["period_id"], lp["party_id"],
                            FlowKind.CONFIRMATION, lp["amount_cents"],
                            f"{p['period_id']} v1 首次确认应分",
                            {
                                "allocation_version": 1,
                                "allocation_hash": p["allocation_hash"],
                            },
                            event,
                        )
                else:
                    settled_amount = int(settled.get(lp["party_id"], 0))
                    true_up = lp["delta_cents"] + settled_amount
                    self._retained[(p["period_id"], lp["party_id"])] -= settled_amount
                    if true_up:
                        reason = (
                            f"{p['period_id']} v{version} 跨期更正追补"
                            f"（监测/份额/边界变化，分配差额 {lp['delta_cents']} 分"
                        )
                        if settled_amount:
                            reason += f"，结清预收回暂缓款 {settled_amount} 分"
                        reason += "）"
                        self._record_flow(
                            p["period_id"], lp["party_id"],
                            FlowKind.TRUE_UP, true_up, reason,
                            {
                                "allocation_version": version,
                                "allocation_hash": p["allocation_hash"],
                                "delta_cents": lp["delta_cents"],
                                "retained_settled_cents": settled_amount,
                            },
                            event,
                        )
        elif et in (
            "payment.withheld",
            "payment.made",
            "withheld.released",
            "withheld.reclaimed",
            "trueup.claimed_back",
        ):
            signed_map = {
                "payment.withheld": (FlowKind.WITHHELD, 0),
                "payment.made": (FlowKind.PAYMENT, -p["amount_cents"]),
                "withheld.released": (FlowKind.RELEASED, -p["amount_cents"]),
                "withheld.reclaimed": (FlowKind.RECLAIMED, -p["amount_cents"]),
                "trueup.claimed_back": (FlowKind.CLAIMBACK, p["amount_cents"]),
            }
            kind, signed = signed_map[et]
            self._record_flow(
                p["period_id"], p["party_id"], kind, signed,
                p.get("reason", p.get("reference", "")),
                {"event_id": event.event_id, "reference": p.get("reference")},
                event,
            )
            if et == "withheld.reclaimed":
                self._retained[(p["period_id"], p["party_id"])] += p["amount_cents"]
        elif et == "dispute.opened":
            self.disputes[p["dispute_id"]] = Dispute(
                dispute_id=p["dispute_id"],
                period_id=p["period_id"],
                party_id=p["party_id"],
                amount_cents=p["amount_cents"],
                direction=DisputeDirection(p["direction"]),
                reason=p["reason"],
                opened_event_seq=event.seq,
                opened_at=event.event_time,
            )
            self._frozen[(p["period_id"], p["party_id"], p["direction"])] += p["amount_cents"]
            self._record_flow(
                p["period_id"], p["party_id"], FlowKind.FROZEN, 0,
                f"争议冻结 {p['amount_cents']} 分：{p['reason']}",
                {"dispute_id": p["dispute_id"], "direction": p["direction"]},
                event,
            )
        elif et == "dispute.resolved":
            dispute = self.disputes[p["dispute_id"]]
            dispute.resolved = True
            dispute.resolution = p["resolution"]
            dispute.resolved_event_seq = event.seq
            self._frozen[
                (dispute.period_id, dispute.party_id, dispute.direction.value)
            ] -= dispute.amount_cents
            self._record_flow(
                dispute.period_id, dispute.party_id, FlowKind.UNFROZEN, 0,
                f"争议解除：{p['resolution']}",
                {"dispute_id": dispute.dispute_id},
                event,
            )
        else:  # pragma: no cover - 未知事件类型应尽早暴露
            raise LedgerError(f"未知事件类型：{et}")

    def _record_flow(self, period_id, party_id, kind, signed_amount, reason, ref, event):
        self._balance[(period_id, party_id)] += signed_amount
        self._flow_seq += 1
        key = (period_id, party_id)
        self.flows.append(
            StatementFlow(
                flow_seq=self._flow_seq,
                event_seq=event.seq,
                period_id=period_id,
                party_id=party_id,
                kind=kind,
                amount_cents=signed_amount,
                balance_after_cents=self._balance[key],
                frozen_after_cents=(
                    self._frozen[(key[0], key[1], DisputeDirection.OUTWARD.value)]
                    + self._frozen[(key[0], key[1], DisputeDirection.INWARD.value)]
                ),
                reason=reason,
                ref=dict(ref),
                created_at=event.event_time,
            )
        )

    # ------------------------------------------------------------------
    # 余额计算
    # ------------------------------------------------------------------

    def _net_outstanding(self, period_id: str, party_id: str) -> int:
        return self._balance[(period_id, party_id)]

    def _withheld_total(self, period_id: str, party_id: str) -> int:
        """暂缓余额：暂缓事件累计 − 已解除/已收回（暂缓流水对净额影响为 0）。"""

        total = 0
        for event in self.store.all():
            p = event.payload
            if p.get("period_id") != period_id or p.get("party_id") != party_id:
                continue
            if event.event_type == "payment.withheld":
                total += p["amount_cents"]
            elif event.event_type in ("withheld.released", "withheld.reclaimed"):
                total -= p["amount_cents"]
        return max(0, total)

    def _frozen_out(self, period_id: str, party_id: str) -> int:
        return max(0, self._frozen[(period_id, party_id, DisputeDirection.OUTWARD.value)])

    def _frozen_in(self, period_id: str, party_id: str) -> int:
        return max(0, self._frozen[(period_id, party_id, DisputeDirection.INWARD.value)])

    def _available_outward(self, period_id: str, party_id: str) -> int:
        """可对外支付/暂缓/冻结：净应付 − 暂缓 − 对外冻结。"""

        return max(
            0,
            self._net_outstanding(period_id, party_id)
            - self._withheld_total(period_id, party_id)
            - self._frozen_out(period_id, party_id),
        )

    # ------------------------------------------------------------------
    # 辅助查询
    # ------------------------------------------------------------------

    def _require_party(self, period_id: str, party_id: str) -> None:
        if period_id not in self.periods:
            raise LedgerError(f"未知结算期 {period_id}")
        if party_id not in self.parties:
            raise LedgerError(f"未知参与方 {party_id}")

    def _require_confirmed(self, period_id: str) -> Allocation:
        alloc = self._latest_allocation(period_id)
        if alloc is None:
            raise LedgerError(f"{period_id} 尚未确认任何分配版本")
        return alloc

    def _latest_allocation(self, period_id: str) -> Allocation | None:
        versions = self.allocations.get(period_id, [])
        return versions[-1] if versions else None

    def _find_method(self, method_id: str, version: int) -> MethodVersion | None:
        for m in self.methods:
            if m.method_id == method_id and m.version == version:
                return m
        return None

    def _find_method_latest(self, method_id: str) -> MethodVersion | None:
        found = [m for m in self.methods if m.method_id == method_id]
        return found[-1] if found else None

    def _active_shares(self, plot_id: str, as_of_date: str) -> list[ShareRule]:
        """取每方在该地块 <= cutoff 的最新份额规则。"""

        latest: dict[str, ShareRule] = {}
        for rule in self.share_rules:
            if rule.plot_id != plot_id or rule.effective_from > as_of_date:
                continue
            cur = latest.get(rule.party_id)
            if cur is None or rule.event_seq > cur.event_seq:
                latest[rule.party_id] = rule
        return list(latest.values())
