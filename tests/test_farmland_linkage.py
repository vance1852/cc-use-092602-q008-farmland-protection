from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from farmland_linkage.api import JsonApplication
from farmland_linkage.clock import FrozenClock
from farmland_linkage.eligibility import (
    FAMILY_RIGHTS_HELD,
    FAMILY_RIGHTS_PINNED,
    INFRASTRUCTURE_BOUNDARY,
    PERMANENT_BASIC_FARMLAND,
    USE_CONTROL_MISMATCH,
    USE_CONTROL_RULE,
    ParcelSnapshot,
    RuleSnapshot,
    evaluate_parcel,
    generate_candidates,
)
from farmland_linkage.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from farmland_linkage.service import LinkageService


def snapshot(parcel_id: str, **overrides: object) -> ParcelSnapshot:
    base = {
        "parcel_version_id": 1,
        "village_id": "village-north",
        "land_use": "cultivated",
        "permanent_basic_farmland": False,
        "within_infrastructure_boundary": False,
        "current_holder_household_id": None,
    }
    base.update(overrides)
    return ParcelSnapshot(parcel_id=parcel_id, **base)


class EligibilityTests(unittest.TestCase):
    def test_eligible_parcel_has_no_reasons(self) -> None:
        reasons = evaluate_parcel(snapshot("p1"), (), "contracted-supplement", "hh-1", ())
        self.assertEqual(reasons, [])

    def test_permanent_basic_farmland_flag_excludes(self) -> None:
        reasons = evaluate_parcel(
            snapshot("p1", permanent_basic_farmland=True), (), "contracted-supplement", "hh-1", ()
        )
        self.assertEqual([reason["rule_code"] for reason in reasons], [PERMANENT_BASIC_FARMLAND])
        self.assertEqual(reasons[0]["basis"]["parcel_version_id"], 1)

    def test_use_control_mismatch_and_rule_exclude(self) -> None:
        parcel = snapshot("p1", land_use="forest")
        reasons = evaluate_parcel(parcel, (), "contracted-supplement", "hh-1", ())
        self.assertEqual([reason["rule_code"] for reason in reasons], [USE_CONTROL_MISMATCH])
        rule = RuleSnapshot("rule-1", "p1", "use_control", ("resettlement",))
        reasons = evaluate_parcel(snapshot("p1"), [rule], "contracted-supplement", "hh-1", ())
        self.assertEqual([reason["rule_code"] for reason in reasons], [USE_CONTROL_RULE])
        self.assertEqual(reasons[0]["basis"]["rule_id"], "rule-1")

    def test_infrastructure_and_family_rights_exclude(self) -> None:
        parcel = snapshot("p1", within_infrastructure_boundary=True, current_holder_household_id="hh-2")
        reasons = evaluate_parcel(parcel, (), "resettlement", "hh-1", ("p1",))
        codes = [reason["rule_code"] for reason in reasons]
        self.assertIn(INFRASTRUCTURE_BOUNDARY, codes)
        self.assertIn(FAMILY_RIGHTS_HELD, codes)
        self.assertIn(FAMILY_RIGHTS_PINNED, codes)

    def test_generate_candidates_is_sorted_and_split(self) -> None:
        result = generate_candidates(
            [snapshot("p2", permanent_basic_farmland=True), snapshot("p1"), snapshot("p3")],
            {},
            "contracted-supplement",
            "hh-1",
            (),
        )
        self.assertEqual([row["parcel_id"] for row in result["eligible"]], ["p1", "p3"])
        self.assertEqual([row["parcel_id"] for row in result["excluded"]], ["p2"])
        self.assertEqual(result["excluded"][0]["reasons"][0]["rule_code"], PERMANENT_BASIC_FARMLAND)


class LinkageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = LinkageService(self.connection, self.clock)
        self.service.create_user("registrar", "登记员", "registrar")
        self.service.create_user("officer", "经办人", "officer")
        self.service.create_user("auditor", "审计员", "auditor")
        self.parcel("homestead-1", "homestead", holder="hh-east")
        self.parcel("supplement-good", "cultivated")
        self.parcel("supplement-farmland", "cultivated", permanent_basic_farmland=True)
        self.parcel("supplement-infra", "reserve", within_infrastructure_boundary=True)
        self.parcel("supplement-held", "cultivated", holder="hh-other")
        self.household("hh-east", [("m1", "王东部"), ("m2", "王妻")])
        self.service.create_user("hh-east-user", "王东部", "household", "hh-east")
        self.service.create_authorization("registrar", {
            "authorization_id": "auth-1",
            "household_id": "hh-east",
            "homestead_parcel_id": "homestead-1",
            "consented_member_ids": ["m1", "m2"],
            "valid_until": "2027-12-31",
        })
        self.service.create_commitment("registrar", {
            "commitment_id": "com-1",
            "household_id": "hh-east",
            "parcel_id": "homestead-1",
            "responsible_party": "县土地整理中心",
            "promised_deadline": "2027-06-30",
        })
        self.service.create_project("officer", {"project_id": "proj-1", "name": "联动整治", "village_ids": ["village-north"]})

    def tearDown(self) -> None:
        self.connection.close()

    def parcel(self, parcel_id: str, land_use: str, holder: str | None = None, **flags: bool) -> dict[str, object]:
        return self.service.register_parcel_version("registrar", {
            "parcel_id": parcel_id,
            "village_id": "village-north",
            "land_use": land_use,
            "permanent_basic_farmland": flags.get("permanent_basic_farmland", False),
            "within_infrastructure_boundary": flags.get("within_infrastructure_boundary", False),
            "area_mu": "6.5",
            "current_holder_household_id": holder,
            "note": "测试登记",
        })

    def household(self, household_id: str, members: list[tuple[str, str]]) -> None:
        self.service.create_household("registrar", {
            "household_id": household_id,
            "village_id": "village-north",
            "head_name": members[0][1],
            "members": [{"member_id": member_id, "name": name} for member_id, name in members],
        })

    def accept(self, supplement_parcel_id: str = "supplement-good", **overrides: object) -> dict[str, object]:
        payload = {
            "application_id": "app-1",
            "project_id": "proj-1",
            "household_id": "hh-east",
            "homestead_parcel_id": "homestead-1",
            "supplement_parcel_id": supplement_parcel_id,
            "authorization_id": "auth-1",
            "commitment_id": "com-1",
            "idempotency_key": "app-key-1",
        }
        payload.update(overrides)
        return self.service.accept_application("officer", payload)

    def plan(self) -> dict[str, object]:
        return self.service.create_plan("officer", {
            "plan_id": "plan-1",
            "application_id": "app-1",
            "purpose": "contracted-supplement",
            "idempotency_key": "plan-key-1",
        })

    # ------------------------------------------------------------------
    # 登记与批量导入
    # ------------------------------------------------------------------

    def test_parcel_versions_increment(self) -> None:
        first = self.parcel("parcel-x", "cultivated")
        second = self.parcel("parcel-x", "reserve")
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertNotEqual(first["parcel_version_id"], second["parcel_version_id"])

    def test_rule_import_is_all_or_nothing(self) -> None:
        batch = {
            "batch_id": "batch-bad",
            "rules": [
                {"rule_id": "rule-ok", "parcel_id": "supplement-good", "scope_type": "infrastructure_boundary"},
                {"rule_id": "rule-bad", "parcel_id": "ghost-parcel", "scope_type": "infrastructure_boundary"},
            ],
        }
        with self.assertRaises(ValidationFailed):
            self.service.import_protection_rules("registrar", batch)
        rules = self.connection.execute("SELECT COUNT(*) AS c FROM protection_rules").fetchone()["c"]
        batches = self.connection.execute("SELECT COUNT(*) AS c FROM protection_rule_batches").fetchone()["c"]
        self.assertEqual((rules, batches), (0, 0))

    def test_rule_import_replay_is_stable_and_conflict_on_change(self) -> None:
        batch = {
            "batch_id": "batch-1",
            "rules": [{"rule_id": "rule-1", "parcel_id": "supplement-good", "scope_type": "infrastructure_boundary"}],
        }
        first = self.service.import_protection_rules("registrar", batch)
        second = self.service.import_protection_rules("registrar", batch)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["rule_ids"], second["rule_ids"])
        changed = {"batch_id": "batch-1", "rules": [{"rule_id": "rule-2", "parcel_id": "supplement-good", "scope_type": "use_control", "allowed_purposes": ["resettlement"]}]}
        with self.assertRaises(Conflict):
            self.service.import_protection_rules("registrar", changed)

    def test_protection_rule_excludes_parcel_from_candidates(self) -> None:
        self.parcel("supplement-ruled", "cultivated")
        self.service.import_protection_rules("registrar", {
            "batch_id": "batch-1",
            "rules": [{"rule_id": "rule-uc", "parcel_id": "supplement-ruled", "scope_type": "use_control", "allowed_purposes": ["resettlement"]}],
        })
        self.accept()
        self.plan()
        view = self.service.plan_view("officer", "plan-1")
        excluded = {row["parcel_id"]: row["reasons"] for row in view["candidates"]["excluded"]}
        self.assertEqual(excluded["supplement-ruled"][0]["rule_code"], USE_CONTROL_RULE)
        self.assertEqual(excluded["supplement-ruled"][0]["basis"]["rule_id"], "rule-uc")

    def test_acceptance_rejects_supplement_excluded_by_rule(self) -> None:
        self.service.import_protection_rules("registrar", {
            "batch_id": "batch-1",
            "rules": [{"rule_id": "rule-uc", "parcel_id": "supplement-good", "scope_type": "use_control", "allowed_purposes": ["resettlement"]}],
        })
        with self.assertRaises(Conflict) as ctx:
            self.accept()
        self.assertEqual(ctx.exception.details["reasons"][0]["rule_code"], USE_CONTROL_RULE)

    # ------------------------------------------------------------------
    # 受理联动校验
    # ------------------------------------------------------------------

    def test_acceptance_rejects_supplement_on_permanent_basic_farmland(self) -> None:
        with self.assertRaises(Conflict) as ctx:
            self.accept("supplement-farmland")
        codes = [reason["rule_code"] for reason in ctx.exception.details["reasons"]]
        self.assertIn(PERMANENT_BASIC_FARMLAND, codes)

    def test_acceptance_requires_member_eligibility(self) -> None:
        self.service.withdraw_member_eligibility("registrar", "hh-east", "m2", "资格复核不通过")
        with self.assertRaises(InvalidState):
            self.accept()

    def test_acceptance_requires_commitment_within_deadline(self) -> None:
        self.service.create_commitment("registrar", {
            "commitment_id": "com-expired",
            "household_id": "hh-east",
            "parcel_id": "homestead-1",
            "responsible_party": "县土地整理中心",
            "promised_deadline": "2026-09-01",
        })
        with self.assertRaises(InvalidState):
            self.accept(commitment_id="com-expired", idempotency_key="app-key-2", application_id="app-2")

    def test_acceptance_requires_valid_authorization(self) -> None:
        self.service.create_authorization("registrar", {
            "authorization_id": "auth-expired",
            "household_id": "hh-east",
            "homestead_parcel_id": "homestead-1",
            "consented_member_ids": ["m1", "m2"],
            "valid_until": "2026-09-01",
        })
        with self.assertRaises(InvalidState):
            self.accept(authorization_id="auth-expired", idempotency_key="app-key-2", application_id="app-2")

    def test_application_replay_and_payload_conflict(self) -> None:
        first = self.accept()
        self.assertEqual(first, self.accept())
        with self.assertRaises(Conflict):
            self.accept(supplement_parcel_id="supplement-infra")

    # ------------------------------------------------------------------
    # 方案：确定版本与候选边界
    # ------------------------------------------------------------------

    def test_plan_pins_versions_registered_at_acceptance(self) -> None:
        accepted = self.accept()
        self.parcel("supplement-good", "cultivated")
        created = self.plan()
        view = self.service.plan_view("officer", "plan-1")
        self.assertEqual(view["supplement_parcel"]["parcel_version_id"], accepted["supplement_parcel_version_id"])
        self.assertEqual(view["supplement_parcel"]["version"], 1)
        self.assertEqual(created["state"], "draft")

    def test_plan_creation_fails_loudly_when_pinned_parcel_loses_eligibility(self) -> None:
        self.accept()
        self.parcel("supplement-good", "cultivated", permanent_basic_farmland=True)
        with self.assertRaises(Conflict) as ctx:
            self.plan()
        codes = [reason["rule_code"] for reason in ctx.exception.details["reasons"]]
        self.assertIn(PERMANENT_BASIC_FARMLAND, codes)

    def test_candidates_respect_three_boundaries_with_explanations(self) -> None:
        self.accept()
        self.plan()
        view = self.service.plan_view("officer", "plan-1")
        excluded = {row["parcel_id"]: row["reasons"] for row in view["candidates"]["excluded"]}
        self.assertEqual([row["parcel_id"] for row in view["candidates"]["eligible"]], ["supplement-good"])
        self.assertEqual(excluded["supplement-farmland"][0]["rule_code"], PERMANENT_BASIC_FARMLAND)
        self.assertEqual(excluded["supplement-infra"][0]["rule_code"], INFRASTRUCTURE_BOUNDARY)
        self.assertEqual(excluded["supplement-held"][0]["rule_code"], FAMILY_RIGHTS_HELD)
        explanation = self.service.parcel_explanation("officer", "plan-1", "supplement-farmland")
        self.assertFalse(explanation["eligible"])
        self.assertTrue(explanation["registered_version"]["permanent_basic_farmland"])

    def test_plan_creation_is_idempotent(self) -> None:
        self.accept()
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        rows = self.connection.execute("SELECT COUNT(*) AS c FROM linkage_plans").fetchone()["c"]
        self.assertEqual(rows, 1)

    # ------------------------------------------------------------------
    # 资格撤回级联
    # ------------------------------------------------------------------

    def test_withdrawal_blocks_draft_and_preserves_pinned_versions(self) -> None:
        self.accept()
        self.plan()
        before = self.service.plan_view("officer", "plan-1")
        result = self.service.withdraw_member_eligibility("registrar", "hh-east", "m2", "户籍迁出")
        self.assertEqual(result["blocked_plans"], ["plan-1"])
        after = self.service.plan_view("officer", "plan-1")
        self.assertEqual(after["state"], "blocked")
        self.assertEqual(after["supplement_parcel"], before["supplement_parcel"])
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("officer", "plan-1", 1)

    def test_withdrawal_holds_confirmed_and_construction_project(self) -> None:
        self.accept()
        self.plan()
        self.service.confirm_plan("officer", "plan-1", 1)
        self.service.start_construction("officer", "proj-1", 1)
        result = self.service.withdraw_member_eligibility("registrar", "hh-east", "m1", "资格复核不通过")
        self.assertEqual(result["manual_review_plans"], ["plan-1"])
        self.assertEqual(result["manual_review_projects"], ["proj-1"])
        with self.assertRaises(InvalidState):
            self.service.deliver_plan("officer", "plan-1", 2)
        resolved = self.service.resolve_plan("officer", "plan-1", "deliver", "人工核查后按原方案交付", 3)
        self.assertEqual(resolved["state"], "delivered")
        project = self.service.resolve_project("officer", "proj-1", "resume", "人工核查后恢复施工", 3)
        self.assertEqual(project["state"], "in_construction")

    def test_withdrawal_never_touches_delivered_plans(self) -> None:
        self.accept()
        self.plan()
        self.service.confirm_plan("officer", "plan-1", 1)
        self.service.deliver_plan("officer", "plan-1", 2)
        result = self.service.withdraw_member_eligibility("registrar", "hh-east", "m1", "资格复核不通过")
        self.assertEqual(result["manual_review_plans"], [])
        self.assertEqual(self.service.plan_view("officer", "plan-1")["state"], "delivered")

    # ------------------------------------------------------------------
    # 最小必要视图与审计
    # ------------------------------------------------------------------

    def test_views_follow_least_necessity(self) -> None:
        self.household("hh-west", [("w1", "李西部")])
        self.service.create_user("hh-west-user", "李西部", "household", "hh-west")
        self.accept()
        self.plan()
        own = self.service.plan_view("hh-east-user", "plan-1")
        self.assertNotIn("created_by", own)
        self.assertNotIn("household", own)
        self.assertIn("excluded", own["candidates"])
        with self.assertRaises(Forbidden):
            self.service.plan_view("hh-west-user", "plan-1")
        officer_view = self.service.plan_view("officer", "plan-1")
        self.assertEqual(officer_view["household"]["head_name"], "王东部")
        auditor_view = self.service.plan_view("auditor", "plan-1")
        self.assertEqual(auditor_view["household"]["head_name"], "***")
        self.assertEqual(auditor_view["household"]["members"][0]["name"], "***")
        with self.assertRaises(Forbidden):
            self.service.household_view("hh-east-user", "hh-west")

    def test_household_user_must_bind_existing_household(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_user("stray", "无绑定用户", "household")
        with self.assertRaises(NotFound):
            self.service.create_user("ghost", "幽灵", "household", "hh-ghost")

    def test_audit_chain_detects_tampering(self) -> None:
        self.accept()
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        self.connection.execute("UPDATE linkage_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])
        with self.assertRaises(Forbidden):
            self.service.audit_chain("officer")

    # ------------------------------------------------------------------
    # API 边界
    # ------------------------------------------------------------------

    def test_api_exposes_linkage_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/plans/plan-1")
        self.assertEqual(missing_actor.status, 422)
        response = app.handle("POST", "/applications", {"X-Actor-Id": "officer"}, b'{"application_id":"app-9","project_id":"proj-1","household_id":"hh-east","homestead_parcel_id":"homestead-1","supplement_parcel_id":"supplement-farmland","authorization_id":"auth-1","commitment_id":"com-1","idempotency_key":"app-key-9"}')
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["details"]["reasons"][0]["rule_code"], PERMANENT_BASIC_FARMLAND)
        unknown = app.handle("GET", "/nope", {"X-Actor-Id": "officer"})
        self.assertEqual(unknown.status, 404)

    def test_api_serves_exclusion_explanation(self) -> None:
        app = JsonApplication(self.service)
        self.accept()
        self.plan()
        response = app.handle("GET", "/plans/plan-1/exclusions/supplement-farmland", {"X-Actor-Id": "hh-east-user"})
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["eligible"])
        self.assertEqual(response.body["reasons"][0]["rule_code"], PERMANENT_BASIC_FARMLAND)
        forbidden = app.handle("GET", "/plans/plan-1", {"X-Actor-Id": "auditor"})
        self.assertEqual(forbidden.status, 200)
        self.assertEqual(forbidden.body["household"]["head_name"], "***")


if __name__ == "__main__":
    unittest.main()
