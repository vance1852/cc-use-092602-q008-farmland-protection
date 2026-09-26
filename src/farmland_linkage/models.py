"""耕地保护与宅基地退出联动领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
LAND_USES = {"cultivated", "homestead", "construction", "facility", "forest", "reserve"}
SCOPE_TYPES = {"permanent_basic_farmland", "use_control", "infrastructure_boundary"}
PURPOSES = {"contracted-supplement", "resettlement"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def boolean_flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是布尔值")
    return value


def purpose_text(value: object, field: str = "purpose") -> str:
    result = required_text(value, field, 32)
    if result not in PURPOSES:
        raise ValidationFailed(f"{field} 必须是 contracted-supplement 或 resettlement")
    return result


@dataclass(frozen=True, slots=True)
class ParcelVersionInput:
    parcel_id: str
    village_id: str
    land_use: str
    permanent_basic_farmland: bool
    within_infrastructure_boundary: bool
    area_mu: Decimal
    current_holder_household_id: str | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ParcelVersionInput":
        land_use = required_text(raw.get("land_use"), "land_use", 32)
        if land_use not in LAND_USES:
            raise ValidationFailed("land_use 不是受支持的用途管制分类")
        holder = raw.get("current_holder_household_id")
        return cls(
            parcel_id=identifier(raw.get("parcel_id"), "parcel_id"),
            village_id=identifier(raw.get("village_id"), "village_id"),
            land_use=land_use,
            permanent_basic_farmland=boolean_flag(
                raw.get("permanent_basic_farmland"), "permanent_basic_farmland"
            ),
            within_infrastructure_boundary=boolean_flag(
                raw.get("within_infrastructure_boundary"), "within_infrastructure_boundary"
            ),
            area_mu=decimal_value(raw.get("area_mu"), "area_mu", minimum=Decimal("0.001")),
            current_holder_household_id=None if holder is None else identifier(holder, "current_holder_household_id"),
            note=required_text(raw.get("note", "-"), "note", 256),
        )


@dataclass(frozen=True, slots=True)
class ProtectionRuleInput:
    rule_id: str
    parcel_id: str
    scope_type: str
    allowed_purposes: tuple[str, ...]
    effective_from: str | None
    effective_to: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProtectionRuleInput":
        scope_type = required_text(raw.get("scope_type"), "scope_type", 32)
        if scope_type not in SCOPE_TYPES:
            raise ValidationFailed("scope_type 不是受支持的保护范围类型")
        purposes_raw = raw.get("allowed_purposes")
        purposes: tuple[str, ...] = ()
        if scope_type == "use_control":
            if not isinstance(purposes_raw, list) or not purposes_raw:
                raise ValidationFailed("use_control 规则必须给出非空 allowed_purposes")
            purposes = tuple(purpose_text(item, "allowed_purposes") for item in purposes_raw)
            if len(set(purposes)) != len(purposes):
                raise ValidationFailed("allowed_purposes 不能重复")
        elif purposes_raw is not None:
            raise ValidationFailed("只有 use_control 规则可以携带 allowed_purposes")
        effective_from = raw.get("effective_from")
        effective_to = raw.get("effective_to")
        parsed_from = None if effective_from is None else date_text(effective_from, "effective_from")
        parsed_to = None if effective_to is None else date_text(effective_to, "effective_to")
        if parsed_from is not None and parsed_to is not None and parsed_to < parsed_from:
            raise ValidationFailed("effective_to 不能早于 effective_from")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            parcel_id=identifier(raw.get("parcel_id"), "parcel_id"),
            scope_type=scope_type,
            allowed_purposes=purposes,
            effective_from=parsed_from,
            effective_to=parsed_to,
        )


@dataclass(frozen=True, slots=True)
class RuleBatchImport:
    batch_id: str
    rules: tuple[ProtectionRuleInput, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleBatchImport":
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ValidationFailed("rules 必须是非空数组")
        if len(rules_raw) > 500:
            raise ValidationFailed("单批保护规则不能超过 500 条")
        rules: list[ProtectionRuleInput] = []
        for item in rules_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("rules 元素必须是对象")
            rules.append(ProtectionRuleInput.from_dict(item))
        parsed = tuple(rules)
        rule_ids = [rule.rule_id for rule in parsed]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValidationFailed("同一批次内 rule_id 不能重复")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            rules=parsed,
        )


@dataclass(frozen=True, slots=True)
class HouseholdInput:
    household_id: str
    village_id: str
    head_name: str
    members: tuple[tuple[str, str], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdInput":
        members_raw = raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise ValidationFailed("members 必须是非空数组")
        members: list[tuple[str, str]] = []
        for item in members_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("members 元素必须是对象")
            members.append((
                identifier(item.get("member_id"), "member_id"),
                required_text(item.get("name"), "name", 64),
            ))
        member_ids = [member_id for member_id, _ in members]
        if len(set(member_ids)) != len(member_ids):
            raise ValidationFailed("member_id 不能重复")
        return cls(
            household_id=identifier(raw.get("household_id"), "household_id"),
            village_id=identifier(raw.get("village_id"), "village_id"),
            head_name=required_text(raw.get("head_name"), "head_name", 64),
            members=tuple(members),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationInput:
    authorization_id: str
    household_id: str
    homestead_parcel_id: str
    consented_member_ids: tuple[str, ...]
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorizationInput":
        consented_raw = raw.get("consented_member_ids")
        if not isinstance(consented_raw, list) or not consented_raw:
            raise ValidationFailed("consented_member_ids 必须是非空数组")
        consented = tuple(identifier(item, "consented_member_ids") for item in consented_raw)
        if len(set(consented)) != len(consented):
            raise ValidationFailed("consented_member_ids 不能重复")
        return cls(
            authorization_id=identifier(raw.get("authorization_id"), "authorization_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            homestead_parcel_id=identifier(raw.get("homestead_parcel_id"), "homestead_parcel_id"),
            consented_member_ids=consented,
            valid_until=date_text(raw.get("valid_until"), "valid_until"),
        )


@dataclass(frozen=True, slots=True)
class CommitmentInput:
    commitment_id: str
    household_id: str
    parcel_id: str
    responsible_party: str
    promised_deadline: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentInput":
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            parcel_id=identifier(raw.get("parcel_id"), "parcel_id"),
            responsible_party=required_text(raw.get("responsible_party"), "responsible_party", 128),
            promised_deadline=date_text(raw.get("promised_deadline"), "promised_deadline"),
        )


@dataclass(frozen=True, slots=True)
class ProjectInput:
    project_id: str
    name: str
    village_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectInput":
        villages_raw = raw.get("village_ids")
        if not isinstance(villages_raw, list) or not villages_raw:
            raise ValidationFailed("village_ids 必须是非空数组")
        villages = tuple(identifier(item, "village_ids") for item in villages_raw)
        if len(set(villages)) != len(villages):
            raise ValidationFailed("village_ids 不能重复")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            name=required_text(raw.get("name"), "name"),
            village_ids=villages,
        )


@dataclass(frozen=True, slots=True)
class ApplicationInput:
    application_id: str
    project_id: str
    household_id: str
    homestead_parcel_id: str
    supplement_parcel_id: str
    authorization_id: str
    commitment_id: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationInput":
        return cls(
            application_id=identifier(raw.get("application_id"), "application_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            homestead_parcel_id=identifier(raw.get("homestead_parcel_id"), "homestead_parcel_id"),
            supplement_parcel_id=identifier(raw.get("supplement_parcel_id"), "supplement_parcel_id"),
            authorization_id=identifier(raw.get("authorization_id"), "authorization_id"),
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class PlanInput:
    plan_id: str
    application_id: str
    purpose: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanInput":
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            application_id=identifier(raw.get("application_id"), "application_id"),
            purpose=purpose_text(raw.get("purpose")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
