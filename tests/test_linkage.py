from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from rural_allocation.linkage import LinkageService


class LinkageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = LinkageService(self.connection, self.clock)
        for user_id, role in (
            ("county", "natural_resources"),
            ("handler", "handler"),
            ("family", "household"),
            ("family-b", "household"),
            ("audit", "auditor"),
            ("plan", "planner"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_project("county", {"project_id": "proj-1", "name": "跨村整治一期"})
        self.versions = {}
        self.versions["parcel-r1"] = self._parcel("parcel-r1", "construction", True)
        self.versions["parcel-l1"] = self._parcel("parcel-l1", "cultivated", True)
        self.versions["parcel-h1"] = self._parcel("parcel-h1", "homestead", True)
        self.versions["parcel-pbf"] = self._parcel("parcel-pbf", "cultivated", True)
        self.versions["parcel-out"] = self._parcel("parcel-out", "cultivated", False)
        self.versions["parcel-uc"] = self._parcel("parcel-uc", "construction", True)
        self.versions["parcel-draft"] = self._parcel("parcel-draft", "cultivated", True, determine=False)
        self.service.import_protection_rules("county", {
            "batch_id": "batch-1",
            "idempotency_key": "imp-1",
            "rules": [
                {"rule_id": "rule-pbf", "parcel_id": "parcel-pbf", "rule_type": "permanent_basic_farmland", "effective_from": "2026-01-01"},
                {"rule_id": "rule-uc", "parcel_id": "parcel-uc", "rule_type": "use_control", "restricted_use": "construction", "effective_from": "2026-01-01"},
            ],
        })
        self._household("hh-1")
        self.service.link_household_user("handler", {"user_id": "family", "household_id": "hh-1"})

    def tearDown(self) -> None:
        self.connection.close()

    def _parcel(self, parcel_id: str, land_use: str, boundary: bool, determine: bool = True) -> int:
        result = self.service.register_parcel("county", {
            "parcel_id": parcel_id,
            "village": "东村",
            "version": {"land_use": land_use, "area_mu": "12.5", "within_infrastructure_boundary": boundary},
        })
        if determine:
            self.service.determine_parcel_version("county", parcel_id, 1)
        return int(result["version"]["version_id"])

    def _household(self, household_id: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "household_id": household_id,
            "head_name": "王五",
            "village": "东村",
            "authorized_scope": "宅基地退出与安置",
            "authorized_until": "2027-12-31",
            "reclamation_commitment_deadline": "2027-06-30",
            "commitment_note": "退出后三个月内完成复垦",
            "members": [
                {"member_id": f"{household_id}-m1", "name": "王五"},
                {"member_id": f"{household_id}-m2", "name": "李四"},
            ],
        }
        payload.update(overrides)
        return self.service.register_household("handler", payload)

    def _withdrawal(self, withdrawal_id: str = "wd-1", household_id: str = "hh-1", supplementary: str = "parcel-l1") -> dict[str, object]:
        return self.service.accept_withdrawal("handler", {
            "withdrawal_id": withdrawal_id,
            "project_id": "proj-1",
            "household_id": household_id,
            "homestead_parcel_id": "parcel-h1",
            "supplementary_parcel_id": supplementary,
        })

    def _plan(self, plan_id: str = "plan-1", withdrawal_id: str = "wd-1") -> dict[str, object]:
        return self.service.create_plan("handler", {
            "plan_id": plan_id,
            "withdrawal_id": withdrawal_id,
            "resettlement": {"parcel_id": "parcel-r1", "version_id": self.versions["parcel-r1"]},
            "land": {"parcel_id": "parcel-l1", "version_id": self.versions["parcel-l1"]},
        })


class ProtectionImportTests(LinkageTestCase):
    def test_batch_import_is_all_or_nothing(self) -> None:
        payload = {
            "batch_id": "batch-bad",
            "idempotency_key": "imp-bad",
            "rules": [
                {"rule_id": "rule-ok", "parcel_id": "parcel-l1", "rule_type": "permanent_basic_farmland", "effective_from": "2026-01-01"},
                {"rule_id": "rule-missing", "parcel_id": "parcel-void", "rule_type": "use_control", "restricted_use": "cultivated", "effective_from": "2026-01-01"},
            ],
        }
        with self.assertRaises(NotFound):
            self.service.import_protection_rules("county", payload)
        batches = self.connection.execute("SELECT COUNT(*) AS c FROM linkage_protection_batches WHERE batch_id='batch-bad'").fetchone()
        rules = self.connection.execute("SELECT COUNT(*) AS c FROM linkage_protection_rules WHERE rule_id IN ('rule-ok','rule-missing')").fetchone()
        self.assertEqual(batches["c"], 0)
        self.assertEqual(rules["c"], 0)

    def test_batch_import_validates_every_rule_before_writing(self) -> None:
        payload = {
            "batch_id": "batch-invalid",
            "idempotency_key": "imp-invalid",
            "rules": [
                {"rule_id": "rule-fine", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "cultivated", "effective_from": "2026-01-01"},
                {"rule_id": "rule-bad-date", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "cultivated", "effective_from": "2026-13-40"},
            ],
        }
        with self.assertRaises(ValidationFailed):
            self.service.import_protection_rules("county", payload)
        count = self.connection.execute("SELECT COUNT(*) AS c FROM linkage_protection_rules WHERE batch_id='batch-invalid'").fetchone()
        self.assertEqual(count["c"], 0)

    def test_duplicate_submission_returns_stable_result(self) -> None:
        payload = {
            "batch_id": "batch-2",
            "idempotency_key": "imp-2",
            "rules": [
                {"rule_id": "rule-x", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "homestead", "effective_from": "2026-01-01", "effective_to": "2026-12-31"},
            ],
        }
        first = self.service.import_protection_rules("county", payload)
        second = self.service.import_protection_rules("county", dict(payload))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual({k: v for k, v in first.items() if k != "replayed"}, {k: v for k, v in second.items() if k != "replayed"})
        count = self.connection.execute("SELECT COUNT(*) AS c FROM linkage_protection_rules WHERE rule_id='rule-x'").fetchone()
        self.assertEqual(count["c"], 1)

    def test_same_key_with_different_payload_conflicts(self) -> None:
        payload = {
            "batch_id": "batch-3",
            "idempotency_key": "imp-1",
            "rules": [
                {"rule_id": "rule-y", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "homestead", "effective_from": "2026-01-01"},
            ],
        }
        with self.assertRaises(Conflict):
            self.service.import_protection_rules("county", payload)

    def test_import_requires_natural_resources_role(self) -> None:
        payload = {
            "batch_id": "batch-4",
            "idempotency_key": "imp-4",
            "rules": [
                {"rule_id": "rule-z", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "homestead", "effective_from": "2026-01-01"},
            ],
        }
        with self.assertRaises(Forbidden):
            self.service.import_protection_rules("handler", payload)
        with self.assertRaises(Forbidden):
            self.service.import_protection_rules("family", payload)


class WithdrawalAcceptanceTests(LinkageTestCase):
    def test_acceptance_rejects_supplementary_parcel_on_permanent_basic_farmland(self) -> None:
        with self.assertRaises(ValidationFailed) as caught:
            self._withdrawal(supplementary="parcel-pbf")
        self.assertIn("永久基本农田", str(caught.exception))
        event = self.connection.execute(
            "SELECT payload_json FROM supply_audit_events WHERE event_type='withdrawal.rejected'"
        ).fetchone()
        self.assertIsNotNone(event)
        reasons = json.loads(event["payload_json"])["reasons"]
        self.assertIn("permanent_basic_farmland", {reason["code"] for reason in reasons})

    def test_acceptance_rejects_unqualified_member(self) -> None:
        self.service.withdraw_qualification("county", "hh-1-m2", "户籍迁出")
        with self.assertRaises(ValidationFailed) as caught:
            self._withdrawal()
        self.assertIn("不具备资格", str(caught.exception))

    def test_acceptance_rejects_expired_reclamation_commitment(self) -> None:
        self._household("hh-2", reclamation_commitment_deadline="2026-09-01")
        with self.assertRaises(ValidationFailed) as caught:
            self._withdrawal("wd-2", household_id="hh-2")
        self.assertIn("复垦责任", str(caught.exception))

    def test_acceptance_rejects_expired_authorization(self) -> None:
        self._household("hh-3", authorized_until="2026-09-01")
        with self.assertRaises(ValidationFailed) as caught:
            self._withdrawal("wd-3", household_id="hh-3")
        self.assertIn("授权已到期", str(caught.exception))

    def test_acceptance_records_check_snapshot(self) -> None:
        result = self._withdrawal()
        self.assertEqual(result["state"], "accepted")
        self.assertEqual({item["result"] for item in result["checks"]}, {"pass"})
        row = self.connection.execute("SELECT checks_json FROM linkage_withdrawals WHERE withdrawal_id='wd-1'").fetchone()
        self.assertEqual({item["code"] for item in json.loads(row["checks_json"])["items"]}, {
            "permanent_basic_farmland",
            "member_unqualified",
            "reclamation_commitment_expired",
            "authorization_expired",
        })


class CandidateGenerationTests(LinkageTestCase):
    def test_candidates_only_from_zones_satisfying_all_conditions(self) -> None:
        self._withdrawal()
        result = self.service.generate_candidates("handler", "wd-1", "contracted-land")
        self.assertEqual([item["parcel_id"] for item in result["candidates"]], ["parcel-l1"])
        excluded = {item["parcel_id"]: {reason["code"] for reason in item["reasons"]} for item in result["excluded"]}
        self.assertIn("permanent_basic_farmland", excluded["parcel-pbf"])
        self.assertIn("outside_infrastructure_boundary", excluded["parcel-out"])
        self.assertIn("use_control_mismatch", excluded["parcel-r1"])
        self.assertIn("use_control_mismatch", excluded["parcel-h1"])
        self.assertIn("no_determined_version", excluded["parcel-draft"])

    def test_use_control_rule_excludes_restricted_use(self) -> None:
        self._withdrawal()
        result = self.service.generate_candidates("handler", "wd-1", "resettlement")
        self.assertEqual([item["parcel_id"] for item in result["candidates"]], ["parcel-r1"])
        excluded = {item["parcel_id"]: {reason["code"] for reason in item["reasons"]} for item in result["excluded"]}
        self.assertIn("use_control_restricted", excluded["parcel-uc"])
        self.assertIn("permanent_basic_farmland", excluded["parcel-pbf"])

    def test_family_rights_failure_excludes_every_parcel(self) -> None:
        self._withdrawal()
        self.service.withdraw_qualification("county", "hh-1-m1", "资格复核未通过")
        result = self.service.generate_candidates("handler", "wd-1", "contracted-land")
        self.assertFalse(result["household_eligible"])
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["household_reasons"])
        for item in result["excluded"]:
            codes = {reason["code"] for reason in item["reasons"]}
            self.assertIn("member_unqualified", codes)

    def test_exclusion_reasons_are_explained_in_chinese(self) -> None:
        self._withdrawal()
        result = self.service.generate_candidates("handler", "wd-1", "contracted-land")
        messages = [reason["message"] for item in result["excluded"] for reason in item["reasons"]]
        self.assertTrue(any("永久基本农田" in message for message in messages))
        self.assertTrue(any("基础设施边界" in message for message in messages))
        self.assertTrue(any("用途管制" in message for message in messages))


class PlanVersionTests(LinkageTestCase):
    def test_plan_must_reference_determined_versions(self) -> None:
        self._withdrawal()
        with self.assertRaises(Conflict) as caught:
            self.service.create_plan("handler", {
                "plan_id": "plan-draft",
                "withdrawal_id": "wd-1",
                "resettlement": {"parcel_id": "parcel-r1", "version_id": self.versions["parcel-r1"]},
                "land": {"parcel_id": "parcel-draft", "version_id": self.versions["parcel-draft"]},
            })
        self.assertIn("确定版本", str(caught.exception))

    def test_plan_rejects_version_of_another_parcel(self) -> None:
        self._withdrawal()
        with self.assertRaises(NotFound):
            self.service.create_plan("handler", {
                "plan_id": "plan-cross",
                "withdrawal_id": "wd-1",
                "resettlement": {"parcel_id": "parcel-r1", "version_id": self.versions["parcel-l1"]},
                "land": {"parcel_id": "parcel-l1", "version_id": self.versions["parcel-l1"]},
            })

    def test_plan_creation_revalidates_linkage_conditions(self) -> None:
        self._withdrawal()
        with self.assertRaises(ValidationFailed) as caught:
            self.service.create_plan("handler", {
                "plan_id": "plan-pbf",
                "withdrawal_id": "wd-1",
                "resettlement": {"parcel_id": "parcel-r1", "version_id": self.versions["parcel-r1"]},
                "land": {"parcel_id": "parcel-pbf", "version_id": self.versions["parcel-pbf"]},
            })
        self.assertIn("永久基本农田", str(caught.exception))

    def test_new_parcel_version_does_not_rewrite_existing_plan(self) -> None:
        self._withdrawal()
        self._plan()
        added = self.service.add_parcel_version("county", "parcel-l1", {
            "land_use": "facility", "area_mu": "9.5", "within_infrastructure_boundary": True,
        })
        self.service.determine_parcel_version("county", "parcel-l1", added["version"]["version_no"])
        view = self.service.plan_view("handler", "plan-1")
        self.assertEqual(view["land"]["version_id"], self.versions["parcel-l1"])
        self.assertEqual(view["land"]["version_no"], 1)
        self.assertEqual(view["land"]["land_use"], "cultivated")


class PlanLifecycleTests(LinkageTestCase):
    def test_full_lifecycle_to_delivery(self) -> None:
        self._withdrawal()
        self._plan()
        confirmed = self.service.confirm_plan("handler", "plan-1", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        started = self.service.start_construction("handler", "plan-1", 2)
        self.assertEqual(started["state"], "in_construction")
        delivered = self.service.deliver_plan("handler", "plan-1", 3)
        self.assertEqual(delivered["state"], "delivered")
        view = self.service.plan_view("audit", "plan-1")
        self.assertIsNotNone(view["confirmed_at"])
        self.assertIsNotNone(view["delivered_at"])

    def test_transition_requires_current_revision(self) -> None:
        self._withdrawal()
        self._plan()
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("handler", "plan-1", 7)
        with self.assertRaises(InvalidState):
            self.service.start_construction("handler", "plan-1", 1)

    def test_confirm_revalidates_household_rights(self) -> None:
        self._withdrawal()
        self._plan()
        self.clock.advance(days=400)
        with self.assertRaises(InvalidState) as caught:
            self.service.confirm_plan("handler", "plan-1", 1)
        self.assertIn("复垦责任", str(caught.exception))


class QualificationWithdrawalTests(LinkageTestCase):
    def _plans_in_each_state(self) -> None:
        self._withdrawal()
        self._plan("plan-draft")
        self._plan("plan-confirmed")
        self.service.confirm_plan("handler", "plan-confirmed", 1)
        self._plan("plan-building")
        self.service.confirm_plan("handler", "plan-building", 1)
        self.service.start_construction("handler", "plan-building", 2)
        self._plan("plan-delivered")
        self.service.confirm_plan("handler", "plan-delivered", 1)
        self.service.start_construction("handler", "plan-delivered", 2)
        self.service.deliver_plan("handler", "plan-delivered", 3)

    def test_withdrawal_blocks_unconfirmed_and_preserves_delivery(self) -> None:
        self._plans_in_each_state()
        result = self.service.withdraw_qualification("county", "hh-1-m1", "资格复核未通过")
        affected = {item["plan_id"]: item["state"] for item in result["affected_plans"]}
        self.assertEqual(affected, {
            "plan-draft": "blocked",
            "plan-confirmed": "manual_review",
            "plan-building": "manual_review",
        })
        self.assertEqual(self.service.plan_view("audit", "plan-draft")["state"], "blocked")
        self.assertEqual(self.service.plan_view("audit", "plan-confirmed")["state"], "manual_review")
        self.assertEqual(self.service.plan_view("audit", "plan-building")["state"], "manual_review")
        delivered = self.service.plan_view("audit", "plan-delivered")
        self.assertEqual(delivered["state"], "delivered")
        self.assertIsNotNone(delivered["delivered_at"])

    def test_withdrawal_does_not_silently_migrate_parcels(self) -> None:
        self._plans_in_each_state()
        before = self.service.plan_view("audit", "plan-building")
        self.service.withdraw_qualification("county", "hh-1-m1", "资格复核未通过")
        after = self.service.plan_view("audit", "plan-building")
        self.assertEqual(before["resettlement"], after["resettlement"])
        self.assertEqual(before["land"], after["land"])
        self.assertIn("资格撤回", after["state_reason"])

    def test_blocked_plan_cannot_progress(self) -> None:
        self._withdrawal()
        self._plan()
        self.service.withdraw_qualification("county", "hh-1-m1", "资格复核未通过")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("handler", "plan-1", 2)

    def test_double_withdrawal_is_rejected(self) -> None:
        self.service.withdraw_qualification("county", "hh-1-m1", "资格复核未通过")
        with self.assertRaises(InvalidState):
            self.service.withdraw_qualification("county", "hh-1-m1", "重复操作")

    def test_withdrawal_requires_county_role(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.withdraw_qualification("handler", "hh-1-m1", "越权")


class ViewScopingTests(LinkageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._withdrawal()
        self.run = self.service.generate_candidates("handler", "wd-1", "contracted-land")
        self._plan()

    def test_household_view_is_minimal_and_scoped_to_own_data(self) -> None:
        view = self.service.plan_view("family", "plan-1")
        self.assertEqual(view["state"], "draft")
        self.assertNotIn("created_by", view)
        self.assertNotIn("revision", view)
        self.assertNotIn("protection_sha256", view)
        self.assertNotIn("household_id", view)
        withdrawal = self.service.withdrawal_view("family", "wd-1")
        self.assertEqual(withdrawal["state"], "accepted")
        self.assertNotIn("created_by", withdrawal)

    def test_household_cannot_view_other_households(self) -> None:
        self._household("hh-9")
        self.service.link_household_user("handler", {"user_id": "family-b", "household_id": "hh-9"})
        with self.assertRaises(Forbidden):
            self.service.plan_view("family-b", "plan-1")
        with self.assertRaises(Forbidden):
            self.service.household_view("family-b", "hh-1")

    def test_unlinked_household_account_is_denied(self) -> None:
        self.service.create_user("family-c", "未关联家庭", "household")
        with self.assertRaises(Forbidden):
            self.service.plan_view("family-c", "plan-1")

    def test_operational_roles_see_full_detail(self) -> None:
        for actor in ("handler", "county", "audit"):
            view = self.service.plan_view(actor, "plan-1")
            self.assertIn("created_by", view)
            self.assertIn("protection_sha256", view)
            self.assertEqual(view["household_id"], "hh-1")

    def test_non_linkage_role_is_denied(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.plan_view("plan", "plan-1")

    def test_candidate_run_explains_exclusion_to_each_role(self) -> None:
        run_id = self.run["run_id"]
        for actor in ("family", "handler", "audit"):
            view = self.service.candidate_run_view(actor, run_id)
            excluded = {item["parcel_id"]: item["reasons"] for item in view["excluded"]}
            self.assertIn("parcel-pbf", excluded)
            codes = {reason["code"] for reason in excluded["parcel-pbf"]}
            self.assertIn("permanent_basic_farmland", codes)
        household_view = self.service.candidate_run_view("family", run_id)
        self.assertNotIn("input_sha256", household_view)
        audit_view = self.service.candidate_run_view("audit", run_id)
        self.assertIn("input_sha256", audit_view)


class LinkageApiTests(LinkageTestCase):
    def test_http_boundary(self) -> None:
        app = JsonApplication(self.service, self.service)
        headers = {"X-Actor-Id": "county"}
        payload = json.dumps({
            "batch_id": "batch-api",
            "idempotency_key": "imp-api",
            "rules": [
                {"rule_id": "rule-api", "parcel_id": "parcel-l1", "rule_type": "use_control", "restricted_use": "homestead", "effective_from": "2026-01-01"},
            ],
        }).encode()
        created = app.handle("POST", "/linkage/protection-rules/import", headers, payload)
        self.assertEqual(created.status, 201)
        self.assertFalse(created.body["replayed"])
        replayed = app.handle("POST", "/linkage/protection-rules/import", headers, payload)
        self.assertEqual(replayed.status, 201)
        self.assertTrue(replayed.body["replayed"])
        self.assertEqual(created.body["rule_ids"], replayed.body["rule_ids"])

    def test_http_views_and_errors(self) -> None:
        self._withdrawal()
        self._plan()
        app = JsonApplication(self.service, self.service)
        own = app.handle("GET", "/linkage/plans/plan-1", {"X-Actor-Id": "family"})
        self.assertEqual(own.status, 200)
        self.assertNotIn("created_by", own.body)
        denied = app.handle("GET", "/linkage/plans/plan-1", {"X-Actor-Id": "family-b"})
        self.assertEqual(denied.status, 403)
        missing = app.handle("GET", "/linkage/plans/plan-void", {"X-Actor-Id": "audit"})
        self.assertEqual(missing.status, 404)
        rejected = app.handle("POST", "/linkage/withdrawals", {"X-Actor-Id": "handler"}, json.dumps({
            "withdrawal_id": "wd-api",
            "project_id": "proj-1",
            "household_id": "hh-1",
            "homestead_parcel_id": "parcel-h1",
            "supplementary_parcel_id": "parcel-pbf",
        }).encode())
        self.assertEqual(rejected.status, 422)
        self.assertIn("永久基本农田", rejected.body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
