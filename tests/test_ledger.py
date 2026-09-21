"""结算引擎测试：版本锁定、更正追补、份额/边界变化、负调整、冻结。"""

from __future__ import annotations

import os
import tempfile
import unittest

from service.ledger import Ledger, LedgerError
from service.models import AllocationState, DisputeDirection, PartyType
from service.store import EventStore


def build_ledger(store: EventStore | None = None) -> tuple[Ledger, str]:
    """单地块 P1：F1 60% / O1 30% / C1 10%，碳价 50 元/吨。"""

    L = Ledger(store or EventStore())
    L.register_method("M1", 1, "方法学", "h1")
    for pid, ptype in [
        ("F1", PartyType.FOREST_FARMER),
        ("O1", PartyType.OPERATOR),
        ("C1", PartyType.COLLECTIVE),
    ]:
        L.register_party(pid, ptype, pid)
    L.register_plot("P1", "山场")
    L.set_boundary("P1", True, "100", "2026-01-01")
    L.set_share("P1", "F1", 6000, "2026-01-01")
    L.set_share("P1", "O1", 3000, "2026-01-01")
    L.set_share("P1", "C1", 1000, "2026-01-01")
    L.open_period("P", "2026H1", "2026-01-01", "2026-06-30", 5000)
    return L, "P"


def seed_period(L: Ledger, tco2_kg: int, pid: str = "P", receipt: str = "R1") -> None:
    L.register_receipt(receipt, "核证方", "2026-07-01", "c")
    L.record_evidence(
        "E1", pid, "P1", "M1", 1, tco2_kg, "u", "监测方", receipt_id=receipt
    )
    L.confirm_allocation(pid)


