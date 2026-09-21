"""每期锁定规则的校验测试。"""

from __future__ import annotations

import unittest

from service.ledger.engine import LedgerError
from tests._fixtures import PERIOD, build_confirmed_engine


class LockdownTest(unittest.TestCase):
    def test_confirmation_locks_versions_and_amounts(self) -> None:
        eng = build_confirmed_engine()
        bundle = eng.period_bundle(PERIOD)
        locked = bundle["locked"]
        self.assertEqual(locked["method"]["version"], "M-1")
        self.assertEqual(locked["baseline"]["version"], "BL-1")
        self.assertEqual(locked["boundary"]["version"], "BND-1")
        self.assertEqual(locked["share_rule"]["version"], "SR-1")
        self.assertEqual(locked["monitoring"]["event_id"], "mon1")
        self.assertEqual(bundle["allocatable_amount"], "10000.00")
        gross_total = sum(
            int(row["gross"].replace(".", "")) for row in bundle["lines"]
        )
        self.assertEqual(gross_total, 1_000_000)

    def test_remainder_cents_allocated_exactly(self) -> None:
        # 100.01 元按 3 份分，尾差 1 分必须分完。
        from service.ledger.engine import LedgerEngine
        from service.ledger.store import EventStore

        engine = LedgerEngine(EventStore())
        a = lambda t, eid, p: engine.append(t, eid, p, actor="office")  # noqa: E731
        a("method.registered", "m", {"version": "M", "doc_hash": "h"})
        a("baseline.registered", "b", {"version": "B", "doc_hash": "h"})
        a("boundary.changed", "bd", {"version": "D", "area_mu": 1, "doc_hash": "h"})
        for i in range(3):
            a("participant.registered", f"p{i}", {"participant_id": f"p{i}", "kind": "forest_farmer"})
        a("period.opened", "per", {"period_id": "P", "boundary_version": "D"})
        a("share.rule.locked", "sr", {"period_id": "P", "version": "S", "lines": [
            {"participant_id": "p0", "share": "0.3333333333"},
            {"participant_id": "p1", "share": "0.3333333333"},
            {"participant_id": "p2", "share": "0.3333333334"},
        ]})
        engine.append("monitoring.submitted", "mon", {
            "period_id": "P", "allocatable_amount": "100.01",
            "method_version": "M", "baseline_version": "B", "boundary_version": "D",
            "evidence": [{"ref": "e", "sha256": "x" * 64}],
        }, actor="monitoring_body")
        engine.append("verification.receipt.recorded", "rcp", {
            "period_id": "P", "receipt_no": "R", "doc_hash": "y" * 64,
        }, actor="verification_body")
        suggested = engine._allocate(10001, [
            {"participant_id": "p0", "share": "0.3333333333"},
            {"participant_id": "p1", "share": "0.3333333333"},
            {"participant_id": "p2", "share": "0.3333333334"},
        ])
        self.assertEqual(sum(suggested.values()), 10001)

    def test_shares_must_sum_to_one(self) -> None:
        eng = build_confirmed_engine()
        eng.append("period.opened", "per2", {"period_id": "2026", "boundary_version": "BND-1"}, actor="office")
        with self.assertRaises(LedgerError):
            eng.append("share.rule.locked", "sr-bad", {"period_id": "2026", "version": "SR-X", "lines": [
                {"participant_id": "C", "share": "0.5"},
                {"participant_id": "FA", "share": "0.4"},
            ]}, actor="office")

    def test_confirmation_requires_receipt(self) -> None:
        from service.ledger.engine import LedgerEngine
        from service.ledger.store import EventStore

        engine = LedgerEngine(EventStore())
        a = lambda t, eid, p: engine.append(t, eid, p, actor="office")  # noqa: E731
        a("method.registered", "m", {"version": "M", "doc_hash": "h"})
        a("baseline.registered", "b", {"version": "B", "doc_hash": "h"})
        a("boundary.changed", "bd", {"version": "D", "area_mu": 1, "doc_hash": "h"})
        a("participant.registered", "p1", {"participant_id": "C", "kind": "village_collective"})
        a("period.opened", "per", {"period_id": "P", "boundary_version": "D"})
        a("share.rule.locked", "sr", {"period_id": "P", "version": "S",
                                      "lines": [{"participant_id": "C", "share": "1"}]})
        engine.append("monitoring.submitted", "mon", {
            "period_id": "P", "allocatable_amount": "10.00",
            "method_version": "M", "baseline_version": "B", "boundary_version": "D",
            "evidence": [{"ref": "e", "sha256": "x" * 64}],
        }, actor="monitoring_body")
        with self.assertRaises(LedgerError):
            a("distribution.confirmed", "conf", {
                "period_id": "P", "monitoring_ref": "mon",
                "method_version": "M", "baseline_version": "B", "boundary_version": "D",
                "share_rule_version": "S", "allocatable_amount": "10.00",
                "lines": [{"participant_id": "C", "share": "1", "amount": "10.00"}],
            })

    def test_confirmation_rejects_amount_mismatch(self) -> None:
        eng = build_confirmed_engine()
        # 重开一期并尝试用错误金额确认。
        eng.append("period.opened", "per2", {"period_id": "2026", "boundary_version": "BND-1"}, actor="office")
        eng.append("share.rule.locked", "sr2", {"period_id": "2026", "version": "SR-2", "lines": [
            {"participant_id": "C", "share": "1"}]}, actor="office")
        eng.append("monitoring.submitted", "mon26", {
            "period_id": "2026", "allocatable_amount": "100.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e", "sha256": "z" * 64}],
        }, actor="monitoring_body")
        eng.append("verification.receipt.recorded", "rcp26", {
            "period_id": "2026", "receipt_no": "VR-2", "doc_hash": "y" * 64,
        }, actor="verification_body")
        with self.assertRaises(LedgerError):
            eng.append("distribution.confirmed", "conf26", {
                "period_id": "2026", "monitoring_ref": "mon26",
                "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
                "share_rule_version": "SR-2", "allocatable_amount": "99.00",
                "lines": [{"participant_id": "C", "share": "1", "amount": "99.00"}],
            }, actor="office")

    def test_history_never_recomputed_after_confirmation(self) -> None:
        """确认后：历史确认事件哈希不变；份额规则禁止在该期新增版本。"""

        eng = build_confirmed_engine()
        before = eng.confirmations[PERIOD]["event_hash"]
        with self.assertRaises(LedgerError):
            eng.append("share.rule.locked", "sr2", {"period_id": PERIOD, "version": "SR-2", "lines": [
                {"participant_id": "C", "share": "1"}]}, actor="office")
        with self.assertRaises(LedgerError):
            eng.append("monitoring.submitted", "mon-again", {
                "period_id": PERIOD, "allocatable_amount": "1.00",
                "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
                "evidence": [{"ref": "e", "sha256": "q" * 64}],
            }, actor="monitoring_body")
        self.assertEqual(eng.confirmations[PERIOD]["event_hash"], before)


if __name__ == "__main__":
    unittest.main()
