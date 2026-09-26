"""耕地保护与宅基地退出联动的事务用例。

跨村整治项目受理宅基地退出时同步校验：
- 补充分配的承包地是否占用永久基本农田；
- 家庭成员是否仍具备资格；
- 复垦责任承诺是否已经落实（承诺到期时间未过）；
- 家庭主体授权是否仍在有效期内。

安置与土地方案必须引用确定的地块版本；候选地块只能在同时满足
用途管制、家庭权益和基础设施边界的区域生成，并为每个被排除的
地块给出具体依据。资格撤回立即阻止尚未确认的方案，已确认未开工
和施工中的方案进入人工处置，已经发生的合法交付不受影响，地块
引用不做静默迁移。保护规则批量导入全有或全无，重复提交返回稳定结果。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal
from typing import Any, Mapping

from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import date_text, decimal_value, identifier, positive_integer, required_text
from .planning import canonical_json, digest
from .service import ROLE_PERMISSIONS, SupplyService
from .storage import transaction


LAND_USES = {"cultivated", "homestead", "construction", "facility"}
LAND_USE_LABELS = {
    "cultivated": "耕地",
    "homestead": "宅基地",
    "construction": "建设用地",
    "facility": "设施用地",
}
PURPOSES = {"resettlement", "contracted-land"}
PURPOSE_USE = {"resettlement": "construction", "contracted-land": "cultivated"}
PURPOSE_LABELS = {"resettlement": "安置", "contracted-land": "承包地"}
RULE_TYPES = {"permanent_basic_farmland", "use_control"}
MAX_BATCH_RULES = 500


def _bool_flag(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValidationFailed(f"{field} 必须是布尔值")


def _land_use(value: object, field: str) -> str:
    result = required_text(value, field, 32)
    if result not in LAND_USES:
        raise ValidationFailed(f"{field} 不是受支持的用途类型")
    return result


@dataclass(frozen=True, slots=True)
class ParcelVersionInput:
    land_use: str
    area_mu: Decimal
    within_infrastructure_boundary: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], prefix: str = "version") -> "ParcelVersionInput":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{prefix} 必须是对象")
        return cls(
            land_use=_land_use(raw.get("land_use"), f"{prefix}.land_use"),
            area_mu=decimal_value(raw.get("area_mu"), f"{prefix}.area_mu", minimum=Decimal("0.001")),
            within_infrastructure_boundary=_bool_flag(
                raw.get("within_infrastructure_boundary"), f"{prefix}.within_infrastructure_boundary"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProtectionRuleInput:
    rule_id: str
    parcel_id: str
    rule_type: str
    restricted_use: str | None
    effective_from: str
    effective_to: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "ProtectionRuleInput":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"rules[{index}] 必须是对象")
        field = f"rules[{index}]"
        rule_type = required_text(raw.get("rule_type"), f"{field}.rule_type", 32)
        if rule_type not in RULE_TYPES:
            raise ValidationFailed(f"{field}.rule_type 不是受支持的规则类型")
        effective_from = date_text(raw.get("effective_from"), f"{field}.effective_from")
        raw_to = raw.get("effective_to")
        effective_to = None if raw_to is None else date_text(raw_to, f"{field}.effective_to")
        if effective_to is not None and effective_to < effective_from:
            raise ValidationFailed(f"{field}.effective_to 不能早于 effective_from")
        restricted_use: str | None = None
        if rule_type == "use_control":
            restricted_use = _land_use(raw.get("restricted_use"), f"{field}.restricted_use")
        return cls(
            rule_id=identifier(raw.get("rule_id"), f"{field}.rule_id"),
            parcel_id=identifier(raw.get("parcel_id"), f"{field}.parcel_id"),
            rule_type=rule_type,
            restricted_use=restricted_use,
            effective_from=effective_from,
            effective_to=effective_to,
        )


@dataclass(frozen=True, slots=True)
class MemberInput:
    member_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "MemberInput":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"members[{index}] 必须是对象")
        return cls(
            member_id=identifier(raw.get("member_id"), f"members[{index}].member_id"),
            name=required_text(raw.get("name"), f"members[{index}].name"),
        )


class LinkageService(SupplyService):
    """耕地保护与宅基地退出联动服务，与供应调度共享账号、审计链和数据库连接。"""

    def _today(self) -> str:
        return self.clock.now().astimezone(timezone.utc).date().isoformat()

    # ------------------------------------------------------------------
    # 项目与地块版本
    # ------------------------------------------------------------------
    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project_id = identifier(raw.get("project_id"), "project_id")
        name = required_text(raw.get("name"), "name")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_projects(project_id,name,state,created_by,created_at) "
                    "VALUES(?,?,'accepting',?,?)",
                    (project_id, name, actor_id, self._now()),
                )
                self._audit("project", project_id, "project.created", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("整治项目编号已经存在") from exc
        return {"project_id": project_id, "name": name, "state": "accepting"}

    def _parcel_row(self, parcel_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM linkage_parcels WHERE parcel_id=?", (parcel_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"地块 {parcel_id} 不存在")
        return row

    def register_parcel(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "parcel.write")
        parcel_id = identifier(raw.get("parcel_id"), "parcel_id")
        village = required_text(raw.get("village"), "village")
        version = ParcelVersionInput.from_dict(raw.get("version"))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_parcels(parcel_id,village,created_by,created_at) VALUES(?,?,?,?)",
                    (parcel_id, village, actor_id, self._now()),
                )
                version_id = self._insert_version(parcel_id, 1, version, actor_id)
                self._audit(
                    "parcel", parcel_id, "parcel.created", actor_id,
                    {"village": village, "version_no": 1},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("地块编号已经存在") from exc
        return {
            "parcel_id": parcel_id,
            "village": village,
            "version": {"version_id": version_id, "version_no": 1, "state": "draft"},
        }

    def _insert_version(
        self, parcel_id: str, version_no: int, version: ParcelVersionInput, actor_id: str
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO linkage_parcel_versions(parcel_id,version_no,land_use,area_mu,"
            "within_infrastructure_boundary,state,registered_by,registered_at) "
            "VALUES(?,?,?,?,?,'draft',?,?)",
            (
                parcel_id,
                version_no,
                version.land_use,
                format(version.area_mu, "f"),
                1 if version.within_infrastructure_boundary else 0,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def add_parcel_version(self, actor_id: str, parcel_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "parcel.write")
        self._parcel_row(parcel_id)
        version = ParcelVersionInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS next_no FROM linkage_parcel_versions WHERE parcel_id=?",
                (parcel_id,),
            ).fetchone()
            version_no = int(row["next_no"])
            version_id = self._insert_version(parcel_id, version_no, version, actor_id)
            self._audit(
                "parcel", parcel_id, "parcel.version_registered", actor_id,
                {"version_no": version_no},
            )
        return {"parcel_id": parcel_id, "version": {"version_id": version_id, "version_no": version_no, "state": "draft"}}

    def determine_parcel_version(self, actor_id: str, parcel_id: str, version_no: object) -> dict[str, Any]:
        self._require(actor_id, "parcel.write")
        number = positive_integer(version_no, "version_no")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE linkage_parcel_versions SET state='determined',determined_at=? "
                "WHERE parcel_id=? AND version_no=? AND state='draft'",
                (self._now(), parcel_id, number),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    "SELECT state FROM linkage_parcel_versions WHERE parcel_id=? AND version_no=?",
                    (parcel_id, number),
                ).fetchone()
                if row is None:
                    raise NotFound("地块版本不存在")
                raise InvalidState("地块版本已是确定版本")
            version_id = int(self.connection.execute(
                "SELECT version_id FROM linkage_parcel_versions WHERE parcel_id=? AND version_no=?",
                (parcel_id, number),
            ).fetchone()["version_id"])
            self._audit(
                "parcel", parcel_id, "parcel.version_determined", actor_id,
                {"version_no": number},
            )
        return {"parcel_id": parcel_id, "version": {"version_id": version_id, "version_no": number, "state": "determined"}}

    # ------------------------------------------------------------------
    # 保护规则批量导入（全有或全无，重复提交返回稳定结果）
    # ------------------------------------------------------------------
    def import_protection_rules(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "protection.import")
        batch_id = identifier(raw.get("batch_id"), "batch_id")
        idempotency_key = identifier(raw.get("idempotency_key"), "idempotency_key")
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ValidationFailed("rules 必须是非空数组")
        if len(rules_raw) > MAX_BATCH_RULES:
            raise ValidationFailed(f"单批次规则不能超过 {MAX_BATCH_RULES} 条")
        parsed = [ProtectionRuleInput.from_dict(item, index) for index, item in enumerate(rules_raw)]
        request_sha256 = digest({"batch_id": batch_id, "rules": rules_raw})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM linkage_protection_batches WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("幂等键对应不同的保护规则内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        response = {
            "batch_id": batch_id,
            "imported": len(parsed),
            "rule_ids": [rule.rule_id for rule in parsed],
        }
        try:
            with transaction(self.connection, immediate=True):
                for rule in parsed:
                    self._parcel_row(rule.parcel_id)
                self.connection.execute(
                    "INSERT INTO linkage_protection_batches(batch_id,idempotency_key,request_sha256,"
                    "response_json,rule_count,imported_by,imported_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        idempotency_key,
                        request_sha256,
                        canonical_json(response),
                        len(parsed),
                        actor_id,
                        self._now(),
                    ),
                )
                for rule in parsed:
                    self.connection.execute(
                        "INSERT INTO linkage_protection_rules(rule_id,batch_id,parcel_id,rule_type,"
                        "restricted_use,effective_from,effective_to,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            rule.rule_id,
                            batch_id,
                            rule.parcel_id,
                            rule.rule_type,
                            rule.restricted_use,
                            rule.effective_from,
                            rule.effective_to,
                            self._now(),
                        ),
                    )
                self._audit(
                    "protection_batch", batch_id, "protection.imported", actor_id,
                    {"rule_count": len(parsed)},
                )
        except sqlite3.IntegrityError as exc:
            stored = self.connection.execute(
                "SELECT request_sha256,response_json FROM linkage_protection_batches WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if stored is not None and stored["request_sha256"] == request_sha256:
                return {**json.loads(stored["response_json"]), "replayed": True}
            raise Conflict("保护规则批次或规则编号冲突") from exc
        return {**response, "replayed": False}

    # ------------------------------------------------------------------
    # 家庭主体、授权与复垦承诺
    # ------------------------------------------------------------------
    def register_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household_id = identifier(raw.get("household_id"), "household_id")
        head_name = required_text(raw.get("head_name"), "head_name")
        village = required_text(raw.get("village"), "village")
        authorized_scope = required_text(raw.get("authorized_scope"), "authorized_scope")
        authorized_until = date_text(raw.get("authorized_until"), "authorized_until")
        deadline = date_text(raw.get("reclamation_commitment_deadline"), "reclamation_commitment_deadline")
        commitment_note = required_text(raw.get("commitment_note", "复垦责任承诺"), "commitment_note")
        members_raw = raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise ValidationFailed("members 必须是非空数组")
        members = [MemberInput.from_dict(item, index) for index, item in enumerate(members_raw)]
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_households(household_id,head_name,village,authorized_scope,"
                    "authorized_until,reclamation_commitment_deadline,commitment_note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        household_id,
                        head_name,
                        village,
                        authorized_scope,
                        authorized_until,
                        deadline,
                        commitment_note,
                        actor_id,
                        self._now(),
                    ),
                )
                for member in members:
                    self.connection.execute(
                        "INSERT INTO linkage_household_members(member_id,household_id,name,qualified,created_at) "
                        "VALUES(?,?,?,1,?)",
                        (member.member_id, household_id, member.name, self._now()),
                    )
                self._audit(
                    "household", household_id, "household.registered", actor_id,
                    {"members": len(members), "authorized_until": authorized_until},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭编号或成员编号冲突") from exc
        return {
            "household_id": household_id,
            "members": [{"member_id": member.member_id, "name": member.name} for member in members],
            "authorized_until": authorized_until,
            "reclamation_commitment_deadline": deadline,
        }

    def link_household_user(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        user_id = identifier(raw.get("user_id"), "user_id")
        household_id = identifier(raw.get("household_id"), "household_id")
        user = self._user(user_id)
        if user["role"] != "household":
            raise ValidationFailed("只能关联家庭角色账号")
        household = self.connection.execute(
            "SELECT household_id FROM linkage_households WHERE household_id=?", (household_id,)
        ).fetchone()
        if household is None:
            raise NotFound("家庭不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_household_users(user_id,household_id) VALUES(?,?)",
                    (user_id, household_id),
                )
                self._audit(
                    "household", household_id, "household.user_linked", actor_id,
                    {"user_id": user_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("账号已关联家庭") from exc
        return {"user_id": user_id, "household_id": household_id}

    # ------------------------------------------------------------------
    # 联动校验
    # ------------------------------------------------------------------
    def _effective_rules(self, parcel_id: str, today: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM linkage_protection_rules WHERE parcel_id=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>=?) ORDER BY rule_id",
            (parcel_id, today, today),
        ).fetchall()

    def _latest_determined_version(self, parcel_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM linkage_parcel_versions WHERE parcel_id=? AND state='determined' "
            "ORDER BY version_no DESC LIMIT 1",
            (parcel_id,),
        ).fetchone()

    def _household_check(self, household_id: str, today: str) -> list[dict[str, Any]]:
        household = self.connection.execute(
            "SELECT * FROM linkage_households WHERE household_id=?", (household_id,)
        ).fetchone()
        if household is None:
            raise NotFound("家庭不存在")
        reasons: list[dict[str, Any]] = []
        if household["authorized_until"] < today:
            reasons.append({"code": "authorization_expired", "message": "家庭主体授权已到期"})
        if household["reclamation_commitment_deadline"] < today:
            reasons.append({
                "code": "reclamation_commitment_expired",
                "message": "复垦责任承诺已到期，复垦责任未落实",
            })
        members = self.connection.execute(
            "SELECT * FROM linkage_household_members WHERE household_id=? ORDER BY member_id",
            (household_id,),
        ).fetchall()
        for member in members:
            if not member["qualified"]:
                reasons.append({
                    "code": "member_unqualified",
                    "message": f"家庭成员 {member['name']} 不具备资格",
                })
        return reasons

    def _parcel_check(
        self,
        parcel_id: str,
        purpose: str,
        today: str,
        version: sqlite3.Row | None,
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        if version is None:
            reasons.append({
                "code": "no_determined_version",
                "message": f"地块 {parcel_id} 没有确定的地块版本",
            })
            return reasons
        required_use = PURPOSE_USE[purpose]
        if version["land_use"] != required_use:
            reasons.append({
                "code": "use_control_mismatch",
                "message": (
                    f"地块 {parcel_id} 用途为{LAND_USE_LABELS[version['land_use']]}，"
                    f"不满足{PURPOSE_LABELS[purpose]}用途管制要求"
                ),
            })
        rules = self._effective_rules(parcel_id, today)
        if any(rule["rule_type"] == "permanent_basic_farmland" for rule in rules):
            reasons.append({
                "code": "permanent_basic_farmland",
                "message": f"地块 {parcel_id} 占用永久基本农田",
            })
        for rule in rules:
            if rule["rule_type"] == "use_control" and rule["restricted_use"] == required_use:
                reasons.append({
                    "code": "use_control_restricted",
                    "message": f"地块 {parcel_id} 受用途管制规则 {rule['rule_id']} 限制",
                })
        if not version["within_infrastructure_boundary"]:
            reasons.append({
                "code": "outside_infrastructure_boundary",
                "message": f"地块 {parcel_id} 超出基础设施边界",
            })
        return reasons

    # ------------------------------------------------------------------
    # 宅基地退出受理（同步校验）
    # ------------------------------------------------------------------
    def accept_withdrawal(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "withdrawal.write")
        withdrawal_id = identifier(raw.get("withdrawal_id"), "withdrawal_id")
        project_id = identifier(raw.get("project_id"), "project_id")
        household_id = identifier(raw.get("household_id"), "household_id")
        homestead_parcel_id = identifier(raw.get("homestead_parcel_id"), "homestead_parcel_id")
        supplementary_parcel_id = identifier(raw.get("supplementary_parcel_id"), "supplementary_parcel_id")
        project = self.connection.execute(
            "SELECT * FROM linkage_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("整治项目不存在")
        if project["state"] != "accepting":
            raise InvalidState("整治项目当前不受理退出申请")
        self._parcel_row(homestead_parcel_id)
        self._parcel_row(supplementary_parcel_id)
        today = self._today()
        reasons = self._household_check(household_id, today)
        supplementary_version = self._latest_determined_version(supplementary_parcel_id)
        if supplementary_version is None:
            reasons.append({
                "code": "no_determined_version",
                "message": f"补充承包地 {supplementary_parcel_id} 没有确定的地块版本",
            })
        if any(
            rule["rule_type"] == "permanent_basic_farmland"
            for rule in self._effective_rules(supplementary_parcel_id, today)
        ):
            reasons.append({
                "code": "permanent_basic_farmland",
                "message": f"补充承包地 {supplementary_parcel_id} 占用永久基本农田",
            })
        if reasons:
            message = "；".join(reason["message"] for reason in reasons)
            with transaction(self.connection, immediate=True):
                self._audit(
                    "withdrawal", withdrawal_id, "withdrawal.rejected", actor_id,
                    {
                        "project_id": project_id,
                        "household_id": household_id,
                        "reasons": reasons,
                    },
                )
            raise ValidationFailed(f"受理校验未通过：{message}")
        checks = {
            "checked_at": self._now(),
            "items": [
                {"code": "permanent_basic_farmland", "result": "pass"},
                {"code": "member_unqualified", "result": "pass"},
                {"code": "reclamation_commitment_expired", "result": "pass"},
                {"code": "authorization_expired", "result": "pass"},
            ],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_withdrawals(withdrawal_id,project_id,household_id,"
                    "homestead_parcel_id,supplementary_parcel_id,state,checks_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,'accepted',?,?,?)",
                    (
                        withdrawal_id,
                        project_id,
                        household_id,
                        homestead_parcel_id,
                        supplementary_parcel_id,
                        canonical_json(checks),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "withdrawal", withdrawal_id, "withdrawal.accepted", actor_id,
                    {"project_id": project_id, "household_id": household_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("退出申请编号冲突") from exc
        return {"withdrawal_id": withdrawal_id, "state": "accepted", "checks": checks["items"]}

    def _withdrawal_row(self, withdrawal_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM linkage_withdrawals WHERE withdrawal_id=?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            raise NotFound("退出申请不存在")
        return row

    # ------------------------------------------------------------------
    # 候选生成（用途管制 + 家庭权益 + 基础设施边界）
    # ------------------------------------------------------------------
    def generate_candidates(self, actor_id: str, withdrawal_id: str, purpose: object) -> dict[str, Any]:
        self._require(actor_id, "candidate.run")
        if purpose not in PURPOSES:
            raise ValidationFailed("purpose 必须是 resettlement 或 contracted-land")
        withdrawal = self._withdrawal_row(withdrawal_id)
        if withdrawal["state"] != "accepted":
            raise InvalidState("退出申请当前不可生成候选")
        today = self._today()
        household_reasons = self._household_check(withdrawal["household_id"], today)
        parcels = self.connection.execute(
            "SELECT * FROM linkage_parcels ORDER BY parcel_id"
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for parcel in parcels:
            version = self._latest_determined_version(parcel["parcel_id"])
            entry: dict[str, Any] = {
                "parcel_id": parcel["parcel_id"],
                "village": parcel["village"],
                "version_no": None if version is None else int(version["version_no"]),
                "land_use": None if version is None else version["land_use"],
                "area_mu": None if version is None else version["area_mu"],
            }
            reasons = self._parcel_check(parcel["parcel_id"], purpose, today, version)
            reasons = reasons + [dict(reason) for reason in household_reasons]
            if reasons:
                excluded.append({**entry, "reasons": reasons})
            else:
                candidates.append(entry)
        result = {
            "withdrawal_id": withdrawal_id,
            "purpose": purpose,
            "evaluated_at": self._now(),
            "household_eligible": not household_reasons,
            "household_reasons": household_reasons,
            "candidates": candidates,
            "excluded": excluded,
        }
        input_sha256 = digest(result)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO linkage_candidate_runs(withdrawal_id,purpose,input_sha256,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (withdrawal_id, purpose, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit(
                "withdrawal", withdrawal_id, "candidates.generated", actor_id,
                {"run_id": run_id, "purpose": purpose, "candidates": len(candidates)},
            )
        return {"run_id": run_id, **result}

    # ------------------------------------------------------------------
    # 安置与土地方案（必须引用确定版本）
    # ------------------------------------------------------------------
    def _pinned_version(self, parcel_id: str, version_id: object) -> sqlite3.Row:
        number = positive_integer(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM linkage_parcel_versions WHERE version_id=?", (number,)
        ).fetchone()
        if row is None or row["parcel_id"] != parcel_id:
            raise NotFound("地块版本不存在")
        if row["state"] != "determined":
            raise Conflict("安置与土地方案必须引用确定版本")
        return row

    def _plan_reference(self, raw: Mapping[str, Any], field: str) -> tuple[str, int]:
        value = raw.get(field)
        if not isinstance(value, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        return (
            identifier(value.get("parcel_id"), f"{field}.parcel_id"),
            positive_integer(value.get("version_id"), f"{field}.version_id"),
        )

    def _plan_eligibility(
        self,
        household_id: str,
        resettlement: tuple[str, sqlite3.Row],
        land: tuple[str, sqlite3.Row],
        today: str,
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        for reason in self._parcel_check(resettlement[0], "resettlement", today, resettlement[1]):
            reasons.append({"phase": "resettlement", **reason})
        for reason in self._parcel_check(land[0], "contracted-land", today, land[1]):
            reasons.append({"phase": "contracted-land", **reason})
        reasons.extend(self._household_check(household_id, today))
        return reasons

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan_id = identifier(raw.get("plan_id"), "plan_id")
        withdrawal_id = identifier(raw.get("withdrawal_id"), "withdrawal_id")
        withdrawal = self._withdrawal_row(withdrawal_id)
        if withdrawal["state"] != "accepted":
            raise InvalidState("退出申请当前不可生成方案")
        r_parcel, r_version_id = self._plan_reference(raw, "resettlement")
        l_parcel, l_version_id = self._plan_reference(raw, "land")
        r_version = self._pinned_version(r_parcel, r_version_id)
        l_version = self._pinned_version(l_parcel, l_version_id)
        today = self._today()
        reasons = self._plan_eligibility(
            withdrawal["household_id"], (r_parcel, r_version), (l_parcel, l_version), today
        )
        if reasons:
            message = "；".join(reason["message"] for reason in reasons)
            raise ValidationFailed(f"方案校验未通过：{message}")
        rule_ids = sorted(
            rule["rule_id"]
            for parcel_id in (r_parcel, l_parcel)
            for rule in self._effective_rules(parcel_id, today)
        )
        protection_sha256 = digest({"parcel_versions": [r_version_id, l_version_id], "rules": rule_ids})
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_plans(plan_id,withdrawal_id,project_id,household_id,"
                    "resettlement_parcel_id,resettlement_version_id,land_parcel_id,land_version_id,"
                    "protection_sha256,state,revision,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'draft',1,?,?)",
                    (
                        plan_id,
                        withdrawal_id,
                        withdrawal["project_id"],
                        withdrawal["household_id"],
                        r_parcel,
                        r_version_id,
                        l_parcel,
                        l_version_id,
                        protection_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "plan", plan_id, "plan.created", actor_id,
                    {
                        "withdrawal_id": withdrawal_id,
                        "resettlement_version_id": r_version_id,
                        "land_version_id": l_version_id,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号冲突") from exc
        return {
            "plan_id": plan_id,
            "withdrawal_id": withdrawal_id,
            "state": "draft",
            "revision": 1,
            "protection_sha256": protection_sha256,
        }

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM linkage_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def _revalidate_plan(self, plan: sqlite3.Row) -> None:
        today = self._today()
        reasons = self._plan_eligibility(
            plan["household_id"],
            (plan["resettlement_parcel_id"], self._pinned_version(
                plan["resettlement_parcel_id"], plan["resettlement_version_id"]
            )),
            (plan["land_parcel_id"], self._pinned_version(
                plan["land_parcel_id"], plan["land_version_id"]
            )),
            today,
        )
        if reasons:
            message = "；".join(reason["message"] for reason in reasons)
            raise InvalidState(f"方案校验未通过：{message}")

    def _transition_plan(
        self,
        actor_id: str,
        plan_id: str,
        expected_revision: object,
        from_state: str,
        to_state: str,
        event_type: str,
        *,
        revalidate: bool,
        timestamp_column: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        revision = positive_integer(expected_revision, "expected_revision")
        plan = self._plan_row(plan_id)
        if revalidate:
            self._revalidate_plan(plan)
        assignments = "state=?,revision=revision+1"
        params: list[Any] = [to_state]
        if timestamp_column is not None:
            assignments += f",{timestamp_column}=?"
            params.append(self._now())
        params.extend([plan_id, from_state, revision])
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE linkage_plans SET {assignments} WHERE plan_id=? AND state=? AND revision=?",
                params,
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前可流转版本")
            self._audit("plan", plan_id, event_type, actor_id, {"from": from_state, "to": to_state})
        return {"plan_id": plan_id, "state": to_state, "revision": revision + 1}

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: object) -> dict[str, Any]:
        return self._transition_plan(
            actor_id, plan_id, expected_revision, "draft", "confirmed", "plan.confirmed",
            revalidate=True, timestamp_column="confirmed_at",
        )

    def start_construction(self, actor_id: str, plan_id: str, expected_revision: object) -> dict[str, Any]:
        return self._transition_plan(
            actor_id, plan_id, expected_revision, "confirmed", "in_construction",
            "plan.construction_started", revalidate=True,
        )

    def deliver_plan(self, actor_id: str, plan_id: str, expected_revision: object) -> dict[str, Any]:
        return self._transition_plan(
            actor_id, plan_id, expected_revision, "in_construction", "delivered", "plan.delivered",
            revalidate=False, timestamp_column="delivered_at",
        )

    # ------------------------------------------------------------------
    # 资格撤回：阻止未确认方案，施工中进入人工处置，合法交付保留
    # ------------------------------------------------------------------
    def withdraw_qualification(self, actor_id: str, member_id: str, reason: object) -> dict[str, Any]:
        self._require(actor_id, "qualification.withdraw")
        note = required_text(reason, "reason")
        member = self.connection.execute(
            "SELECT * FROM linkage_household_members WHERE member_id=?", (member_id,)
        ).fetchone()
        if member is None:
            raise NotFound("家庭成员不存在")
        if not member["qualified"]:
            raise InvalidState("成员资格已撤回")
        now = self._now()
        affected: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE linkage_household_members SET qualified=0,withdrawn_reason=?,withdrawn_at=? "
                "WHERE member_id=? AND qualified=1",
                (note, now, member_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("成员资格已撤回")
            plans = self.connection.execute(
                "SELECT plan_id,state FROM linkage_plans WHERE household_id=? "
                "AND state IN ('draft','confirmed','in_construction') ORDER BY plan_id",
                (member["household_id"],),
            ).fetchall()
            for plan in plans:
                previous = plan["state"]
                new_state = "blocked" if previous == "draft" else "manual_review"
                state_reason = f"家庭成员 {member['name']} 资格撤回：{note}"
                self.connection.execute(
                    "UPDATE linkage_plans SET state=?,state_reason=?,revision=revision+1 "
                    "WHERE plan_id=? AND state=?",
                    (new_state, state_reason, plan["plan_id"], previous),
                )
                self._audit(
                    "plan", plan["plan_id"], f"plan.{new_state}", actor_id,
                    {"member_id": member_id, "previous_state": previous, "reason": note},
                )
                affected.append({
                    "plan_id": plan["plan_id"],
                    "previous_state": previous,
                    "state": new_state,
                })
            self._audit(
                "member", member_id, "member.qualification_withdrawn", actor_id,
                {
                    "household_id": member["household_id"],
                    "reason": note,
                    "affected_plans": [item["plan_id"] for item in affected],
                },
            )
        return {
            "member_id": member_id,
            "household_id": member["household_id"],
            "qualified": False,
            "affected_plans": affected,
        }

    # ------------------------------------------------------------------
    # 角色视图（最小必要原则）
    # ------------------------------------------------------------------
    def _reader_role(self, actor_id: str, household_id: str) -> str:
        user = self._user(actor_id)
        role = user["role"]
        if role == "household":
            link = self.connection.execute(
                "SELECT household_id FROM linkage_household_users WHERE user_id=?", (actor_id,)
            ).fetchone()
            if link is None or link["household_id"] != household_id:
                raise Forbidden("家庭账号只能查看本家庭数据")
            return "household"
        if "linkage.read" not in ROLE_PERMISSIONS.get(role, set()):
            raise Forbidden(f"角色 {role} 无权查看联动业务数据")
        return role

    def withdrawal_view(self, actor_id: str, withdrawal_id: str) -> dict[str, Any]:
        row = self._withdrawal_row(withdrawal_id)
        role = self._reader_role(actor_id, row["household_id"])
        view: dict[str, Any] = {
            "withdrawal_id": row["withdrawal_id"],
            "project_id": row["project_id"],
            "state": row["state"],
            "homestead_parcel_id": row["homestead_parcel_id"],
            "supplementary_parcel_id": row["supplementary_parcel_id"],
            "checks": json.loads(row["checks_json"])["items"],
            "created_at": row["created_at"],
        }
        if role != "household":
            view["household_id"] = row["household_id"]
            view["created_by"] = row["created_by"]
        return view

    def _version_brief(self, version_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT version_no,land_use,state FROM linkage_parcel_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        return {
            "version_id": version_id,
            "version_no": None if row is None else int(row["version_no"]),
            "land_use": None if row is None else row["land_use"],
            "state": None if row is None else row["state"],
        }

    def plan_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        role = self._reader_role(actor_id, row["household_id"])
        view: dict[str, Any] = {
            "plan_id": row["plan_id"],
            "withdrawal_id": row["withdrawal_id"],
            "project_id": row["project_id"],
            "state": row["state"],
            "state_reason": row["state_reason"],
            "resettlement": {
                "parcel_id": row["resettlement_parcel_id"],
                **self._version_brief(int(row["resettlement_version_id"])),
            },
            "land": {
                "parcel_id": row["land_parcel_id"],
                **self._version_brief(int(row["land_version_id"])),
            },
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
            "delivered_at": row["delivered_at"],
        }
        if role != "household":
            view["household_id"] = row["household_id"]
            view["revision"] = int(row["revision"])
            view["protection_sha256"] = row["protection_sha256"]
            view["created_by"] = row["created_by"]
        return view

    def candidate_run_view(self, actor_id: str, run_id: object) -> dict[str, Any]:
        number = positive_integer(run_id, "run_id")
        row = self.connection.execute(
            "SELECT * FROM linkage_candidate_runs WHERE run_id=?", (number,)
        ).fetchone()
        if row is None:
            raise NotFound("候选运行不存在")
        withdrawal = self._withdrawal_row(row["withdrawal_id"])
        role = self._reader_role(actor_id, withdrawal["household_id"])
        result = json.loads(row["result_json"])
        view: dict[str, Any] = {"run_id": int(row["run_id"]), **result}
        if role != "household":
            view["input_sha256"] = row["input_sha256"]
            view["created_by"] = row["created_by"]
        return view

    def household_view(self, actor_id: str, household_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM linkage_households WHERE household_id=?", (household_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭不存在")
        role = self._reader_role(actor_id, household_id)
        members = self.connection.execute(
            "SELECT * FROM linkage_household_members WHERE household_id=? ORDER BY member_id",
            (household_id,),
        ).fetchall()
        member_views: list[dict[str, Any]] = []
        for member in members:
            item: dict[str, Any] = {
                "member_id": member["member_id"],
                "name": member["name"],
                "qualified": bool(member["qualified"]),
            }
            if role != "household":
                item["withdrawn_reason"] = member["withdrawn_reason"]
                item["withdrawn_at"] = member["withdrawn_at"]
            member_views.append(item)
        view: dict[str, Any] = {
            "household_id": row["household_id"],
            "head_name": row["head_name"],
            "village": row["village"],
            "authorized_until": row["authorized_until"],
            "reclamation_commitment_deadline": row["reclamation_commitment_deadline"],
            "members": member_views,
        }
        if role != "household":
            view["authorized_scope"] = row["authorized_scope"]
            view["commitment_note"] = row["commitment_note"]
            view["created_by"] = row["created_by"]
            view["created_at"] = row["created_at"]
        return view
