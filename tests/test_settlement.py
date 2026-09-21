"""首期结算演示：先支付、后更正、追补与争议的完整事件流。"""

from __future__ import annotations

import unittest

from service.ledger.engine import LedgerError, cents_str
from tests._fixtures import PERIOD, build_confirmed_engine, raise_trueup_for_correction


class SettlementFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = build_confirmed_engine()

    def pay(self, payment_id: str, pid: str, amount: str, **extra: object) -> None:
        payload = {"period_id": PERIOD, "participant_id": pid,
                   "payment_id": payment_id, "amount": amount, "bank_account": "ACC"}
        payload.update(extra)
        self.eng.append("payment.made", f"evt-{payment_id}", payload, actor="office")

    def test_partial_payment_then_correction_creates_clawback(self) -> None:
        # 1) 首期演示会先支付部分收益：林农甲应得 3500，先付 3000。
        self.pay("P-FA-1", "FA", "3000.00", payment_ref="BANK-REF-1")
        line = self.eng.line_account(PERIOD, "FA")
        self.assertEqual(line["remaining_payable"], "500.00")
        self.assertEqual(line["clawback_due"], "0.00")

        # 2) 监测结果跨期更正：10000 -> 6000（只追加，不改历史）。
        mon_before = self.eng.confirmations[PERIOD]["event_hash"]
        self.eng.append("monitoring.corrected", "mon2", {
            "period_id": PERIOD, "allocatable_amount": "6000.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "evidence-2.pdf", "sha256": "b" * 64}],
            "reason": "样地复测调减",
        }, actor="monitoring_body")
        # 历史确认哈希纹丝不动。
        self.assertEqual(self.eng.confirmations[PERIOD]["event_hash"], mon_before)

        # 3) 每方追补建议：甲应得降到 2100，已领 3000 → 超付 900。
        suggestions = self.eng.suggest_trueups(PERIOD)
        self.assertEqual(cents_str(suggestions["FA"]), "-1400.00")  # 权益差额
        raise_trueup_for_correction(self.eng, "6000.00", reason_event="mon2")

        line = self.eng.line_account(PERIOD, "FA")
        self.assertEqual(line["entitlement"], "2100.00")
        self.assertEqual(line["remaining_payable"], "0.00")
        self.assertEqual(line["clawback_due"], "900.00")  # 追补责任

        # 追补流水把“冲减剩余应付 500”和“形成返还责任 400”之外的口径记清楚：
        tu = next(t for t in self.eng.trueups if t.participant_id == "FA")
        self.assertEqual(cents_str(tu.delta), "-1400.00")
        self.assertEqual(cents_str(tu.payable_offset), "-500.00")
        self.assertEqual(cents_str(tu.clawback), "900.00")

        # 4) 返还只能在追补责任额度内。
        with self.assertRaises(LedgerError):
            self.pay("P-FA-BAD", "FA", "901.00", direction="return")
        self.pay("P-FA-RET", "FA", "900.00", direction="return")
        self.assertEqual(self.eng.line_account(PERIOD, "FA")["clawback_due"], "0.00")

        # 5) 流水齐备：确认/支付/追补，且引用事件哈希。
        flows = self.eng.period_flows(PERIOD)
        self.assertEqual(len(flows["confirmed"]), 1)
        self.assertEqual({p["payment_id"] for p in flows["payments"]}, {"P-FA-1", "P-FA-RET"})
        self.assertTrue(all(t["clawback"] or t["payable_offset"] for t in flows["trueups"]))

    def test_hold_does_not_become_clawback_after_correction(self) -> None:
        # 暂缓的钱尚未出账：更正后即使暂缓额超过新权益，也不产生追补责任，
        # 只是把可付额度压到 0，等待办公室解除多余暂缓。
        self.eng.append("hold.placed", "h-fa", {
            "period_id": PERIOD, "participant_id": "FA", "hold_id": "H-FA",
            "amount": "3500.00", "reason": "全额暂缓待核",
        }, actor="office")
        self.eng.append("monitoring.corrected", "mon2", {
            "period_id": PERIOD, "allocatable_amount": "6000.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e2", "sha256": "b" * 64}], "reason": "复测调减",
        }, actor="monitoring_body")
        raise_trueup_for_correction(self.eng, "6000.00", reason_event="mon2")
        line = self.eng.line_account(PERIOD, "FA")
        self.assertEqual(line["entitlement"], "2100.00")
        self.assertEqual(line["clawback_due"], "0.00")  # 分文未付，无超付
        self.assertEqual(line["remaining_payable"], "0.00")  # 暂缓 3500 压住全部
        tu = next(t for t in self.eng.trueups if t.participant_id == "FA")
        self.assertEqual(tu.clawback, 0)
        # 解除暂缓 1400 后，新权益 2100 恢复可付。
        self.eng.append("hold.released", "h-fa-r", {
            "hold_id": "H-FA", "amount": "1400.00", "reason": "更正后调减暂缓",
        }, actor="office")
        self.assertEqual(self.eng.line_account(PERIOD, "FA")["remaining_payable"], "0.00")
        self.eng.append("hold.released", "h-fa-r2", {
            "hold_id": "H-FA", "amount": "2100.00", "reason": "解除",
        }, actor="office")
        self.assertEqual(self.eng.line_account(PERIOD, "FA")["remaining_payable"], "2100.00")

    def test_upward_correction_pays_additional_via_trueup(self) -> None:
        # 向上更正：10000 -> 11000，每方补付，历史支付不动。
        self.pay("P-C-1", "C", "1000.00")
        self.eng.append("monitoring.corrected", "mon-up", {
            "period_id": PERIOD, "allocatable_amount": "11000.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e3", "sha256": "c" * 64}],
            "reason": "漏计样地上调",
        }, actor="monitoring_body")
        raise_trueup_for_correction(self.eng, "11000.00", reason_event="mon-up")
        line = self.eng.line_account(PERIOD, "C")
        self.assertEqual(line["entitlement"], "1100.00")
        self.assertEqual(line["remaining_payable"], "100.00")
        self.assertEqual(line["clawback_due"], "0.00")
        self.pay("P-C-2", "C", "100.00")
        self.assertEqual(self.eng.line_account(PERIOD, "C")["remaining_payable"], "0.00")

    def test_hold_blocks_payment_until_released(self) -> None:
        self.eng.append("hold.placed", "h1", {
            "period_id": PERIOD, "participant_id": "OP", "hold_id": "H-1",
            "amount": "800.00", "reason": "管护资料待补",
        }, actor="office")
        self.assertEqual(self.eng.line_account(PERIOD, "OP")["remaining_payable"], "2200.00")
        # 暂缓额本身不可支付。
        with self.assertRaises(LedgerError):
            self.pay("P-OP-BAD", "OP", "2201.00")
        self.eng.append("hold.released", "h1r", {
            "hold_id": "H-1", "amount": "800.00", "reason": "资料补齐",
        }, actor="office")
        self.assertEqual(self.eng.line_account(PERIOD, "OP")["remaining_payable"], "3000.00")
        release = next(
            row for row in self.eng.period_flows(PERIOD)["holds"]
            if row["kind"] == "hold_release"
        )
        self.assertEqual(release["released_amount"], "800.00")
        self.assertTrue(release["event_hash"])

    def test_dispute_freeze_stops_funds_only_new_events_resolve(self) -> None:
        self.eng.append("dispute.frozen", "d1", {
            "participant_id": "FB", "case_ref": "CASE-9", "reason": "共有林权异议",
        }, actor="office")
        with self.assertRaises(LedgerError):
            self.pay("P-FB-X", "FB", "100.00")
        # 不能重复冻结。
        with self.assertRaises(LedgerError):
            self.eng.append("dispute.frozen", "d1b", {
                "participant_id": "FB", "case_ref": "CASE-10", "reason": "重复",
            }, actor="office")
        # 只能通过新事件解冻。
        self.eng.append("dispute.resolved", "d2", {
            "participant_id": "FB", "case_ref": "CASE-9", "resolution": "异议不成立",
        }, actor="office")
        self.pay("P-FB-1", "FB", "2500.00")
        self.assertEqual(self.eng.line_account(PERIOD, "FB")["remaining_payable"], "0.00")

    def test_negative_adjustment_is_new_event(self) -> None:
        self.eng.append("negative.adjustment", "na1", {
            "period_id": PERIOD, "participant_id": "OP", "adjustment_id": "NA-1",
            "amount": "-150.00", "reason": "管护考核扣款",
        }, actor="office")
        line = self.eng.line_account(PERIOD, "OP")
        self.assertEqual(line["entitlement"], "2850.00")
        self.assertEqual(line["remaining_payable"], "2850.00")
        # 负调整不改变已确认事件。
        flows = self.eng.period_flows(PERIOD)
        self.assertEqual(flows["negative_adjustments"][0]["adjustment_id"], "NA-1")
        self.assertEqual(len(flows["confirmed"]), 1)

    def test_duplicate_receipt_and_duplicate_submission_are_idempotent(self) -> None:
        # 同一 event_id 网络重试：返回既有事件，不新增流水。
        _, created = self.eng.append("verification.receipt.recorded", "rcp1", {
            "period_id": PERIOD, "receipt_no": "VR-001", "doc_hash": "r" * 64,
        }, actor="verification_body")
        self.assertFalse(created)
        # 新 event_id 但相同业务编号：拒绝。
        with self.assertRaises(LedgerError):
            self.eng.append("verification.receipt.recorded", "rcp-dup", {
                "period_id": PERIOD, "receipt_no": "VR-001", "doc_hash": "r" * 64,
            }, actor="verification_body")

    def test_boundary_and_share_changes_affect_future_periods_only(self) -> None:
        self.eng.append("boundary.changed", "bd2", {
            "version": "BND-2", "area_mu": 1150, "doc_hash": "h2",
        }, actor="boundary_body")
        # 历史期不能改份额。
        with self.assertRaises(LedgerError):
            self.eng.append("share.rule.locked", "sr-hist", {
                "period_id": PERIOD, "version": "SR-2",
                "lines": [{"participant_id": "C", "share": "1"}],
            }, actor="office")
        # 新期使用新边界与新份额规则。
        self.eng.append("period.opened", "per2", {
            "period_id": "2026", "boundary_version": "BND-2",
        }, actor="office")
        self.eng.append("share.rule.locked", "sr26", {
            "period_id": "2026", "version": "SR-26", "lines": [
                {"participant_id": "C", "share": "0.20"},
                {"participant_id": "FA", "share": "0.30"},
                {"participant_id": "FB", "share": "0.20"},
                {"participant_id": "OP", "share": "0.30"},
            ],
        }, actor="office")
        # 2025 历史分配完全不变。
        self.assertEqual(self.eng.line_account(PERIOD, "C")["gross"], "1000.00")


if __name__ == "__main__":
    unittest.main()
