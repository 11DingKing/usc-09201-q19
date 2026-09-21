"""基于角色的访问控制与敏感字段脱敏。

角色（service.models.Role）：

* admin 项目办公室：全部数据；
* auditor 审计/核证监督：财务流水与依据可见，个人证件号不可见；
* monitor 监测机构：可提交/查看监测证据与回执，不可见分成财务与个人信息；
* operator / forest_farmer 参与方：只能看本方对账数据，敏感地点 URI 脱敏；
* public 公众：只能凭分配哈希做存证核验。
"""

from __future__ import annotations

from typing import Any

from .models import PartyType, Role

# 仅项目办公室可见的个人字段
PARTY_SECRET_FIELDS = ("id_number",)
# 审计可见账号、但不可见证件号；监测与参与方均不可见
PARTY_RESTRICTED_FIELDS = ("contact", "account_ref")

# 敏感监测位置/原始证据 URI：办公室、审计、监测可见，参与方脱敏
LOCATION_FIELDS = ("source_uri",)


class AccessDenied(PermissionError):
    """当前角色无权执行该操作。"""


class Actor:
    """请求方身份：角色 + 可选的参与方绑定。"""

    def __init__(self, role: Role, party_id: str | None = None) -> None:
        self.role = role
        self.party_id = party_id

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def require(self, allowed: set[Role]) -> None:
        if self.role not in allowed:
            raise AccessDenied(f"角色 {self.role.value} 无权执行此操作")

    def require_party_self_or(self, allowed: set[Role], party_id: str) -> None:
        if self.role in allowed:
            return
        if self.party_id == party_id and self.role in (
            Role.FARMER,
            Role.OPERATOR,
        ):
            return
        raise AccessDenied("只能访问本方数据")


# ---------------------------------------------------------------------------
# 命令权限
# ---------------------------------------------------------------------------

OFFICE = {Role.ADMIN}
OFFICE_AUDIT = {Role.ADMIN, Role.AUDITOR}
MONITORORS = {Role.ADMIN, Role.MONITOR}

PERMISSIONS: dict[str, set[Role]] = {
    "register_method": OFFICE,
    "register_party": OFFICE,
    "register_plot": OFFICE,
    "set_boundary": OFFICE,
    "set_share": OFFICE,
    "open_period": OFFICE,
    "register_receipt": {Role.ADMIN, Role.MONITOR},
    "record_evidence": MONITORORS,
    "confirm_allocation": OFFICE,
    "withhold": OFFICE,
    "pay": OFFICE,
    "release_withheld": OFFICE,
    "claim_back": OFFICE,
    "resolve_dispute": OFFICE,
    "period_summary": OFFICE_AUDIT,
    "allocation_versions": OFFICE_AUDIT,
    "list_parties": OFFICE_AUDIT,
    "list_evidence": MONITORORS | {Role.AUDITOR},
    "verify_anchor": set(Role),  # 任意角色（含公众）
}


def authorize(actor: Actor, action: str) -> None:
    allowed = PERMISSIONS[action]
    actor.require(allowed)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def redact_party(party_dict: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """按角色裁剪参与方敏感字段。"""

    result = dict(party_dict)
    if actor.role == Role.ADMIN:
        return result
    if actor.role == Role.AUDITOR:
        for field_name in PARTY_SECRET_FIELDS:
            result[field_name] = _mask(result.get(field_name, ""))
        return result
    for field_name in PARTY_SECRET_FIELDS + PARTY_RESTRICTED_FIELDS:
        result.pop(field_name, None)
    return result


def redact_statement(statement: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """参与方视角脱敏：隐去监测原始位置；审计视角隐去证件类信息（本接口不含）。"""

    result = dict(statement)
    if actor.role in (Role.ADMIN, Role.AUDITOR):
        return result
    redacted_basis = []
    for item in result.get("basis", []):
        item = dict(item)
        evidence = dict(item.get("evidence", {}))
        for field_name in LOCATION_FIELDS:
            if evidence.get(field_name):
                evidence[field_name] = "***REDACTED***"
        item["evidence"] = evidence
        redacted_basis.append(item)
    result["basis"] = redacted_basis
    return result


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def party_type_to_role(party_type: PartyType) -> Role:
    """参与方类型对应的默认登录角色。"""

    mapping = {
        PartyType.FOREST_FARMER: Role.FARMER,
        PartyType.OPERATOR: Role.OPERATOR,
        PartyType.COLLECTIVE: Role.ADMIN,
    }
    return mapping[party_type]
