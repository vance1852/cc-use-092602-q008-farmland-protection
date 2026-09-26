"""耕地保护与宅基地退出联动的事务用例：登记、受理、方案、资格撤回与角色视图。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .eligibility import ParcelSnapshot, RuleSnapshot, evaluate_parcel, generate_candidates
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ApplicationInput,
    AuthorizationInput,
    CommitmentInput,
    HouseholdInput,
    ParcelVersionInput,
    PlanInput,
    ProjectInput,
    RuleBatchImport,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "registrar": {
        "parcel.register",
        "rule.import",
        "household.write",
        "authorization.write",
        "commitment.write",
        "eligibility.write",
    },
    "officer": {"project.write", "application.write", "plan.write"},
    "household": {"view.household"},
    "auditor": {"audit.read"},
}

NON_TERMINAL_PLAN_STATES = ("draft", "confirmed", "manual_review", "delivered")
MASKED = "***"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


class LinkageService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().astimezone(timezone.utc).date().isoformat()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM linkage_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM linkage_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO linkage_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent_get(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM linkage_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(row["response_json"])

    def _idempotent_put(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO linkage_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------
    # 用户与登记
    # ------------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        display_name: str,
        role: str,
        household_id: str | None = None,
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "household":
            if household_id is None:
                raise ValidationFailed("家庭角色必须绑定家庭主体")
            self._household(household_id)
        elif household_id is not None:
            raise ValidationFailed("只有家庭角色可以绑定家庭主体")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_users(user_id,display_name,role,household_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, household_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_parcel_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "parcel.register")
        parcel = ParcelVersionInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT OR IGNORE INTO parcels(parcel_id,created_at) VALUES(?,?)",
                (parcel.parcel_id, self._now()),
            )
            row = self.connection.execute(
                "SELECT MAX(version) AS max_version FROM parcel_versions WHERE parcel_id=?",
                (parcel.parcel_id,),
            ).fetchone()
            version = (row["max_version"] or 0) + 1
            cursor = self.connection.execute(
                "INSERT INTO parcel_versions(parcel_id,version,village_id,land_use,permanent_basic_farmland,"
                "within_infrastructure_boundary,area_mu,current_holder_household_id,note,"
                "registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    parcel.parcel_id,
                    version,
                    parcel.village_id,
                    parcel.land_use,
                    int(parcel.permanent_basic_farmland),
                    int(parcel.within_infrastructure_boundary),
                    decimal_text(parcel.area_mu),
                    parcel.current_holder_household_id,
                    parcel.note,
                    actor_id,
                    self._now(),
                ),
            )
            parcel_version_id = int(cursor.lastrowid)
            self._audit(
                "parcel",
                parcel.parcel_id,
                "parcel.version_registered",
                actor_id,
                {"parcel_version_id": parcel_version_id, "version": version},
            )
        return {"parcel_id": parcel.parcel_id, "version": version, "parcel_version_id": parcel_version_id}

    def import_protection_rules(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """批量导入保护规则：全部校验通过才落库，重复提交返回首次的稳定结果。"""
        self._require(actor_id, "rule.import")
        batch = RuleBatchImport.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM protection_rule_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("批次号对应不同规则内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        parcel_ids = sorted({rule.parcel_id for rule in batch.rules})
        missing = [
            parcel_id
            for parcel_id in parcel_ids
            if self.connection.execute(
                "SELECT 1 FROM parcels WHERE parcel_id=?", (parcel_id,)
            ).fetchone()
            is None
        ]
        if missing:
            raise ValidationFailed(
                "保护规则引用的地块未登记",
                details={"missing_parcel_ids": missing},
            )
        response = {
            "batch_id": batch.batch_id,
            "imported": len(batch.rules),
            "rule_ids": [rule.rule_id for rule in batch.rules],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO protection_rule_batches(batch_id,request_sha256,response_json,rule_count,"
                    "imported_by,imported_at) VALUES(?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        request_digest,
                        canonical_json(response),
                        len(batch.rules),
                        actor_id,
                        self._now(),
                    ),
                )
                for rule in batch.rules:
                    self.connection.execute(
                        "INSERT INTO protection_rules(rule_id,batch_id,parcel_id,scope_type,allowed_purposes,"
                        "effective_from,effective_to,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            rule.rule_id,
                            batch.batch_id,
                            rule.parcel_id,
                            rule.scope_type,
                            None if not rule.allowed_purposes else canonical_json(list(rule.allowed_purposes)),
                            rule.effective_from,
                            rule.effective_to,
                            self._now(),
                        ),
                    )
                self._audit(
                    "protection_rule_batch",
                    batch.batch_id,
                    "rules.imported",
                    actor_id,
                    {"rule_count": len(batch.rules)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("保护规则编号或批次冲突") from exc
        return {**response, "replayed": False}

    def create_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household = HouseholdInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO households(household_id,village_id,head_name,created_at) VALUES(?,?,?,?)",
                    (household.household_id, household.village_id, household.head_name, self._now()),
                )
                for member_id, name in household.members:
                    self.connection.execute(
                        "INSERT INTO household_members(household_id,member_id,name,eligible,updated_at) "
                        "VALUES(?,?,?,1,?)",
                        (household.household_id, member_id, name, self._now()),
                    )
                self._audit(
                    "household",
                    household.household_id,
                    "household.registered",
                    actor_id,
                    {"members": len(household.members)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭主体已经存在") from exc
        return {"household_id": household.household_id, "members": len(household.members)}

    def create_authorization(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "authorization.write")
        authorization = AuthorizationInput.from_dict(raw)
        self._household(authorization.household_id)
        self._parcel(authorization.homestead_parcel_id)
        known = {
            row["member_id"]
            for row in self.connection.execute(
                "SELECT member_id FROM household_members WHERE household_id=?",
                (authorization.household_id,),
            ).fetchall()
        }
        unknown = [member_id for member_id in authorization.consented_member_ids if member_id not in known]
        if unknown:
            raise ValidationFailed("授权成员不在家庭名册", details={"unknown_member_ids": unknown})
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO household_authorizations(authorization_id,household_id,homestead_parcel_id,"
                    "consented_member_ids,valid_until,status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                    (
                        authorization.authorization_id,
                        authorization.household_id,
                        authorization.homestead_parcel_id,
                        canonical_json(list(authorization.consented_member_ids)),
                        authorization.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "authorization",
                    authorization.authorization_id,
                    "authorization.created",
                    actor_id,
                    {"household_id": authorization.household_id, "valid_until": authorization.valid_until},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号已经存在") from exc
        return {"authorization_id": authorization.authorization_id, "status": "active"}

    def create_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        commitment = CommitmentInput.from_dict(raw)
        self._household(commitment.household_id)
        self._parcel(commitment.parcel_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reclamation_commitments(commitment_id,household_id,parcel_id,"
                    "responsible_party,promised_deadline,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,'pending',?,?)",
                    (
                        commitment.commitment_id,
                        commitment.household_id,
                        commitment.parcel_id,
                        commitment.responsible_party,
                        commitment.promised_deadline,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "commitment",
                    commitment.commitment_id,
                    "commitment.created",
                    actor_id,
                    {"household_id": commitment.household_id, "promised_deadline": commitment.promised_deadline},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("复垦承诺编号已经存在") from exc
        return {"commitment_id": commitment.commitment_id, "status": "pending"}

    # ------------------------------------------------------------------
    # 项目
    # ------------------------------------------------------------------

    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = ProjectInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO remediation_projects(project_id,name,village_ids,state,created_by,created_at) "
                    "VALUES(?,?,?,'accepting',?,?)",
                    (
                        project.project_id,
                        project.name,
                        canonical_json(list(project.village_ids)),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("project", project.project_id, "project.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号已经存在") from exc
        return {"project_id": project.project_id, "state": "accepting", "revision": 1}

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM remediation_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("跨村整治项目不存在")
        return row

    def start_construction(self, actor_id: str, project_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        self._project(project_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE remediation_projects SET state='in_construction',revision=revision+1 "
                "WHERE project_id=? AND state='accepting' AND revision=?",
                (project_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("项目不是当前受理版本")
            self._audit("project", project_id, "project.construction_started", actor_id, {})
        return {"project_id": project_id, "state": "in_construction", "revision": expected_revision + 1}

    def resolve_project(
        self,
        actor_id: str,
        project_id: str,
        action: str,
        note: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """人工处置施工中被资格撤回波及的项目：恢复施工或暂停，不做静默迁移。"""
        self._require(actor_id, "project.write")
        self._project(project_id)
        note = required_text(note, "note")
        transitions = {"resume": "in_construction", "suspend": "suspended"}
        if action not in transitions:
            raise ValidationFailed("action 必须是 resume 或 suspend")
        new_state = transitions[action]
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE remediation_projects SET state=?,revision=revision+1 "
                "WHERE project_id=? AND state='manual_review' AND revision=?",
                (new_state, project_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("项目不在人工处置版本")
            self._audit("project", project_id, "project.resolved", actor_id, {"action": action, "note": note})
        return {"project_id": project_id, "state": new_state, "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 宅基地退出受理（联动校验）
    # ------------------------------------------------------------------

    def _household(self, household_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM households WHERE household_id=?", (household_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭主体不存在")
        return row

    def _parcel(self, parcel_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,)
        ).fetchone()
        if row is None:
            raise NotFound("地块不存在")
        return row

    def _latest_parcel_version(self, parcel_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_id=? ORDER BY version DESC LIMIT 1",
            (parcel_id,),
        ).fetchone()
        if row is None:
            raise NotFound("地块没有登记版本")
        return row

    def _members(self, household_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM household_members WHERE household_id=? ORDER BY member_id",
            (household_id,),
        ).fetchall()

    def _active_rules_by_parcel(self, parcel_ids: Sequence[str], today: str) -> dict[str, list[RuleSnapshot]]:
        result: dict[str, list[RuleSnapshot]] = {}
        if not parcel_ids:
            return result
        placeholders = ",".join("?" for _ in parcel_ids)
        rows = self.connection.execute(
            f"SELECT * FROM protection_rules WHERE parcel_id IN ({placeholders}) "
            "AND (effective_from IS NULL OR effective_from<=?) "
            "AND (effective_to IS NULL OR effective_to>=?) ORDER BY rule_id",
            (*parcel_ids, today, today),
        ).fetchall()
        for row in rows:
            snapshot = RuleSnapshot(
                rule_id=row["rule_id"],
                parcel_id=row["parcel_id"],
                scope_type=row["scope_type"],
                allowed_purposes=tuple(json.loads(row["allowed_purposes"])) if row["allowed_purposes"] else (),
            )
            result.setdefault(row["parcel_id"], []).append(snapshot)
        return result

    def _pinned_parcel_ids(self, exclude_plan_id: str | None = None) -> set[str]:
        """未终结方案（含已合法交付）引用为确定版本的地块，候选生成时必须回避。"""
        placeholders = ",".join("?" for _ in NON_TERMINAL_PLAN_STATES)
        rows = self.connection.execute(
            f"SELECT pv.parcel_id FROM linkage_plans p "
            f"JOIN parcel_versions pv ON pv.parcel_version_id=p.supplement_parcel_version_id "
            f"WHERE p.state IN ({placeholders}) AND p.plan_id IS NOT ?",
            (*NON_TERMINAL_PLAN_STATES, exclude_plan_id),
        ).fetchall()
        return {row["parcel_id"] for row in rows}

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ParcelSnapshot:
        return ParcelSnapshot(
            parcel_id=row["parcel_id"],
            parcel_version_id=int(row["parcel_version_id"]),
            village_id=row["village_id"],
            land_use=row["land_use"],
            permanent_basic_farmland=bool(row["permanent_basic_farmland"]),
            within_infrastructure_boundary=bool(row["within_infrastructure_boundary"]),
            current_holder_household_id=row["current_holder_household_id"],
        )

    def _assert_authorization_usable(self, household_id: str, homestead_parcel_id: str, authorization_id: str, today: str) -> None:
        row = self.connection.execute(
            "SELECT * FROM household_authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭主体授权不存在")
        if row["household_id"] != household_id or row["homestead_parcel_id"] != homestead_parcel_id:
            raise Conflict("家庭主体授权与退出宅基地不匹配")
        if row["status"] != "active":
            raise InvalidState("家庭主体授权已撤销")
        if row["valid_until"] < today:
            raise InvalidState("家庭主体授权已过期")
        consented = set(json.loads(row["consented_member_ids"]))
        current = {member["member_id"] for member in self._members(household_id)}
        if not current <= consented:
            raise Conflict("家庭主体授权未覆盖全部家庭成员")

    def _assert_members_eligible(self, household_id: str) -> None:
        ineligible = [member["member_id"] for member in self._members(household_id) if not member["eligible"]]
        if ineligible:
            raise InvalidState(
                "家庭成员资格已撤回，不得继续办理",
                details={"ineligible_member_ids": ineligible},
            )

    def _assert_commitment_usable(self, household_id: str, parcel_id: str, commitment_id: str, today: str) -> None:
        row = self.connection.execute(
            "SELECT * FROM reclamation_commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("复垦责任承诺不存在")
        if row["household_id"] != household_id or row["parcel_id"] != parcel_id:
            raise Conflict("复垦责任承诺与退出宅基地不匹配")
        if row["status"] == "breached":
            raise InvalidState("复垦责任承诺已违约")
        if row["status"] == "pending" and row["promised_deadline"] < today:
            raise InvalidState("复垦责任承诺已过承诺到期时间仍未落实")

    def accept_application(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """受理宅基地退出：同步校验补充承包地保护边界、家庭成员资格与复垦责任。"""
        self._require(actor_id, "application.write")
        application = ApplicationInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self._idempotent_get("application", application.idempotency_key, request_digest)
        if stored is not None:
            return stored
        project = self._project(application.project_id)
        if project["state"] != "accepting":
            raise InvalidState("项目当前不在受理阶段")
        villages = set(json.loads(project["village_ids"]))
        self._household(application.household_id)
        today = self._today()
        self._assert_authorization_usable(
            application.household_id, application.homestead_parcel_id, application.authorization_id, today
        )
        self._assert_members_eligible(application.household_id)
        self._assert_commitment_usable(
            application.household_id, application.homestead_parcel_id, application.commitment_id, today
        )
        homestead_version = self._latest_parcel_version(application.homestead_parcel_id)
        if homestead_version["village_id"] not in villages:
            raise Conflict("退出宅基地不在项目村庄范围内")
        if homestead_version["land_use"] != "homestead":
            raise ValidationFailed("退出地块登记用途管制分类不是宅基地")
        supplement_version = self._latest_parcel_version(application.supplement_parcel_id)
        if supplement_version["village_id"] not in villages:
            raise Conflict("补充分配承包地不在项目村庄范围内")
        rules = self._active_rules_by_parcel([application.supplement_parcel_id], today)
        reasons = evaluate_parcel(
            self._snapshot(supplement_version),
            rules.get(application.supplement_parcel_id, ()),
            "contracted-supplement",
            application.household_id,
            self._pinned_parcel_ids(),
        )
        if reasons:
            raise Conflict("补充分配承包地不满足耕地保护联动校验", details={"reasons": reasons})
        accepted_at = self._now()
        response = {
            "application_id": application.application_id,
            "project_id": application.project_id,
            "household_id": application.household_id,
            "state": "accepted",
            "homestead_parcel_version_id": int(homestead_version["parcel_version_id"]),
            "supplement_parcel_version_id": int(supplement_version["parcel_version_id"]),
            "accepted_at": accepted_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO exit_applications(application_id,project_id,household_id,"
                    "homestead_parcel_version_id,supplement_parcel_version_id,authorization_id,commitment_id,"
                    "state,idempotency_key,accepted_by,accepted_at) VALUES(?,?,?,?,?,?,?,'accepted',?,?,?)",
                    (
                        application.application_id,
                        application.project_id,
                        application.household_id,
                        int(homestead_version["parcel_version_id"]),
                        int(supplement_version["parcel_version_id"]),
                        application.authorization_id,
                        application.commitment_id,
                        application.idempotency_key,
                        actor_id,
                        accepted_at,
                    ),
                )
                self._idempotent_put("application", application.idempotency_key, request_digest, response)
                self._audit(
                    "application",
                    application.application_id,
                    "application.accepted",
                    actor_id,
                    {
                        "household_id": application.household_id,
                        "homestead_parcel_version_id": int(homestead_version["parcel_version_id"]),
                        "supplement_parcel_version_id": int(supplement_version["parcel_version_id"]),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请编号或幂等键冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 安置与土地方案
    # ------------------------------------------------------------------

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM linkage_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """生成安置与土地方案：引用申请钉住的确定版本，只在三重边界内生成候选。"""
        self._require(actor_id, "plan.write")
        plan_input = PlanInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self._idempotent_get("plan", plan_input.idempotency_key, request_digest)
        if stored is not None:
            return stored
        application = self.connection.execute(
            "SELECT * FROM exit_applications WHERE application_id=?", (plan_input.application_id,)
        ).fetchone()
        if application is None:
            raise NotFound("退出申请不存在")
        if application["state"] != "accepted":
            raise InvalidState("退出申请当前不可生成方案")
        active = self.connection.execute(
            "SELECT plan_id FROM linkage_plans WHERE application_id=? AND state IN ('draft','confirmed','manual_review')",
            (plan_input.application_id,),
        ).fetchone()
        if active is not None:
            raise Conflict("申请已存在未终结方案")
        project = self._project(application["project_id"])
        villages = json.loads(project["village_ids"])
        today = self._today()
        homestead_version = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_version_id=?",
            (application["homestead_parcel_version_id"],),
        ).fetchone()
        supplement_version = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_version_id=?",
            (application["supplement_parcel_version_id"],),
        ).fetchone()
        placeholders = ",".join("?" for _ in villages)
        pool_rows = self.connection.execute(
            f"SELECT pv.* FROM parcel_versions pv "
            f"JOIN (SELECT parcel_id,MAX(version) version FROM parcel_versions GROUP BY parcel_id) latest "
            f"ON latest.parcel_id=pv.parcel_id AND latest.version=pv.version "
            f"WHERE pv.village_id IN ({placeholders}) ORDER BY pv.parcel_id",
            tuple(villages),
        ).fetchall()
        snapshots = [
            self._snapshot(row)
            for row in pool_rows
            if row["parcel_id"] != homestead_version["parcel_id"]
        ]
        rules = self._active_rules_by_parcel([item.parcel_id for item in snapshots], today)
        evaluation = generate_candidates(
            snapshots,
            rules,
            plan_input.purpose,
            application["household_id"],
            self._pinned_parcel_ids(),
        )
        eligible_ids = {item["parcel_id"] for item in evaluation["eligible"]}
        supplement_parcel_id = supplement_version["parcel_id"]
        if supplement_parcel_id not in eligible_ids:
            excluded = {item["parcel_id"]: item["reasons"] for item in evaluation["excluded"]}
            raise Conflict(
                "申请钉住的补充分配承包地不再满足候选边界",
                details={"reasons": excluded.get(supplement_parcel_id, [])},
            )
        evaluated_at = self._now()
        response = {
            "plan_id": plan_input.plan_id,
            "application_id": plan_input.application_id,
            "state": "draft",
            "revision": 1,
            "purpose": plan_input.purpose,
            "evaluated_at": evaluated_at,
            "eligible_count": len(evaluation["eligible"]),
            "excluded_count": len(evaluation["excluded"]),
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO linkage_plans(plan_id,application_id,project_id,household_id,purpose,"
                    "homestead_parcel_version_id,supplement_parcel_version_id,state,evaluated_at,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?)",
                    (
                        plan_input.plan_id,
                        plan_input.application_id,
                        application["project_id"],
                        application["household_id"],
                        plan_input.purpose,
                        int(application["homestead_parcel_version_id"]),
                        int(application["supplement_parcel_version_id"]),
                        evaluated_at,
                        plan_input.idempotency_key,
                        actor_id,
                        evaluated_at,
                    ),
                )
                for item in evaluation["eligible"]:
                    self.connection.execute(
                        "INSERT INTO plan_candidates(plan_id,parcel_id,parcel_version_id,eligible,reasons_json) "
                        "VALUES(?,?,?,1,'[]')",
                        (plan_input.plan_id, item["parcel_id"], item["parcel_version_id"]),
                    )
                for item in evaluation["excluded"]:
                    self.connection.execute(
                        "INSERT INTO plan_candidates(plan_id,parcel_id,parcel_version_id,eligible,reasons_json) "
                        "VALUES(?,?,?,0,?)",
                        (plan_input.plan_id, item["parcel_id"], item["parcel_version_id"], canonical_json(item["reasons"])),
                    )
                self._idempotent_put("plan", plan_input.idempotency_key, request_digest, response)
                self._audit(
                    "plan",
                    plan_input.plan_id,
                    "plan.created",
                    actor_id,
                    {
                        "application_id": plan_input.application_id,
                        "eligible_count": len(evaluation["eligible"]),
                        "excluded_count": len(evaluation["excluded"]),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号或幂等键冲突") from exc
        return response

    def _assert_plan_prerequisites(self, plan: sqlite3.Row, today: str) -> None:
        application = self.connection.execute(
            "SELECT * FROM exit_applications WHERE application_id=?", (plan["application_id"],)
        ).fetchone()
        homestead_parcel_id = self.connection.execute(
            "SELECT parcel_id FROM parcel_versions WHERE parcel_version_id=?",
            (application["homestead_parcel_version_id"],),
        ).fetchone()["parcel_id"]
        self._assert_authorization_usable(
            plan["household_id"], homestead_parcel_id, application["authorization_id"], today
        )
        self._assert_members_eligible(plan["household_id"])
        self._assert_commitment_usable(
            plan["household_id"], homestead_parcel_id, application["commitment_id"], today
        )

    def _reevaluate_supplement(self, plan: sqlite3.Row, today: str) -> None:
        version = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_version_id=?",
            (plan["supplement_parcel_version_id"],),
        ).fetchone()
        rules = self._active_rules_by_parcel([version["parcel_id"]], today)
        reasons = evaluate_parcel(
            self._snapshot(version),
            rules.get(version["parcel_id"], ()),
            plan["purpose"],
            plan["household_id"],
            self._pinned_parcel_ids(exclude_plan_id=plan["plan_id"]),
        )
        if reasons:
            raise Conflict("补充分配承包地不再满足候选边界", details={"reasons": reasons})

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = self._plan(plan_id)
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前草稿版本")
        today = self._today()
        self._assert_plan_prerequisites(plan, today)
        self._reevaluate_supplement(plan, today)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE linkage_plans SET state='confirmed',revision=revision+1 "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前草稿版本")
            self._audit("plan", plan_id, "plan.confirmed", actor_id, {})
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    def deliver_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = self._plan(plan_id)
        if plan["state"] != "confirmed" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前已确认版本")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE linkage_plans SET state='delivered',revision=revision+1 "
                "WHERE plan_id=? AND state='confirmed' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前已确认版本")
            self._audit("plan", plan_id, "plan.delivered", actor_id, {})
        return {"plan_id": plan_id, "state": "delivered", "revision": expected_revision + 1}

    def resolve_plan(
        self,
        actor_id: str,
        plan_id: str,
        action: str,
        note: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """人工处置被资格撤回波及的已确认方案：继续交付或取消，保留确定版本不迁移。"""
        self._require(actor_id, "plan.write")
        plan = self._plan(plan_id)
        note = required_text(note, "note")
        transitions = {"deliver": "delivered", "cancel": "cancelled"}
        if action not in transitions:
            raise ValidationFailed("action 必须是 deliver 或 cancel")
        new_state = transitions[action]
        if plan["state"] != "manual_review" or plan["revision"] != expected_revision:
            raise InvalidState("方案不在人工处置版本")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE linkage_plans SET state=?,revision=revision+1 "
                "WHERE plan_id=? AND state='manual_review' AND revision=?",
                (new_state, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不在人工处置版本")
            self._audit("plan", plan_id, "plan.resolved", actor_id, {"action": action, "note": note})
        return {"plan_id": plan_id, "state": new_state, "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 资格撤回级联
    # ------------------------------------------------------------------

    def withdraw_member_eligibility(
        self,
        actor_id: str,
        household_id: str,
        member_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """撤回家庭成员资格：立即阻止未确认方案，施工中项目转人工处置，已交付不动。"""
        self._require(actor_id, "eligibility.write")
        reason = required_text(reason, "reason")
        self._household(household_id)
        member = self.connection.execute(
            "SELECT * FROM household_members WHERE household_id=? AND member_id=?",
            (household_id, member_id),
        ).fetchone()
        if member is None:
            raise NotFound("家庭成员不存在")
        if not member["eligible"]:
            raise InvalidState("家庭成员资格已撤回")
        now = self._now()
        blocked: list[str] = []
        manual_review: list[str] = []
        projects_held: list[str] = []
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE household_members SET eligible=0,ineligible_reason=?,updated_at=? "
                "WHERE household_id=? AND member_id=?",
                (reason, now, household_id, member_id),
            )
            self._audit(
                "member",
                f"{household_id}:{member_id}",
                "member.withdrawn",
                actor_id,
                {"household_id": household_id, "reason": reason},
            )
            plans = self.connection.execute(
                "SELECT plan_id,state FROM linkage_plans WHERE household_id=? AND state IN ('draft','confirmed')",
                (household_id,),
            ).fetchall()
            for plan in plans:
                if plan["state"] == "draft":
                    new_state = "blocked"
                    blocked.append(plan["plan_id"])
                else:
                    new_state = "manual_review"
                    manual_review.append(plan["plan_id"])
                self.connection.execute(
                    "UPDATE linkage_plans SET state=?,revision=revision+1 WHERE plan_id=? AND state=?",
                    (new_state, plan["plan_id"], plan["state"]),
                )
                self._audit(
                    "plan",
                    plan["plan_id"],
                    f"plan.{new_state}",
                    actor_id,
                    {"trigger": "member.withdrawn", "member_id": member_id},
                )
            projects = self.connection.execute(
                "SELECT DISTINCT p.project_id,p.revision FROM remediation_projects p "
                "JOIN linkage_plans lp ON lp.project_id=p.project_id "
                "WHERE lp.household_id=? AND lp.state<>'cancelled' AND p.state='in_construction'",
                (household_id,),
            ).fetchall()
            for project in projects:
                self.connection.execute(
                    "UPDATE remediation_projects SET state='manual_review',revision=revision+1 "
                    "WHERE project_id=? AND state='in_construction'",
                    (project["project_id"],),
                )
                projects_held.append(project["project_id"])
                self._audit(
                    "project",
                    project["project_id"],
                    "project.manual_review",
                    actor_id,
                    {"trigger": "member.withdrawn", "household_id": household_id},
                )
        return {
            "household_id": household_id,
            "member_id": member_id,
            "eligible": False,
            "blocked_plans": blocked,
            "manual_review_plans": manual_review,
            "manual_review_projects": projects_held,
        }

    # ------------------------------------------------------------------
    # 最小必要角色视图
    # ------------------------------------------------------------------

    def _parcel_ref(self, parcel_version_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_version_id=?", (parcel_version_id,)
        ).fetchone()
        return {
            "parcel_id": row["parcel_id"],
            "parcel_version_id": int(row["parcel_version_id"]),
            "version": int(row["version"]),
            "village_id": row["village_id"],
            "land_use": row["land_use"],
        }

    def _candidates_for(self, plan_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM plan_candidates WHERE plan_id=? ORDER BY parcel_id", (plan_id,)
        ).fetchall()
        eligible = [
            {"parcel_id": row["parcel_id"], "parcel_version_id": int(row["parcel_version_id"])}
            for row in rows
            if row["eligible"]
        ]
        excluded = [
            {
                "parcel_id": row["parcel_id"],
                "parcel_version_id": int(row["parcel_version_id"]),
                "reasons": json.loads(row["reasons_json"]),
            }
            for row in rows
            if not row["eligible"]
        ]
        return {"eligible": eligible, "excluded": excluded}

    def _household_brief(self, household_id: str, mask_names: bool) -> dict[str, Any]:
        household = self._household(household_id)
        members = [
            {
                "member_id": member["member_id"],
                "name": MASKED if mask_names else member["name"],
                "eligible": bool(member["eligible"]),
                "ineligible_reason": member["ineligible_reason"],
            }
            for member in self._members(household_id)
        ]
        return {
            "household_id": household_id,
            "village_id": household["village_id"],
            "head_name": MASKED if mask_names else household["head_name"],
            "members": members,
        }

    def plan_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        plan = self._plan(plan_id)
        role = user["role"]
        if role == "household":
            if user["household_id"] != plan["household_id"]:
                raise Forbidden("只能查看本家庭主体的方案")
            return {
                "plan_id": plan["plan_id"],
                "project_id": plan["project_id"],
                "purpose": plan["purpose"],
                "state": plan["state"],
                "evaluated_at": plan["evaluated_at"],
                "homestead_parcel_id": self._parcel_ref(plan["homestead_parcel_version_id"])["parcel_id"],
                "supplement_parcel_id": self._parcel_ref(plan["supplement_parcel_version_id"])["parcel_id"],
                "candidates": self._candidates_for(plan_id),
            }
        if role not in {"officer", "registrar", "auditor"}:
            raise Forbidden("角色无权查看方案")
        return {
            "plan_id": plan["plan_id"],
            "application_id": plan["application_id"],
            "project_id": plan["project_id"],
            "household_id": plan["household_id"],
            "purpose": plan["purpose"],
            "state": plan["state"],
            "revision": int(plan["revision"]),
            "homestead_parcel": self._parcel_ref(plan["homestead_parcel_version_id"]),
            "supplement_parcel": self._parcel_ref(plan["supplement_parcel_version_id"]),
            "evaluated_at": plan["evaluated_at"],
            "created_by": plan["created_by"],
            "created_at": plan["created_at"],
            "candidates": self._candidates_for(plan_id),
            "household": self._household_brief(plan["household_id"], mask_names=role == "auditor"),
        }

    def parcel_explanation(self, actor_id: str, plan_id: str, parcel_id: str) -> dict[str, Any]:
        """解释某个地块在方案候选评估中被排除（或纳入）的具体依据。"""
        user = self._user(actor_id)
        plan = self._plan(plan_id)
        if user["role"] == "household" and user["household_id"] != plan["household_id"]:
            raise Forbidden("只能查看本家庭主体的方案")
        row = self.connection.execute(
            "SELECT * FROM plan_candidates WHERE plan_id=? AND parcel_id=?",
            (plan_id, parcel_id),
        ).fetchone()
        if row is None:
            raise NotFound("该地块不在方案候选评估范围")
        version = self.connection.execute(
            "SELECT * FROM parcel_versions WHERE parcel_version_id=?",
            (row["parcel_version_id"],),
        ).fetchone()
        return {
            "plan_id": plan_id,
            "parcel_id": parcel_id,
            "parcel_version_id": int(row["parcel_version_id"]),
            "eligible": bool(row["eligible"]),
            "reasons": json.loads(row["reasons_json"]),
            "purpose": plan["purpose"],
            "evaluated_at": plan["evaluated_at"],
            "registered_version": {
                "version": int(version["version"]),
                "village_id": version["village_id"],
                "land_use": version["land_use"],
                "permanent_basic_farmland": bool(version["permanent_basic_farmland"]),
                "within_infrastructure_boundary": bool(version["within_infrastructure_boundary"]),
            },
        }

    def household_view(self, actor_id: str, household_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        role = user["role"]
        if role == "household" and user["household_id"] != household_id:
            raise Forbidden("只能查看本家庭主体")
        household = self._household(household_id)
        mask_names = role == "auditor"
        authorizations = [
            {
                "authorization_id": row["authorization_id"],
                "homestead_parcel_id": row["homestead_parcel_id"],
                "valid_until": row["valid_until"],
                "status": row["status"],
            }
            for row in self.connection.execute(
                "SELECT * FROM household_authorizations WHERE household_id=? ORDER BY authorization_id",
                (household_id,),
            ).fetchall()
        ]
        commitments = [
            {
                "commitment_id": row["commitment_id"],
                "parcel_id": row["parcel_id"],
                "responsible_party": row["responsible_party"],
                "promised_deadline": row["promised_deadline"],
                "status": row["status"],
            }
            for row in self.connection.execute(
                "SELECT * FROM reclamation_commitments WHERE household_id=? ORDER BY commitment_id",
                (household_id,),
            ).fetchall()
        ]
        applications = [
            {
                "application_id": row["application_id"],
                "project_id": row["project_id"],
                "state": row["state"],
                "accepted_at": row["accepted_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM exit_applications WHERE household_id=? ORDER BY application_id",
                (household_id,),
            ).fetchall()
        ]
        plans = [
            {
                "plan_id": row["plan_id"],
                "purpose": row["purpose"],
                "state": row["state"],
                "evaluated_at": row["evaluated_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM linkage_plans WHERE household_id=? ORDER BY plan_id",
                (household_id,),
            ).fetchall()
        ]
        return {
            "household_id": household_id,
            "village_id": household["village_id"],
            "head_name": MASKED if mask_names else household["head_name"],
            "members": self._household_brief(household_id, mask_names)["members"],
            "authorizations": authorizations,
            "commitments": commitments,
            "applications": applications,
            "plans": plans,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM linkage_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
