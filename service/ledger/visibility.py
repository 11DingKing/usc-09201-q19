"""分角色可见性。

所有角色看到的账本哈希根、事件哈希与锁定版本完全一致；差别仅在
敏感交易字段（银行账户、证件号、联系方式、支付凭证号）与他方金额
的裁剪上。依据包不按角色重算，只做字段过滤。
"""

from __future__ import annotations

import copy
from typing import Any

from .events import SENSITIVE_PAYLOAD_FIELDS
from .store import event_to_dict

# 办公室之外的内部机构角色（基线、方法、边界、监测、核证）。
INSTITUTIONAL_ROLES = frozenset(
    {
        "office",
        "monitoring_body",
        "baseline_body",
        "methodology_body",
        "boundary_body",
        "verification_body",
    }
)

REDACTED = "***REDACTED***"


def is_office(role: str) -> bool:
    return role == "office"


def can_view_period_bundle(role: str) -> bool:
    return role in INSTITUTIONAL_ROLES


def redact_event(event_dict: dict[str, Any], role: str) -> dict[str, Any]:
    """按角色过滤事件中的敏感字段。办公室不过滤。"""

    if is_office(role):
        return event_dict
    out = copy.deepcopy(event_dict)
    for field in SENSITIVE_PAYLOAD_FIELDS.get(out["type"], ()):
        if field in out.get("payload", {}):
            out["payload"][field] = REDACTED
    return out


def _scrub(obj: Any, fields: set[str]) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if k in fields else _scrub(v, fields)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, fields) for v in obj]
    return obj


_PAYMENT_SECRET_FIELDS = {"bank_account", "payment_ref", "id_number", "contact"}


def view_period_bundle(bundle: dict[str, Any], role: str) -> dict[str, Any]:
    """机构视角的期依据包。

    - 办公室：全量。
    - 其他机构：锁定版本、证据哈希、确认哈希、各方金额与流水保留，
      银行账户/凭证号等敏感字段打码；监测机构之外不回显证据明细，
      仅保留事件哈希以便核对。
    """

    if is_office(role):
        return bundle
    out = copy.deepcopy(bundle)
    out = _scrub(out, _PAYMENT_SECRET_FIELDS)
    if role != "monitoring_body":
        def _hide_evidence(mon: dict[str, Any]) -> None:
            mon["evidence"] = [
                {"ref": REDACTED, "sha256": item["sha256"]} for item in mon.get("evidence", [])
            ]

        _hide_evidence(out["locked"]["monitoring"])
        for row in out["locked"].get("monitoring_history", []):
            _hide_evidence(row)
    if role != "verification_body":
        # 非核证机构只见回执编号与哈希，不见附加备注。
        for receipt in out["locked"].get("receipts", []):
            receipt.pop("note", None)
    return out


def view_participant_pack(pack: dict[str, Any], *, requester_role: str) -> dict[str, Any]:
    """参与方下载自己的依据包：敏感字段对其本人可见，无需裁剪。"""

    _ = requester_role
    return pack
