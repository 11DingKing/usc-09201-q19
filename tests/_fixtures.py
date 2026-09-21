"""共享账场景测试夹具。"""

from __future__ import annotations

from service.ledger.engine import LedgerEngine
from service.ledger.store import EventStore

PERIOD = "2025"

SHARES = [
    ("C", "0.10"),
    ("FA", "0.35"),
    ("FB", "0.25"),
    ("OP", "0.30"),
]
INITIAL_ALLOC = "10000.00"


def build_confirmed_engine(alloc: str = INITIAL_ALLOC) -> LedgerEngine:
    """搭好一本已确认首期的账：方法/基线/边界/参与方/期/份额/监测/回执/确认。"""

    eng = LedgerEngine(EventStore())

    def office(type_: str, event_id: str, payload: dict) -> None:
        eng.append(type_, event_id, payload, actor="office")

    office("method.registered", "m1", {"version": "M-1", "doc_hash": "h-method"})
    office("baseline.registered", "b1", {"version": "BL-1", "doc_hash": "h-bl"})
    office("boundary.changed", "bd1", {"version": "BND-1", "area_mu": 1200, "doc_hash": "h-bnd"})
    for pid, kind, name in [
        ("C", "village_collective", "村集体"),
        ("FA", "forest_farmer", "林农甲"),
        ("FB", "forest_farmer", "林农乙"),
        ("OP", "operator", "运营方"),
    ]:
        office(
            "participant.registered",
            f"p-{pid}",
            {"participant_id": pid, "kind": kind, "name": name, "bank_account": f"ACC-{pid}", "id_number": "ID-X"},
        )
    office(
        "period.opened",
        "per1",
        {"period_id": PERIOD, "title": "2025首期", "boundary_version": "BND-1",
         "starts_on": "2025-01-01", "ends_on": "2025-12-31"},
    )
    office(
        "share.rule.locked",
        "sr1",
        {"period_id": PERIOD, "version": "SR-1",
         "lines": [{"participant_id": pid, "share": share} for pid, share in SHARES]},
    )
    eng.append(
        "monitoring.submitted",
        "mon1",
        {"period_id": PERIOD, "allocatable_amount": alloc, "method_version": "M-1",
         "baseline_version": "BL-1", "boundary_version": "BND-1",
         "evidence": [{"ref": "evidence-1.pdf", "sha256": "a" * 64}]},
        actor="monitoring_body",
    )
    eng.append(
        "verification.receipt.recorded",
        "rcp1",
        {"period_id": PERIOD, "receipt_no": "VR-001", "doc_hash": "r" * 64},
        actor="verification_body",
    )
    amounts = {"C": "1000.00", "FA": "3500.00", "FB": "2500.00", "OP": "3000.00"}
    if alloc != INITIAL_ALLOC:
        # 非标准金额时按份额用引擎同算法给出整数分配，供测试自行传入。
        from decimal import Decimal

        from service.ledger.engine import to_cents

        alloc_cents = to_cents(alloc, field="alloc")
        raw = {pid: Decimal(alloc_cents) * Decimal(share) for pid, share in SHARES}
        floors = {pid: int(v // 1) for pid, v in raw.items()}
        rest = alloc_cents - sum(floors.values())
        for pid, _ in sorted(raw.items(), key=lambda kv: kv[1] - (kv[1] // 1), reverse=True)[:rest]:
            floors[pid] += 1
        from service.ledger.engine import cents_str

        amounts = {pid: cents_str(v) for pid, v in floors.items()}
    office(
        "distribution.confirmed",
        "conf1",
        {"period_id": PERIOD, "monitoring_ref": "mon1", "method_version": "M-1",
         "baseline_version": "BL-1", "boundary_version": "BND-1", "share_rule_version": "SR-1",
         "allocatable_amount": alloc,
         "lines": [
             {"participant_id": pid, "share": share, "amount": amounts[pid]}
             for pid, share in SHARES
         ]},
    )
    return eng


def raise_trueup_for_correction(eng: LedgerEngine, new_alloc: str, *, reason_event: str) -> None:
    """按引擎建议，对每方发起追补事件。"""

    from service.ledger.engine import cents_str

    for pid, delta in eng.suggest_trueups(PERIOD).items():
        eng.append(
            "trueup.raised",
            f"tu-{pid}-{reason_event}",
            {"period_id": PERIOD, "participant_id": pid, "trueup_id": f"TU-{pid}-{reason_event}",
             "delta": cents_str(delta), "reason": f"监测更正 {reason_event}"},
            actor="office",
        )
