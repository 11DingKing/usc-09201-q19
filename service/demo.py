"""首期结算演示：完整走查森林碳益共享账的业务闭环。

运行：python3 -m service.demo
事件默认写入 demo-ledger.jsonl，删除该文件可重新演示。
"""

from __future__ import annotations

import json
import os

from .access import Actor
from .app import AppService
from .ledger import LedgerError
from .models import DisputeDirection, PartyType, Role
from .store import EventStore

DEMO_PATH = os.environ.get("DEMO_LEDGER_PATH", "demo-ledger.jsonl")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) / 100:.2f} 元"


def show(title: str, data: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def run() -> AppService:
    fresh = not os.path.exists(DEMO_PATH)
    app = AppService(EventStore(DEMO_PATH))
    L = app.ledger

    admin = Actor(Role.ADMIN)
    monitor = Actor(Role.MONITOR)
    farmer = Actor(Role.FARMER, "F1")
    operator = Actor(Role.OPERATOR, "O1")
    auditor = Actor(Role.AUDITOR)
    public = Actor(Role.PUBLIC)

    if fresh:
        # 1. 主数据：方法版本锁定、参与方、共有林地、份额 ----------------
        L.register_method("FJ-FOREST", 3, "福建省林业碳汇方法学", "params:sha256:9f86d0",
                          idem_key="m-fj-v3")
        L.register_party("F1", PartyType.FOREST_FARMER, "林农张茂林",
                         contact="138****0001", id_number="350121197001011234",
                         account_ref="CUAV-F1")
        L.register_party("F2", PartyType.FOREST_FARMER, "林农李秀竹",
                         contact="138****0002", id_number="350121197203032345",
                         account_ref="CUAV-F2")
        L.register_party("O1", PartyType.OPERATOR, "青山运营有限公司",
                         contact="ops@qingshan.example", account_ref="BANK-O1")
        L.register_party("C1", PartyType.COLLECTIVE, "岭后村股份经济合作社")

        L.register_plot("P1", "岭后村一号共有山场")
        L.register_plot("P2", "岭后村二号山场")
        L.set_boundary("P1", True, "120.5", "2026-01-01", "项目首期边界")
        # P2 首年尚未纳入边界

        # 共有林地份额：林农 F1 45%、F2 25%、运营 20%、村集体 10%
        for party_id, bps in [("F1", 4500), ("F2", 2500), ("O1", 2000), ("C1", 1000)]:
            L.set_share("P1", party_id, bps, "2026-01-01")

        # 碳价 60 元/吨 = 6000 分/吨
        L.open_period("2026H1", "2026 年上半年度", "2026-01-01", "2026-06-30", 6000)

        # 2. 核证回执（不同机构维护）+ 监测证据 -------------------------
        L.register_receipt("RC-2026-001", "省林业核证中心", "2026-07-08",
                           "receipt:sha256:a1b2")
        L.record_evidence(
            "EV-2026H1-P1-V1", "2026H1", "P1", "FJ-FOREST", 3,
            200_000,  # 200 吨
            "monitor://2026H1/P1/v1/dataset.zip",
            "省林业调查规划院", receipt_id="RC-2026-001",
        )

        # 3. 确认 v1：锁定方法、证据、可分配量、份额与边界 ---------------
        L.confirm_allocation("2026H1", idem_key="confirm-2026H1-v1")
        show("v1 确认后办公室汇总", app.period_summary(admin, "2026H1"))

        # 4. 首期演示会：先支付部分收益、暂缓一部分 ----------------------
        # F1 应分 5400 元；现场先付 3000 元，另有 600 元因边界复核暂缓
        L.pay("2026H1", "F1", 300_000, "PAY-2026H1-F1-01")
        L.withhold("2026H1", "F1", 60_000, "二号山场边界复核，暂缓 600 元")
        L.pay("2026H1", "O1", 100_000, "PAY-2026H1-O1-01")

        # 5. 重复核证回执被拒绝 ----------------------------------------
        L.register_receipt("RC-2026-002", "省林业核证中心", "2026-07-20",
                           "receipt:sha256:c3d4")
        try:
            L.record_evidence(
                "EV-DUP", "2026H1", "P1", "FJ-FOREST", 3, 200_000,
                "monitor://dup", "省林业调查规划院", receipt_id="RC-2026-001",
            )
            raise AssertionError("重复回执必须被拒绝")
        except LedgerError:
            print("\n[规则] 重复核证回执已被拒绝（同一回执只能占用一次）")

        # 6. 监测结果更正：200t -> 170t（现场样地复核下调）--------------
        L.record_evidence(
            "EV-2026H1-P1-V2", "2026H1", "P1", "FJ-FOREST", 3,
            170_000,
            "monitor://2026H1/P1/v2/dataset.zip",
            "省林业调查规划院", receipt_id="RC-2026-002",
        )
        print(f"[规则] 更正后分配状态：{L._latest_allocation('2026H1').state.value}，暂停支付")
        try:
            L.pay("2026H1", "F2", 10_000, "PAY-BLOCKED")
            raise AssertionError("STALE 期间必须暂停支付")
        except LedgerError:
            print("[规则] STALE 期间支付已被拦截")

        # 暂缓款先收回，待追补版本确认时一次结清
        L.release_withheld("2026H1", "F1", 60_000, "WH-RECLAIM-01", reclaim=True)

        # 7. 确认 v2 追补版本（负调整）----------------------------------
        L.confirm_allocation("2026H1", idem_key="confirm-2026H1-v2")
        f1 = app.statement(admin, "2026H1", "F1")
        show("F1 更正后对账单（办公室视角）", f1)

        # 8. 共有林地份额变化：经村民代表会议，运营份额 20% -> 15%，
        #    差额 5% 转给村集体；仅对之后确认的版本生效，历史不重写 ----
        L.set_share("P1", "O1", 1500, "2026-04-01", idem_key="share-o1-cut")
        L.set_share("P1", "C1", 1500, "2026-04-01", idem_key="share-c1-up")
        L.confirm_allocation("2026H1", idem_key="confirm-2026H1-v3")
        show("份额变化后 v3 汇总", app.period_summary(admin, "2026H1"))

        # 9. 项目边界变化：二号山场纳入，补充证据后确认 v4 --------------
        L.set_boundary("P2", True, "60.0", "2026-05-01", "边界扩展纳入二号山场")
        for party_id, bps in [("F1", 4500), ("F2", 2500), ("O1", 1500), ("C1", 1500)]:
            L.set_share("P2", party_id, bps, "2026-05-01")
        L.register_receipt("RC-2026-003", "省林业核证中心", "2026-07-25",
                           "receipt:sha256:e5f6")
        L.record_evidence(
            "EV-2026H1-P2-V1", "2026H1", "P2", "FJ-FOREST", 3,
            50_000, "monitor://2026H1/P2/v1/dataset.zip",
            "省林业调查规划院", receipt_id="RC-2026-003",
        )
        L.confirm_allocation("2026H1", idem_key="confirm-2026H1-v4")

        # 10. 争议冻结：F2 对面积有异议，冻结其剩余应付 ----------------
        f2_due = L.statement("2026H1", "F2")["payable_cents"]
        L.open_dispute(
            "DSP-001", "2026H1", "F2", f2_due, DisputeDirection.OUTWARD,
            "F2 主张二号山场共有份额丈量有误",
        )
        try:
            L.pay("2026H1", "F2", f2_due, "PAY-FROZEN")
            raise AssertionError("冻结金额不可支付")
        except LedgerError:
            print("[规则] 争议冻结金额的支付已被拦截")

    # -----------------------------------------------------------------
    # 结果展示：剩余应付、追补责任、各方下载的依据一致
    # -----------------------------------------------------------------
    summary = app.period_summary(admin, "2026H1")
    show("最终办公室汇总（v4）", summary)

    for label, actor_, pid in [
        ("林农 F1（本人视角，敏感地点脱敏）", farmer, "F1"),
        ("运营方 O1（本人视角）", operator, "O1"),
        ("审计视角", auditor, "F1"),
    ]:
        st = app.statement(actor_, "2026H1", pid)
        st_light = {k: v for k, v in st.items() if k != "basis"}
        show(label, st_light)

    # 跨角色一致性：三方拿到的 allocation_hash 必须相同
    hashes = {
        app.statement(admin, "2026H1", "F1")["allocation_hash"],
        app.statement(farmer, "2026H1", "F1")["allocation_hash"],
        app.statement(auditor, "2026H1", "F1")["allocation_hash"],
    }
    assert len(hashes) == 1, "各方下载到的分配哈希必须一致"
    print(f"\n[一致性] 办公室/林农/审计下载到同一 allocation_hash：{hashes.pop()[:24]}...")

    # 权限：林农不能看别人；公众只能做存证核验
    try:
        app.statement(farmer, "2026H1", "O1")
        raise AssertionError("林农不得查看运营方账单")
    except PermissionError:
        print("[权限] 林农查看运营方账单被拒绝")
    anchor = app.verify_anchor(public, summary["allocation_hash"])
    show("公众存证核验（无敏感信息）", anchor)

    # 历史版本永不重写
    show("历史锁定版本清单", app.allocation_versions(admin, "2026H1"))
    print("\n[完整性] 事件链校验：", app.ledger.store.verify_chain() or "完好")
    return app


if __name__ == "__main__":
    run()
