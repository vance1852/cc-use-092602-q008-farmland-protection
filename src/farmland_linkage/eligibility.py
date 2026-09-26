"""确定性的地块候选资格评估：用途管制、保护范围、家庭权益与基础设施边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


# 各类方案用途允许落位的用途管制分类。
PURPOSE_ALLOWED_LAND_USE = {
    "contracted-supplement": {"cultivated", "reserve"},
    "resettlement": {"construction", "homestead", "facility"},
}

USE_CONTROL_MISMATCH = "USE_CONTROL_MISMATCH"
PERMANENT_BASIC_FARMLAND = "PERMANENT_BASIC_FARMLAND"
PERMANENT_BASIC_FARMLAND_RULE = "PERMANENT_BASIC_FARMLAND_RULE"
USE_CONTROL_RULE = "USE_CONTROL_RULE"
INFRASTRUCTURE_BOUNDARY = "INFRASTRUCTURE_BOUNDARY"
INFRASTRUCTURE_BOUNDARY_RULE = "INFRASTRUCTURE_BOUNDARY_RULE"
FAMILY_RIGHTS_HELD = "FAMILY_RIGHTS_HELD"
FAMILY_RIGHTS_PINNED = "FAMILY_RIGHTS_PINNED"


@dataclass(frozen=True, slots=True)
class ParcelSnapshot:
    """参与资格评估的地块版本快照。"""

    parcel_id: str
    parcel_version_id: int
    village_id: str
    land_use: str
    permanent_basic_farmland: bool
    within_infrastructure_boundary: bool
    current_holder_household_id: str | None


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    """评估时点处于有效期内的保护规则。"""

    rule_id: str
    parcel_id: str
    scope_type: str
    allowed_purposes: tuple[str, ...]


def _reason(rule_code: str, detail: str, **basis: object) -> dict[str, object]:
    return {"rule_code": rule_code, "detail": detail, "basis": basis}


def evaluate_parcel(
    parcel: ParcelSnapshot,
    active_rules: Sequence[RuleSnapshot],
    purpose: str,
    household_id: str,
    pinned_elsewhere: Iterable[str],
) -> list[dict[str, object]]:
    """返回地块被排除的具体依据列表；空列表表示满足全部边界可以落位。"""
    if purpose not in PURPOSE_ALLOWED_LAND_USE:
        raise ValueError("未知的方案用途")
    reasons: list[dict[str, object]] = []
    if parcel.land_use not in PURPOSE_ALLOWED_LAND_USE[purpose]:
        reasons.append(_reason(
            USE_CONTROL_MISMATCH,
            f"登记用途管制分类 {parcel.land_use} 不允许用于 {purpose}",
            parcel_version_id=parcel.parcel_version_id,
            land_use=parcel.land_use,
        ))
    if parcel.permanent_basic_farmland:
        reasons.append(_reason(
            PERMANENT_BASIC_FARMLAND,
            "登记版本位于永久基本农田保护范围，不得占用",
            parcel_version_id=parcel.parcel_version_id,
        ))
    if parcel.within_infrastructure_boundary:
        reasons.append(_reason(
            INFRASTRUCTURE_BOUNDARY,
            "登记版本位于基础设施边界内，不得安排安置或补充分配",
            parcel_version_id=parcel.parcel_version_id,
        ))
    for rule in active_rules:
        if rule.parcel_id != parcel.parcel_id:
            continue
        if rule.scope_type == "permanent_basic_farmland":
            reasons.append(_reason(
                PERMANENT_BASIC_FARMLAND_RULE,
                "保护规则将地块划入永久基本农田保护范围，不得占用",
                rule_id=rule.rule_id,
            ))
        elif rule.scope_type == "infrastructure_boundary":
            reasons.append(_reason(
                INFRASTRUCTURE_BOUNDARY_RULE,
                "保护规则将地块划入基础设施边界，不得安排安置或补充分配",
                rule_id=rule.rule_id,
            ))
        elif rule.scope_type == "use_control" and purpose not in rule.allowed_purposes:
            reasons.append(_reason(
                USE_CONTROL_RULE,
                f"用途管制规则仅允许 {','.join(rule.allowed_purposes)}，不允许 {purpose}",
                rule_id=rule.rule_id,
                allowed_purposes=list(rule.allowed_purposes),
            ))
    holder = parcel.current_holder_household_id
    if holder is not None and holder != household_id:
        reasons.append(_reason(
            FAMILY_RIGHTS_HELD,
            "地块登记在册权利人为其他家庭主体，家庭权益冲突",
            parcel_version_id=parcel.parcel_version_id,
        ))
    if parcel.parcel_id in set(pinned_elsewhere):
        reasons.append(_reason(
            FAMILY_RIGHTS_PINNED,
            "地块已被其他未终结方案引用为确定版本，家庭权益冲突",
            parcel_id=parcel.parcel_id,
        ))
    return reasons


def generate_candidates(
    parcels: Iterable[ParcelSnapshot],
    rules_by_parcel: Mapping[str, Sequence[RuleSnapshot]],
    purpose: str,
    household_id: str,
    pinned_elsewhere: Iterable[str],
) -> dict[str, object]:
    """按地块编号稳定排序生成候选与排除清单，排除项附带具体依据。"""
    pinned = set(pinned_elsewhere)
    eligible: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for parcel in sorted(parcels, key=lambda item: item.parcel_id):
        reasons = evaluate_parcel(
            parcel,
            rules_by_parcel.get(parcel.parcel_id, ()),
            purpose,
            household_id,
            pinned,
        )
        row = {"parcel_id": parcel.parcel_id, "parcel_version_id": parcel.parcel_version_id}
        if reasons:
            excluded.append({**row, "reasons": reasons})
        else:
            eligible.append(row)
    return {"purpose": purpose, "eligible": eligible, "excluded": excluded}
