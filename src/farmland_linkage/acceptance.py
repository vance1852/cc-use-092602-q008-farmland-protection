"""贯通地块登记、保护规则、家庭授权、退出受理、方案候选与资格撤回的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import LinkageService


def _parcel(parcel_id: str, village_id: str, land_use: str, **flags: object) -> dict[str, object]:
    return {
        "parcel_id": parcel_id,
        "village_id": village_id,
        "land_use": land_use,
        "permanent_basic_farmland": bool(flags.get("permanent_basic_farmland", False)),
        "within_infrastructure_boundary": bool(flags.get("within_infrastructure_boundary", False)),
        "area_mu": flags.get("area_mu", "6.5"),
        "current_holder_household_id": flags.get("current_holder_household_id"),
        "note": flags.get("note", "验收登记"),
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = LinkageService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    service.create_user("registrar", "县自然资源登记员", "registrar")
    service.create_user("officer", "乡镇经办人", "officer")
    service.create_user("auditor", "县审计员", "auditor")

    # 登记地块版本：退出宅基地与多宗补充分配候选地。
    service.register_parcel_version("registrar", _parcel("homestead-1", "village-north", "homestead", current_holder_household_id="hh-east"))
    service.register_parcel_version("registrar", _parcel("homestead-2", "village-north", "homestead", current_holder_household_id="hh-west"))
    service.register_parcel_version("registrar", _parcel("homestead-3", "village-south", "homestead", current_holder_household_id="hh-north"))
    service.register_parcel_version("registrar", _parcel("supplement-good", "village-north", "cultivated"))
    service.register_parcel_version("registrar", _parcel("supplement-good-2", "village-north", "cultivated"))
    service.register_parcel_version("registrar", _parcel("supplement-good-3", "village-south", "reserve"))
    service.register_parcel_version("registrar", _parcel("supplement-farmland", "village-north", "cultivated", permanent_basic_farmland=True))
    service.register_parcel_version("registrar", _parcel("supplement-infra", "village-south", "reserve", within_infrastructure_boundary=True))
    service.register_parcel_version("registrar", _parcel("supplement-held", "village-south", "cultivated", current_holder_household_id="hh-other"))
    service.register_parcel_version("registrar", _parcel("supplement-ruled", "village-north", "cultivated"))

    # 批量导入保护规则：全有或全无，重复提交返回稳定结果。
    batch = {
        "batch_id": "batch-2026-09",
        "rules": [
            {"rule_id": "rule-use-control-1", "parcel_id": "supplement-ruled", "scope_type": "use_control", "allowed_purposes": ["resettlement"]},
            {"rule_id": "rule-infra-1", "parcel_id": "supplement-infra", "scope_type": "infrastructure_boundary", "effective_from": "2026-01-01"},
        ],
    }
    imported = service.import_protection_rules("registrar", batch)
    replayed = service.import_protection_rules("registrar", batch)

    # 家庭主体、授权与复垦承诺。
    for household_id, village_id, head, members in (
        ("hh-east", "village-north", "王东部", [("m1", "王东部"), ("m2", "王妻")]),
        ("hh-west", "village-north", "李西部", [("w1", "李西部")]),
        ("hh-north", "village-south", "赵北部", [("n1", "赵北部")]),
    ):
        service.create_household("registrar", {"household_id": household_id, "village_id": village_id, "head_name": head, "members": [{"member_id": mid, "name": name} for mid, name in members]})
    service.create_user("hh-east-user", "王东部", "household", "hh-east")
    for index, household_id, member_ids in (
        (1, "hh-east", ["m1", "m2"]),
        (2, "hh-west", ["w1"]),
        (3, "hh-north", ["n1"]),
    ):
        service.create_authorization("registrar", {"authorization_id": f"auth-{index}", "household_id": household_id, "homestead_parcel_id": f"homestead-{index}", "consented_member_ids": member_ids, "valid_until": "2027-12-31"})
        service.create_commitment("registrar", {"commitment_id": f"com-{index}", "household_id": household_id, "parcel_id": f"homestead-{index}", "responsible_party": "县土地整理中心", "promised_deadline": "2027-06-30"})

    # 跨村整治项目受理三户宅基地退出。
    service.create_project("officer", {"project_id": "proj-1", "name": "南北两村联动整治", "village_ids": ["village-north", "village-south"]})
    applications = []
    for index, household_id in ((1, "hh-east"), (2, "hh-west"), (3, "hh-north")):
        applications.append(service.accept_application("officer", {
            "application_id": f"app-{index}",
            "project_id": "proj-1",
            "household_id": household_id,
            "homestead_parcel_id": f"homestead-{index}",
            "supplement_parcel_id": f"supplement-good{'' if index == 1 else f'-{index}'}",
            "authorization_id": f"auth-{index}",
            "commitment_id": f"com-{index}",
            "idempotency_key": f"app-key-{index}",
        }))

    # 安置与土地方案引用确定版本并生成候选。
    plan_1 = service.create_plan("officer", {"plan_id": "plan-1", "application_id": "app-1", "purpose": "contracted-supplement", "idempotency_key": "plan-key-1"})
    service.create_plan("officer", {"plan_id": "plan-2", "application_id": "app-2", "purpose": "contracted-supplement", "idempotency_key": "plan-key-2"})
    service.create_plan("officer", {"plan_id": "plan-3", "application_id": "app-3", "purpose": "contracted-supplement", "idempotency_key": "plan-key-3"})
    service.confirm_plan("officer", "plan-1", 1)
    service.confirm_plan("officer", "plan-2", 1)
    service.start_construction("officer", "proj-1", 1)
    service.deliver_plan("officer", "plan-1", 2)

    # 资格撤回：plan-3（草稿）立即阻止，plan-2（已确认未交付）与施工中项目转人工处置，plan-1（已交付）不动。
    withdrawn = service.withdraw_member_eligibility("registrar", "hh-north", "n1", "户籍迁出不再具备成员资格")
    withdrawn_west = service.withdraw_member_eligibility("registrar", "hh-west", "w1", "资格复核不通过")
    resolved_plan = service.resolve_plan("officer", "plan-2", "cancel", "资格撤回后人工取消", 3)
    resolved_project = service.resolve_project("officer", "proj-1", "resume", "人工核查后恢复施工", 3)

    explanation = service.parcel_explanation("officer", "plan-1", "supplement-farmland")
    household_plan = service.plan_view("hh-east-user", "plan-1")
    auditor_plan = service.plan_view("auditor", "plan-1")
    final_states = {plan_id: service.plan_view("officer", plan_id)["state"] for plan_id in ("plan-1", "plan-2", "plan-3")}
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "rules_imported": imported["imported"],
        "rules_replayed": replayed["replayed"],
        "applications": [item["application_id"] for item in applications],
        "plan_1_eligible_candidates": plan_1["eligible_count"],
        "plan_final_states": final_states,
        "withdrawn": {"blocked": withdrawn["blocked_plans"], "manual": withdrawn_west["manual_review_plans"], "projects": withdrawn["manual_review_projects"]},
        "resolved": {"plan": resolved_plan["state"], "project": resolved_project["state"]},
        "exclusion_reason": explanation["reasons"][0]["rule_code"],
        "household_view_state": household_plan["state"],
        "auditor_head_name": auditor_plan["household"]["head_name"],
        "audit": service.audit_chain("auditor"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行耕地保护与宅基地退出联动离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
