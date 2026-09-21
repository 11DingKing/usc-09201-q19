"""HTTP API 端到端测试（内存账本，线程内起服务）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service.main import create_server


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        role: str | None = None,
        party: str | None = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        if role:
            req.add_header("X-Role", role)
        if party:
            req.add_header("X-Party-Id", party)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)


def setup_period(client: ApiClient) -> str:
    admin_posts = [
        ("/api/methods", {"method_id": "M1", "version": 1, "name": "m",
                          "parameters_hash": "h"}),
        ("/api/parties", {"party_id": "F1", "party_type": "forest_farmer",
                          "name": "林农甲", "id_number": "ID-F1"}),
        ("/api/parties", {"party_id": "O1", "party_type": "operator", "name": "运营方"}),
        ("/api/parties", {"party_id": "C1", "party_type": "collective", "name": "村集体"}),
        ("/api/plots", {"plot_id": "P1", "name": "山场"}),
        ("/api/boundaries", {"plot_id": "P1", "included": True, "area_ha": "100",
                             "effective_from": "2026-01-01"}),
        ("/api/shares", {"plot_id": "P1", "party_id": "F1", "share_bps": 6000,
                         "effective_from": "2026-01-01"}),
        ("/api/shares", {"plot_id": "P1", "party_id": "O1", "share_bps": 3000,
                         "effective_from": "2026-01-01"}),
        ("/api/shares", {"plot_id": "P1", "party_id": "C1", "share_bps": 1000,
                         "effective_from": "2026-01-01"}),
        ("/api/periods", {"period_id": "P", "name": "2026H1", "start_date": "2026-01-01",
                          "end_date": "2026-06-30", "price_cents_per_t": 5000}),
        ("/api/receipts", {"receipt_id": "R1", "verifier": "核证方",
                           "issued_at": "2026-07-01", "content_hash": "c"}),
        ("/api/evidence", {"evidence_id": "E1", "period_id": "P", "plot_id": "P1",
                           "method_id": "M1", "method_version": 1, "tco2_kg": 100_000,
                           "source_uri": "secret://loc", "submitted_by": "监测方",
                           "receipt_id": "R1"}),
    ]
    for path, body in admin_posts:
        status, _ = client.request("POST", path, body, role="admin")
        assert status == 201, (path, status)
    status, summary = client.request("POST", "/api/periods/P/confirm", {}, role="admin")
    assert status == 201
    return summary["allocation_hash"]


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, _ = create_server("127.0.0.1", 0, path=None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = ApiClient(f"http://{host}:{port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health(self) -> None:
        status, body = self.client.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_full_settlement_flow_over_http(self) -> None:
        anchor = setup_period(self.client)

        # 办公室汇总
        status, summary = self.client.request("GET", "/api/periods/P/summary", role="admin")
        self.assertEqual(status, 200)
        self.assertEqual(summary["distributable_cents"], 500_000)

        # 首期部分支付
        status, _ = self.client.request(
            "POST", "/api/periods/P/pay",
            {"party_id": "F1", "amount_cents": 100_000, "reference": "PAY1"},
            role="admin",
        )
        self.assertEqual(status, 201)

        # 监测更正
        status, _ = self.client.request(
            "POST", "/api/evidence",
            {"evidence_id": "E2", "period_id": "P", "plot_id": "P1",
             "method_id": "M1", "method_version": 1, "tco2_kg": 80_000,
             "source_uri": "secret://loc2", "submitted_by": "监测方"},
            role="monitor",
        )
        self.assertEqual(status, 201)

        # STALE 期间支付被业务规则拒绝（409）
        status, err = self.client.request(
            "POST", "/api/periods/P/pay",
            {"party_id": "O1", "amount_cents": 100, "reference": "X"},
            role="admin",
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "ledger_rule_violation")

        # 确认追补版本
        status, v2 = self.client.request("POST", "/api/periods/P/confirm", {}, role="admin")
        self.assertEqual(status, 201)
        self.assertEqual(v2["allocation_version"], 2)
        self.assertEqual(v2["distributable_cents"], 400_000)

        # 林农下载本方账单：能看到哈希与流水，地点被脱敏
        status, st = self.client.request(
            "GET", "/api/periods/P/statements/F1", role="forest_farmer", party="F1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(st["allocation_hash"], v2["allocation_hash"])
        self.assertEqual(st["basis"][0]["evidence"]["source_uri"], "***REDACTED***")
        kinds = [f["kind"] for f in st["flows"]]
        self.assertEqual(kinds, ["confirmation", "payment", "true_up"])

        # 林农不能下载别人账单（403）
        status, _ = self.client.request(
            "GET", "/api/periods/P/statements/O1", role="forest_farmer", party="F1"
        )
        self.assertEqual(status, 403)

        # 监测机构不能看财务（403）
        status, _ = self.client.request("GET", "/api/periods/P/summary", role="monitor")
        self.assertEqual(status, 403)

        # 公众存证核验
        status, verification = self.client.request(
            "GET", f"/api/anchor/{anchor}", role="public"
        )
        self.assertEqual(status, 200)
        self.assertTrue(verification["found"])

        # 历史版本
        status, versions = self.client.request(
            "GET", "/api/periods/P/versions", role="auditor"
        )
        self.assertEqual(status, 200)
        self.assertEqual([v["version"] for v in versions["versions"]], [1, 2])
        self.assertEqual(versions["versions"][0]["state"], "stale")

        # 事件链审计
        status, audit = self.client.request("GET", "/api/events", role="admin")
        self.assertEqual(status, 200)
        self.assertEqual(audit["problems"], [])

    def test_duplicate_receipt_rejected_over_http(self) -> None:
        setup_period(self.client)
        status, err = self.client.request(
            "POST", "/api/evidence",
            {"evidence_id": "EDUP", "period_id": "P", "plot_id": "P1",
             "method_id": "M1", "method_version": 1, "tco2_kg": 100_000,
             "source_uri": "u", "submitted_by": "s", "receipt_id": "R1"},
            role="monitor",
        )
        self.assertEqual(status, 409)

    def test_idempotent_payment_over_http(self) -> None:
        setup_period(self.client)
        body = {"party_id": "O1", "amount_cents": 10_000, "reference": "REF",
                "idem_key": "pay-1"}
        s1, b1 = self.client.request("POST", "/api/periods/P/pay", body, role="admin")
        s2, b2 = self.client.request("POST", "/api/periods/P/pay", body, role="admin")
        self.assertEqual((s1, s1), (201, s2))
        self.assertEqual(b1, b2)

    def test_dispute_blocks_payment_over_http(self) -> None:
        setup_period(self.client)
        status, _ = self.client.request(
            "POST", "/api/disputes",
            {"dispute_id": "D1", "period_id": "P", "party_id": "F1",
             "amount_cents": 300_000, "direction": "outward", "reason": "异议"},
            role="admin",
        )
        self.assertEqual(status, 201)
        status, _ = self.client.request(
            "POST", "/api/periods/P/pay",
            {"party_id": "F1", "amount_cents": 1, "reference": "X"},
            role="admin",
        )
        self.assertEqual(status, 409)
        status, _ = self.client.request(
            "POST", "/api/disputes/D1/resolve", {"resolution": "维持"}, role="admin"
        )
        self.assertEqual(status, 201)

    def test_unknown_route_and_bad_json(self) -> None:
        status, _ = self.client.request("GET", "/nope")
        self.assertEqual(status, 404)
        req = urllib.request.Request(
            self.client.base + "/api/periods",
            data=b"{not json", method="POST",
            headers={"Content-Type": "application/json", "X-Role": "admin"},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
