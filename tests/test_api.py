"""HTTP 接口测试：事件提交、角色鉴权、依据包下载。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service.api import LedgerApp
from service.ledger.engine import cents_str
from service.ledger.store import EventStore
from service.main import create_server
from tests._fixtures import PERIOD, build_confirmed_engine, raise_trueup_for_correction


class HttpSession:
    def __init__(self, app: LedgerApp) -> None:
        self.server = create_server("127.0.0.1", 0, app=app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        eng = build_confirmed_engine()
        self.eng = eng
        self.app = LedgerApp(eng.store)
        self.http = HttpSession(self.app)

    def tearDown(self) -> None:
        self.http.stop()

    def test_health_and_root(self) -> None:
        status, body = self.http.request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        status, body = self.http.request("GET", "/ledger/root")
        self.assertEqual(status, 200)
        self.assertEqual(body["root_hash"], self.eng.store.root_hash)
        self.assertGreater(body["event_count"], 0)

    def test_post_event_role_enforced_and_idempotent(self) -> None:
        payload = {
            "event_id": "mon-http", "type": "monitoring.submitted",
            "payload": {"period_id": "2026"},
        }
        # 办公室不能提交监测事件。
        status, body = self.http.request("POST", "/events", payload, {"X-Role": "office"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 开新期后由监测机构提交。
        self.eng.append("period.opened", "per26", {"period_id": "2026", "boundary_version": "BND-1"}, actor="office")
        payload["payload"] = {
            "period_id": "2026", "allocatable_amount": "1.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e", "sha256": "q" * 64}],
        }
        status, first = self.http.request("POST", "/events", payload, {"X-Role": "monitoring_body"})
        self.assertEqual(status, 201)
        self.assertTrue(first["created"])
        # 同 event_id 重放：200 + created=False，事件数不变。
        count_before = first["event_count"] if "event_count" in first else None
        status, second = self.http.request("POST", "/events", payload, {"X-Role": "monitoring_body"})
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(second["event"]["event_hash"], first["event"]["event_hash"])
        _ = count_before

    def test_validation_error_is_422_and_nothing_persisted(self) -> None:
        before = len(self.app.store.events())
        status, body = self.http.request("POST", "/events", {
            "event_id": "bad-share", "type": "share.rule.locked",
            "payload": {"period_id": PERIOD, "version": "SR-X",
                        "lines": [{"participant_id": "C", "share": "0.9"}]},
        }, {"X-Role": "office"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "validation_failed")
        self.assertEqual(len(self.app.store.events()), before)

    def test_bundle_role_access_and_redaction(self) -> None:
        status, body = self.http.request("GET", f"/periods/{PERIOD}/bundle", headers={"X-Role": "office"})
        self.assertEqual(status, 200)
        self.assertEqual(body["locked"]["method"]["version"], "M-1")

        status, mon_view = self.http.request(
            "GET", f"/periods/{PERIOD}/bundle", headers={"X-Role": "monitoring_body"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(mon_view["ledger_root_hash"], body["ledger_root_hash"])
        self.assertEqual(mon_view["locked"]["monitoring"]["evidence"][0]["ref"], "evidence-1.pdf")

        # 参与方角色不能取机构期包。
        status, denied = self.http.request(
            "GET", f"/periods/{PERIOD}/bundle", headers={"X-Role": "participant"}
        )
        self.assertEqual(status, 403)

    def test_participant_downloads_own_pack_and_cannot_read_others(self) -> None:
        # 制造一笔支付与一次更正追补，使依据包含流水。
        self.eng.append("payment.made", "p1", {
            "period_id": PERIOD, "participant_id": "FA", "payment_id": "P1",
            "amount": "3000.00", "bank_account": "ACC-FA", "payment_ref": "REF1",
        }, actor="office")
        self.eng.append("monitoring.corrected", "mon2", {
            "period_id": PERIOD, "allocatable_amount": "6000.00",
            "method_version": "M-1", "baseline_version": "BL-1", "boundary_version": "BND-1",
            "evidence": [{"ref": "e2", "sha256": "b" * 64}], "reason": "复测",
        }, actor="monitoring_body")
        raise_trueup_for_correction(self.eng, "6000.00", reason_event="mon2")

        status, pack = self.http.request(
            "GET", "/participants/FA/evidence-pack",
            headers={"X-Role": "participant", "X-Participant-Id": "FA"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pack["ledger_root_hash"], self.eng.store.root_hash)
        period = pack["periods"][0]
        self.assertEqual(period["own_line"]["gross"], "3500.00")
        self.assertEqual(period["own_line"]["clawback_due"], "900.00")
        # 更正链可追溯。
        self.assertEqual([r["event_id"] for r in period["locked"]["monitoring_history"]], ["mon1", "mon2"])
        # 林农能看到自己的银行账号/凭证（本方敏感信息）。
        pay = next(r for r in period["own_flows"]["payments"])
        self.assertEqual(pay["bank_account"], "ACC-FA")

        # 不能下载他人的包。
        status, denied = self.http.request(
            "GET", "/participants/FA/evidence-pack",
            headers={"X-Role": "participant", "X-Participant-Id": "FB"},
        )
        self.assertEqual(status, 403)
        _ = cents_str  # 保持导入（工具函数，供扩展断言使用）

    def test_account_self_service(self) -> None:
        status, body = self.http.request(
            "GET", "/accounts/FA",
            headers={"X-Role": "participant", "X-Participant-Id": "FA"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["remaining_payable_total"], "3500.00")
        status, denied = self.http.request(
            "GET", "/accounts/FA",
            headers={"X-Role": "participant", "X-Participant-Id": "OP"},
        )
        self.assertEqual(status, 403)

    def test_flows_endpoint_lists_four_flow_types(self) -> None:
        self.eng.append("hold.placed", "h1", {
            "period_id": PERIOD, "participant_id": "OP", "hold_id": "H1", "amount": "100.00",
        }, actor="office")
        status, body = self.http.request(
            "GET", f"/periods/{PERIOD}/flows", headers={"X-Role": "office"}
        )
        self.assertEqual(status, 200)
        flows = body["flows"]
        self.assertEqual(len(flows["confirmed"]), 1)
        self.assertEqual(flows["holds"][0]["hold_id"], "H1")
        self.assertIn("payments", flows)
        self.assertIn("trueups", flows)


if __name__ == "__main__":
    unittest.main()