class ConfirmationTest(unittest.TestCase):
    def test_v1_confirmation_lines(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        summary = L.period_summary(pid)
        # 100t × 50 元 = 5000 元 = 500000 分
        self.assertEqual(summary["distributable_cents"], 500_000)
        self.assertEqual(summary["allocation_state"], "confirmed")
        amounts = {
            p["party_id"]: p["confirmed_total_cents"] for p in summary["parties"]
        }
        self.assertEqual(amounts, {"C1": 50_000, "F1": 300_000, "O1": 150_000})
        st = L.statement(pid, "F1")
        self.assertEqual(len(st["basis"]), 1)
        self.assertEqual(st["basis"][0]["method"]["version"], 1)
        self.assertTrue(st["allocation_hash"])

    def test_share_must_sum_to_total(self) -> None:
        L, pid = build_ledger()
        L.register_receipt("R", "v", "t", "c")
        L.record_evidence("E1", pid, "P1", "M1", 1, 100_000, "u", "s", receipt_id="R")
        L.set_share("P1", "O1", 2000, "2026-01-01")  # 合计变 9000
        with self.assertRaises(LedgerError):
            L.confirm_allocation(pid)

    def test_evidence_required_per_included_plot(self) -> None:
        L, pid = build_ledger()
        L.register_plot("P2", "二场")
        L.set_boundary("P2", True, "10", "2026-01-01")
        for party_id, bps in [("F1", 6000), ("O1", 3000), ("C1", 1000)]:
            L.set_share("P2", party_id, bps, "2026-01-01")
        with self.assertRaises(LedgerError):
            L.confirm_allocation(pid)

    def test_history_not_rewritten_on_correction(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        v1 = L.allocation_versions(pid)[0]
        L.record_evidence("E2", pid, "P1", "M1", 1, 80_000, "u2", "监测方")
        L.confirm_allocation(pid)
        versions = L.allocation_versions(pid)
        self.assertEqual(versions[0]["allocation_hash"], v1["allocation_hash"])
        self.assertEqual(versions[0]["distributable_cents"], v1["distributable_cents"])
        self.assertEqual(versions[0]["state"], "stale")
        self.assertEqual(versions[1]["state"], "confirmed")
        self.assertEqual(versions[1]["distributable_cents"], 400_000)


class TrueUpTest(unittest.TestCase):
    def test_positive_correction_adds(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.record_evidence("E2", pid, "P1", "M1", 1, 120_000, "u2", "s")
        L.confirm_allocation(pid)
        st = L.statement(pid, "F1")
        self.assertEqual(st["confirmed_total_cents"], 360_000)
        self.assertEqual(st["outstanding_cents"], 360_000)
        self.assertEqual([f["kind"] for f in st["flows"]], ["confirmation", "true_up"])
        self.assertEqual(st["flows"][1]["amount_cents"], 60_000)

    def test_negative_correction_after_partial_pay_creates_claimback(self) -> None:
        # 100t -> 50t：F1 应分 300000 -> 150000；已付 250000，另 50000 暂缓。
        # STALE 期收回暂缓 50000 后，实际应退回 = 250000-150000 = 100000。
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.pay(pid, "F1", 250_000, "PAY1")
        L.withhold(pid, "F1", 50_000, "暂缓")
        L.record_evidence("E2", pid, "P1", "M1", 1, 50_000, "u2", "s")
        self.assertEqual(L._latest_allocation(pid).state, AllocationState.STALE)
        L.release_withheld(pid, "F1", 50_000, "REC", reclaim=True)
        L.confirm_allocation(pid)
        st = L.statement(pid, "F1")
        self.assertEqual(st["outstanding_cents"], -100_000)
        self.assertEqual(st["claimback_due_cents"], 100_000)
        self.assertEqual(st["retained_pending_cents"], 0)
        L.claim_back(pid, "F1", 100_000, "CB")
        self.assertEqual(L.statement(pid, "F1")["outstanding_cents"], 0)

    def test_retained_exceeding_delta_is_refunded_via_trueup(self) -> None:
        # 100t -> 90t 小幅下调：F1 300000 -> 270000；已付 240000、暂缓 60000。
        # STALE 期收回暂缓 60000；TRUE_UP = -30000 + 60000 = +30000（项目补付）。
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.pay(pid, "F1", 240_000, "PAY1")
        L.withhold(pid, "F1", 60_000, "暂缓")
        L.record_evidence("E2", pid, "P1", "M1", 1, 90_000, "u2", "s")
        L.release_withheld(pid, "F1", 60_000, "REC", reclaim=True)
        L.confirm_allocation(pid)
        st = L.statement(pid, "F1")
        self.assertEqual(st["outstanding_cents"], 30_000)
        self.assertEqual(st["claimback_due_cents"], 0)
        last = st["flows"][-1]
        self.assertEqual(last["kind"], "true_up")
        self.assertEqual(last["amount_cents"], 30_000)
        self.assertEqual(last["ref"]["retained_settled_cents"], 60_000)

    def test_stale_blocks_payment_until_new_version_confirmed(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.record_evidence("E2", pid, "P1", "M1", 1, 80_000, "u2", "s")
        with self.assertRaises(LedgerError):
            L.pay(pid, "O1", 100, "X")
        L.confirm_allocation(pid)
        L.pay(pid, "O1", 10_000, "PAY2")
        self.assertEqual(L.statement(pid, "O1")["outstanding_cents"], 110_000)


class ShareBoundaryTest(unittest.TestCase):
    def test_share_change_only_affects_new_versions(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        v1_hash = L.allocation_versions(pid)[0]["allocation_hash"]
        L.set_share("P1", "O1", 2000, "2026-04-01")
        L.set_share("P1", "C1", 2000, "2026-04-01")
        L.confirm_allocation(pid)
        st_o = L.statement(pid, "O1")
        self.assertEqual(st_o["confirmed_total_cents"], 100_000)  # 500000×20%
        true_up = [f for f in st_o["flows"] if f["kind"] == "true_up"]
        self.assertEqual(true_up[0]["amount_cents"], -50_000)
        self.assertEqual(L.allocation_versions(pid)[0]["allocation_hash"], v1_hash)

    def test_boundary_add_plot_extends_distributable(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.register_plot("P2", "二场")
        L.set_boundary("P2", True, "50", "2026-05-01")
        for party_id, bps in [("F1", 6000), ("O1", 3000), ("C1", 1000)]:
            L.set_share("P2", party_id, bps, "2026-05-01")
        L.register_receipt("R2", "v", "t", "c2")
        L.record_evidence(
            "E2", pid, "P2", "M1", 1, 40_000, "u2", "s", receipt_id="R2"
        )
        L.confirm_allocation(pid)
        summary = L.period_summary(pid)
        self.assertEqual(summary["distributable_cents"], 700_000)  # 140t×50
        self.assertEqual(summary["tco2_kg"], 140_000)

    def test_share_rule_before_period_start_is_used(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        st = L.statement(pid, "F1")
        self.assertEqual(st["confirmed_total_cents"], 300_000)


class ReceiptDisputeTest(unittest.TestCase):
    def test_duplicate_receipt_rejected(self) -> None:
        L, pid = build_ledger()
        L.register_receipt("R1", "v", "t", "c")
        L.record_evidence("E1", pid, "P1", "M1", 1, 100_000, "u", "s", receipt_id="R1")
        with self.assertRaises(LedgerError):
            L.record_evidence(
                "E2", pid, "P1", "M1", 1, 100_000, "u2", "s", receipt_id="R1"
            )

    def test_outward_freeze_blocks_payment(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.open_dispute("D1", pid, "F1", 50_000, DisputeDirection.OUTWARD, "异议")
        self.assertEqual(L.statement(pid, "F1")["payable_cents"], 250_000)
        with self.assertRaises(LedgerError):
            L.pay(pid, "F1", 250_001, "X")
        L.pay(pid, "F1", 250_000, "P")
        L.resolve_dispute("D1", "维持原判")
        L.pay(pid, "F1", 50_000, "P2")
        self.assertEqual(L.statement(pid, "F1")["outstanding_cents"], 0)

    def test_inward_freeze_blocks_claimback(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.pay(pid, "F1", 300_000, "P")
        L.record_evidence("E2", pid, "P1", "M1", 1, 50_000, "u2", "s")
        L.confirm_allocation(pid)
        self.assertEqual(L.statement(pid, "F1")["claimback_due_cents"], 150_000)
        L.open_dispute("D2", pid, "F1", 150_000, DisputeDirection.INWARD, "监测异议")
        with self.assertRaises(LedgerError):
            L.claim_back(pid, "F1", 150_000, "C")
        L.resolve_dispute("D2", "复核维持")
        L.claim_back(pid, "F1", 150_000, "C")
        self.assertEqual(L.statement(pid, "F1")["outstanding_cents"], 0)

    def test_withhold_cannot_exceed_available(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.withhold(pid, "F1", 300_000, "w")
        with self.assertRaises(LedgerError):
            L.withhold(pid, "F1", 1, "w2")
        with self.assertRaises(LedgerError):
            L.pay(pid, "F1", 1, "p")

    def test_reclaim_only_allowed_when_stale(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        L.withhold(pid, "F1", 50_000, "w")
        with self.assertRaises(LedgerError):
            L.release_withheld(pid, "F1", 50_000, "R", reclaim=True)
        # 非 STALE 期可以解除并支付
        L.release_withheld(pid, "F1", 50_000, "R", reclaim=False)
        self.assertEqual(L.statement(pid, "F1")["outstanding_cents"], 250_000)


class PersistenceIdempotencyTest(unittest.TestCase):
    def test_replay_from_persisted_events_matches(self) -> None:
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            L, pid = build_ledger(EventStore(path))
            seed_period(L, 100_000)
            L.pay(pid, "F1", 100_000, "P1")
            L.withhold(pid, "F1", 50_000, "w")
            L.record_evidence("E2", pid, "P1", "M1", 1, 80_000, "u2", "s")
            L.release_withheld(pid, "F1", 50_000, "R", reclaim=True)
            L.confirm_allocation(pid)
            L.open_dispute("D", pid, "O1", 10_000, DisputeDirection.OUTWARD, "x")
            expected = {
                p: L.statement(pid, p)["outstanding_cents"] for p in ("F1", "O1", "C1")
            }
            expected_flows = len(L.flows)
            head = L.store.head_hash()

            L2 = Ledger(EventStore(path))
            self.assertEqual(L2.store.head_hash(), head)
            self.assertEqual(L2.store.verify_chain(), [])
            self.assertEqual(len(L2.flows), expected_flows)
            for party_id, outstanding in expected.items():
                self.assertEqual(
                    L2.statement(pid, party_id)["outstanding_cents"], outstanding
                )
            # basis 也必须能从重放中恢复
            self.assertEqual(len(L2.statement(pid, "F1")["basis"]), 1)
        finally:
            os.remove(path)

    def test_command_idempotency(self) -> None:
        L, pid = build_ledger()
        seed_period(L, 100_000)
        e1 = L.pay(pid, "O1", 10_000, "REF", idem_key="opay")
        e2 = L.pay(pid, "O1", 10_000, "REF", idem_key="opay")
        self.assertEqual(e1, e2)
        self.assertEqual(L.statement(pid, "O1")["outstanding_cents"], 140_000)


if __name__ == "__main__":
    unittest.main()
