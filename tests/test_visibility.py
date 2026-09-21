"""分角色可见性与依据包一致性测试。"""

from __future__ import annotations

import unittest

from service.ledger.visibility import (
    REDACTED,
    view_participant_pack,
    view_period_bundle,
)
from tests._fixtures import PERIOD, build_confirmed_engine


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = build_confirmed_engine()
        self.eng.append("payment.made", "pay1", {
            "period_id": PERIOD, "participant_id": "FA", "payment_id": "P1",
            "amount": "3000.00", "bank_account": "6228-4800", "payment_ref": "REF-1",
        }, actor="office")

    def test_all_roles_share_same_root_and_hashes(self) -> None:
        bundle = self.eng.period_bundle(PERIOD)
        views = {
            role: view_period_bundle(bundle, role)
            for role in ("office", "baseline_body", "monitoring_body", "verification_body")
        }
        roots = {v["ledger_root_hash"] for v in views.values()}
        self.assertEqual(roots, {self.eng.store.root_hash})
        confirms = {v["confirmation_event_hash"] for v in views.values()}
        self.assertEqual(len(confirms), 1)

    def test_sensitive_payment_fields_redacted_for_institutions(self) -> None:
        bundle = self.eng.period_bundle(PERIOD)
        for role in ("baseline_body", "monitoring_body", "verification_body"):
            view = view_period_bundle(bundle, role)
            pay = next(p for p in view["flows"]["payments"] if p["payment_id"] == "P1")
            self.assertEqual(pay["bank_account"], REDACTED)
            self.assertEqual(pay["payment_ref"], REDACTED)
        office_view = view_period_bundle(bundle, "office")
        pay = office_view["flows"]["payments"][0]
        self.assertEqual(pay["bank_account"], "6228-4800")

    def test_evidence_detail_only_for_monitoring_body(self) -> None:
        bundle = self.eng.period_bundle(PERIOD)
        baseline = view_period_bundle(bundle, "baseline_body")
        self.assertEqual(baseline["locked"]["monitoring"]["evidence"][0]["ref"], REDACTED)
        self.assertTrue(baseline["locked"]["monitoring"]["evidence"][0]["sha256"])
        monitoring = view_period_bundle(bundle, "monitoring_body")
        self.assertEqual(monitoring["locked"]["monitoring"]["evidence"][0]["ref"], "evidence-1.pdf")

    def test_participant_pack_contains_only_own_amounts(self) -> None:
        pack = view_participant_pack(
            self.eng.participant_evidence_pack("FA"), requester_role="participant"
        )
        self.assertEqual(pack["ledger_root_hash"], self.eng.store.root_hash)
        period = pack["periods"][0]
        self.assertEqual(period["own_line"]["participant_id"], "FA")
        self.assertEqual(period["own_line"]["gross"], "3500.00")
        # 本方流水出现本方支付；不出现他人参与方 id。
        all_rows = [row for key, rows in period["own_flows"].items() for row in rows]
        for row in all_rows:
            if row["kind"] != "confirmation":
                self.assertEqual(row.get("participant_id"), "FA")
        # 锁定版本完整，可独立核验。
        self.assertEqual(period["locked"]["method_version"], "M-1")
        self.assertEqual(period["locked"]["baseline_version"], "BL-1")
        self.assertTrue(period["locked"]["monitoring_event_hash"])
        self.assertTrue(period["confirmation_event_hash"])

    def test_participant_pack_shows_correction_chain(self) -> None:
        self.eng.append("monitoring.corrected", "mon2", {
            "period_id": PERIOD, "allocatable_amount": "9000.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e2", "sha256": "b" * 64}],
            "reason": "复测调减",
        }, actor="monitoring_body")
        pack = self.eng.participant_evidence_pack("C")
        locked = pack["periods"][0]["locked"]
        ids = [row["event_id"] for row in locked["monitoring_history"]]
        self.assertEqual(ids, ["mon1", "mon2"])


if __name__ == "__main__":
    unittest.main()
