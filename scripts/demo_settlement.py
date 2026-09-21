"""首期结算演示。

故事线（对应 docs/domain.md 的共享账约定）：

1. 方法学机构、基线机构、边界机构分别登记并锁定版本；
2. 村集体/林农/运营方登记，办公室锁定首期共有林地份额规则；
3. 监测机构提交监测证据与可分配量，核证机构出具核证回执；
4. 办公室确认首期分配（确认即锁定，金额到分，合计严格相等）；
5. 演示会先支付部分收益、对运营方一笔暂缓；
6. 监测结果跨期更正（只追加新事件，历史确认哈希不变）；
7. 办公室按“确认时份额”生成每方追补：补付或返还责任；
8. 林农甲超付部分形成追补责任，凭返还流水结清；
9. 林农乙的争议通过冻结/解冻两个事件处理，冻结期资金动作全部被阻；
10. 每方下载依据包：哈希根相同、版本相同、敏感字段按角色裁剪。

运行：python3 scripts/demo_settlement.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.ledger.engine import LedgerEngine, cents_str
from service.ledger.store import EventStore
from service.ledger.visibility import view_period_bundle


def main() -> None:
    eng = LedgerEngine(EventStore())
    office = lambda t, eid, p: eng.append(t, eid, p, actor="office")  # noqa: E731

    print("=" * 68)
    print("1. 版本登记（方法/基线/边界由不同机构维护）")
    print("=" * 68)
    eng.append("method.registered", "method-2023-1", {
        "version": "M-2023-1", "doc_hash": "sha256:method-ccer-v1",
        "title": "林业碳汇方法学 2023-1",
    }, actor="methodology_body")
    eng.append("baseline.registered", "baseline-v1", {
        "version": "BL-V1", "doc_hash": "sha256:baseline-v1",
        "title": "项目基线第一版",
    }, actor="baseline_body")
    eng.append("boundary.changed", "boundary-v1", {
        "version": "BND-V1", "area_mu": 1200,
        "doc_hash": "sha256:boundary-v1", "note": "项目初始边界",
    }, actor="boundary_body")

    for pid, kind, name in [
        ("JT", "village_collective", "某村集体"),
        ("LN-A", "forest_farmer", "林农甲"),
        ("LN-B", "forest_farmer", "林农乙"),
        ("YYF", "operator", "合作运营方"),
    ]:
        office("participant.registered", f"participant-{pid}", {
            "participant_id": pid, "kind": kind, "name": name,
            "bank_account": f"6228****{pid[-3:]}", "id_number": "REDACTED-AT-SOURCE",
        })

    office("period.opened", "period-2025", {
        "period_id": "2025", "title": "2025 年度首期碳收益结算",
        "starts_on": "2025-01-01", "ends_on": "2025-12-31",
        "boundary_version": "BND-V1",
    })
    office("share.rule.locked", "share-2025-v1", {
        "period_id": "2025", "version": "SHARE-2025-V1",
        "lines": [
            {"participant_id": "JT", "share": "0.10"},
            {"participant_id": "LN-A", "share": "0.35"},
            {"participant_id": "LN-B", "share": "0.25"},
            {"participant_id": "YYF", "share": "0.30"},
        ],
    })

    print("方法/基线/边界/参与方/份额规则已锁定")

    print("=" * 68)
    print("2. 监测提交 → 核证回执 → 办公室确认分配")
    print("=" * 68)
    eng.append("monitoring.submitted", "monitoring-2025-v1", {
        "period_id": "2025",
        "allocatable_amount": "10000.00",
        "method_version": "M-2023-1",
        "baseline_version": "BL-V1",
        "boundary_version": "BND-V1",
        "evidence": [
            {"ref": "monitoring/2025/sample-plot-report.pdf", "sha256": "a" * 64},
            {"ref": "monitoring/2025/remote-sensing.tif", "sha256": "c" * 64},
        ],
    }, actor="monitoring_body")
    eng.append("verification.receipt.recorded", "verification-2025-1", {
        "period_id": "2025", "receipt_no": "VR-2025-001",
        "doc_hash": "sha256:" + "v" * 60,
    }, actor="verification_body")
    office("distribution.confirmed", "distribution-2025", {
        "period_id": "2025", "monitoring_ref": "monitoring-2025-v1",
        "method_version": "M-2023-1", "baseline_version": "BL-V1",
        "boundary_version": "BND-V1", "share_rule_version": "SHARE-2025-V1",
        "allocatable_amount": "10000.00",
        "lines": [
            {"participant_id": "JT", "share": "0.10", "amount": "1000.00"},
            {"participant_id": "LN-A", "share": "0.35", "amount": "3500.00"},
            {"participant_id": "LN-B", "share": "0.25", "amount": "2500.00"},
            {"participant_id": "YYF", "share": "0.30", "amount": "3000.00"},
        ],
    })
    conf_hash = eng.confirmations["2025"]["event_hash"]
    print(f"确认事件哈希: {conf_hash}")

    print("=" * 68)
    print("3. 首期演示会：先支付部分收益 + 运营方一笔暂缓")
    print("=" * 68)
    office("payment.made", "pay-ln-a-1", {
        "period_id": "2025", "participant_id": "LN-A", "payment_id": "PAY-0001",
        "amount": "3000.00", "bank_account": "6228***001", "payment_ref": "BANK/2025/0001",
    })
    office("hold.placed", "hold-yyf-1", {
        "period_id": "2025", "participant_id": "YYF", "hold_id": "HOLD-0001",
        "amount": "600.00", "reason": "运营月报待补，暂缓部分收益",
    })
    for pid in ("JT", "LN-A", "LN-B", "YYF"):
        line = eng.line_account("2025", pid)
        print(f"  {pid}: 应得 {line['gross']}  已付 {line['paid_net']}  "
              f"暂缓 {line['holds_active']}  剩余应付 {line['remaining_payable']}")

    print("=" * 68)
    print("4. 跨期监测更正：可分配量 10000.00 → 6000.00（新事件，不改历史）")
    print("=" * 68)
    eng.append("monitoring.corrected", "monitoring-2025-v2", {
        "period_id": "2025",
        "allocatable_amount": "6000.00",
        "method_version": "M-2023-1",
        "baseline_version": "BL-V1",
        "boundary_version": "BND-V1",
        "evidence": [{"ref": "monitoring/2025/recheck-report.pdf", "sha256": "b" * 64}],
        "reason": "复测发现部分样地边界重叠，调减计储量",
    }, actor="monitoring_body")
    assert eng.confirmations["2025"]["event_hash"] == conf_hash, "历史确认不得变化"
    print(f"确认事件哈希仍为: {conf_hash}")
    suggestions = eng.suggest_trueups("2025")
    print("  追补建议:", {pid: cents_str(v) for pid, v in suggestions.items()})

    print("=" * 68)
    print("5. 逐方追补：负差额先冲剩余应付，超出已付部分形成返还责任")
    print("=" * 68)
    for pid, delta in suggestions.items():
        eng.append("trueup.raised", f"trueup-2025-{pid}", {
            "period_id": "2025", "participant_id": pid,
            "trueup_id": f"TU-2025-{pid}",
            "delta": cents_str(delta),
            "reason": "依据监测更正 monitoring-2025-v2 重算差额",
        }, actor="office")
        tu = eng.trueups[-1]
        print(f"  {pid}: 追补 {cents_str(tu.delta)}  冲应付 {cents_str(tu.payable_offset)}"
              f"  返还责任 {cents_str(tu.clawback)}")

    print("=" * 68)
    print("6. 林农甲返还超付；争议冻结期间林农乙的支付被阻止")
    print("=" * 68)
    due = eng.line_account("2025", "LN-A")["clawback_due"]
    office("payment.made", "return-ln-a-1", {
        "period_id": "2025", "participant_id": "LN-A", "payment_id": "RET-0001",
        "amount": due, "direction": "return", "payment_ref": "BANK/2025/R001",
    })
    print(f"  林农甲返还 {due}，剩余追补责任 "
          f"{eng.line_account('2025', 'LN-A')['clawback_due']}")

    office("dispute.frozen", "dispute-ln-b-1", {
        "participant_id": "LN-B", "case_ref": "DS-2025-07",
        "reason": "共有林权份额异议，冻结待裁",
    })
    print("  林农乙进入争议冻结")
    try:
        office("payment.made", "pay-ln-b-blocked", {
            "period_id": "2025", "participant_id": "LN-B", "payment_id": "PAY-BLOCKED",
            "amount": "100.00",
        })
    except Exception as exc:  # noqa: BLE001 - 演示需要展示拦截
        print(f"  冻结期支付被系统阻止：{exc}")
    office("dispute.resolved", "dispute-ln-b-2", {
        "participant_id": "LN-B", "case_ref": "DS-2025-07",
        "resolution": "林权裁定维持原份额，解除冻结",
    })
    office("payment.made", "pay-ln-b-final", {
        "period_id": "2025", "participant_id": "LN-B", "payment_id": "PAY-0002",
        "amount": eng.line_account("2025", "LN-B")["remaining_payable"],
    })
    print("  解冻后林农乙尾款已付，剩余应付 "
          f"{eng.line_account('2025', 'LN-B')['remaining_payable']}")

    print("=" * 68)
    print("7. 分角色查看同一本账：哈希根一致，敏感信息按角色可见")
    print("=" * 68)
    bundle = eng.period_bundle("2025")
    office_view = bundle
    baseline_view = view_period_bundle(bundle, "baseline_body")
    farmer_pack = eng.participant_evidence_pack("LN-A")
    print(f"  办公室期包哈希根   : {office_view['ledger_root_hash']}")
    print(f"  基线机构期包哈希根 : {baseline_view['ledger_root_hash']}")
    print(f"  林农甲依据包哈希根 : {farmer_pack['ledger_root_hash']}")
    office_pay = office_view["flows"]["payments"][0]
    base_pay = baseline_view["flows"]["payments"][0]
    print(f"  支付凭证（办公室可见）: {office_pay['payment_ref']}")
    print(f"  支付凭证（基线机构）  : {base_pay['payment_ref']}")
    own = farmer_pack["periods"][0]["own_line"]
    print(f"  林农甲本方行：应得 {own['gross']} → 权益 {own['entitlement']}，"
          f"已付 {own['paid_net']}，追补责任 {own['clawback_due']}")

    print("=" * 68)
    print("四类流水（确认/暂缓/支付/追补）：")
    print("=" * 68)
    print(json.dumps(eng.period_flows("2025"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
