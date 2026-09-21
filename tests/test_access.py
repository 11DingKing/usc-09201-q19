"""分角色访问与敏感信息脱敏测试。"""

from __future__ import annotations

import unittest

from service.access import AccessDenied, Actor, redact_party, redact_statement
from service.app import AppService
from service.models import PartyType, Role
from service.store import EventStore


def app_with_period() -> AppService:
    app = AppService(EventStore())
    L = app.ledger
    L.register_method("M1", 1, "方法学", "h1")
    L.register_party(
        "F1", PartyType.FOREST_FARMER, "林农甲",
        contact="13800000001", id_number="350121197001011234", account_ref="ACC-F1",
    )
    L.register_party("O1", PartyType.OPERATOR, "运营公司", account_ref="ACC-O1")
    L.register_party("C1", PartyType.COLLECTIVE, "村集体")
    L.register_plot("P1", "山场")
    L.set_boundary("P1", True, "100", "2026-01-01")
    L.set_share("P1", "F1", 6000, "2026-01-01")
    L.set_share("P1", "O1", 3000, "2026-01-01")
    L.set_share("P1", "C1", 1000, "2026-01-01")
    L.open_period("P", "2026H1", "2026-01-01", "2026-06-30", 5000)
    L.register_receipt("R1", "核证方", "2026-07-01", "c")
    L.record_evidence("E1", "P", "P1", "M1", 1, 100_000, "secret://gps/raw", "监测方", receipt_id="R1")
    L.confirm_allocation("P")
    return app


class PermissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = app_with_period()

    def test_farmer_can_read_own_statement_only(self) -> None:
        farmer = Actor(Role.FARMER, "F1")
        st = self.app.statement(farmer, "P", "F1")
        self.assertEqual(st["party_id"], "F1")
        with self.assertRaises(AccessDenied):
            self.app.statement(farmer, "P", "O1")

    def test_monitor_cannot_see_finances(self) -> None:
        monitor = Actor(Role.MONITOR)
        with self.assertRaises(AccessDenied):
            self.app.period_summary(monitor, "P")
        with self.assertRaises(AccessDenied):
            self.app.statement(monitor, "P", "F1")
        # 但可以看证据
        ev = self.app.list_evidence(monitor, "P")
        self.assertEqual(len(ev["evidence"]), 1)

    def test_monitor_cannot_confirm_or_pay(self) -> None:
        monitor = Actor(Role.MONITOR)
        with self.assertRaises(AccessDenied):
            self.app.confirm_allocation(monitor, "P", {})
        with self.assertRaises(AccessDenied):
            self.app.pay(monitor, "P", {"party_id": "F1", "amount_cents": 1, "reference": "x"})

    def test_public_can_only_verify_anchor(self) -> None:
        public = Actor(Role.PUBLIC)
        h = self.app.ledger.period_summary("P")["allocation_hash"]
        result = self.app.verify_anchor(public, h)
        self.assertTrue(result["found"])
        self.assertNotIn("parties", result)
        with self.assertRaises(AccessDenied):
            self.app.period_summary(public, "P")
        self.assertFalse(self.app.verify_anchor(public, "nope")["found"])

    def test_auditor_sees_finances_but_not_id_number(self) -> None:
        auditor = Actor(Role.AUDITOR)
        parties = self.app.list_parties(auditor)["parties"]
        f1 = next(p for p in parties if p["party_id"] == "F1")
        self.assertNotIn("350121197001011234", str(f1))
        self.assertIn("***", f1["id_number"])
        # 财务可见
        self.assertEqual(
            self.app.period_summary(auditor, "P")["distributable_cents"], 500_000
        )
        events = self.app.audit_events(auditor)["events"]
        self.assertNotIn("350121197001011234", str(events))

    def test_admin_sees_id_number(self) -> None:
        admin = Actor(Role.ADMIN)
        f1 = next(p for p in self.app.list_parties(admin)["parties"] if p["party_id"] == "F1")
        self.assertEqual(f1["id_number"], "350121197001011234")


class RedactionTest(unittest.TestCase):
    def test_location_uri_redacted_for_party(self) -> None:
        app = app_with_period()
        farmer = Actor(Role.FARMER, "F1")
        st = app.statement(farmer, "P", "F1")
        self.assertEqual(st["basis"][0]["evidence"]["source_uri"], "***REDACTED***")
        admin = Actor(Role.ADMIN)
        st_admin = app.statement(admin, "P", "F1")
        self.assertEqual(st_admin["basis"][0]["evidence"]["source_uri"], "secret://gps/raw")

    def test_redact_party_helper(self) -> None:
        party = {"party_id": "F1", "contact": "13800000001", "id_number": "ABCDEF123456"}
        self.assertNotIn("id_number", redact_party(party, Actor(Role.FARMER, "F1")))
        masked = redact_party(party, Actor(Role.AUDITOR))["id_number"]
        self.assertIn("*", masked)
        self.assertEqual(redact_party(party, Actor(Role.ADMIN))["id_number"], "ABCDEF123456")

    def test_statement_hashes_identical_across_roles(self) -> None:
        app = app_with_period()
        h_admin = app.statement(Actor(Role.ADMIN), "P", "F1")["allocation_hash"]
        h_farmer = app.statement(Actor(Role.FARMER, "F1"), "P", "F1")["allocation_hash"]
        h_auditor = app.statement(Actor(Role.AUDITOR), "P", "F1")["allocation_hash"]
        self.assertEqual(len({h_admin, h_farmer, h_auditor}), 1)

    def test_redact_statement_pure_shape(self) -> None:
        statement = {"basis": [{"evidence": {"source_uri": "x"}}]}
        out = redact_statement(statement, Actor(Role.ADMIN))
        self.assertEqual(out["basis"][0]["evidence"]["source_uri"], "x")


if __name__ == "__main__":
    unittest.main()
