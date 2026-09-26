"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .linkage import LinkageService
from .service import SupplyService


def run_linkage(connection: sqlite3.Connection, service: SupplyService) -> dict[str, object]:
    """贯通地块版本、保护规则、家庭授权、退出受理、候选生成与方案交付。"""
    linkage = LinkageService(connection, service.clock)
    service.create_user("county", "县自然资源", "natural_resources")
    service.create_user("handler", "经办人", "handler")
    service.create_user("family", "家庭账号", "household")
    linkage.create_project("county", {"project_id": "remediation-1", "name": "跨村整治一期"})
    resettlement = linkage.register_parcel("county", {"parcel_id": "parcel-r1", "village": "东村", "version": {"land_use": "construction", "area_mu": "18.5", "within_infrastructure_boundary": True}})
    land = linkage.register_parcel("county", {"parcel_id": "parcel-l1", "village": "东村", "version": {"land_use": "cultivated", "area_mu": "12.0", "within_infrastructure_boundary": True}})
    linkage.register_parcel("county", {"parcel_id": "parcel-h1", "village": "东村", "version": {"land_use": "homestead", "area_mu": "0.4", "within_infrastructure_boundary": True}})
    linkage.register_parcel("county", {"parcel_id": "parcel-pbf", "village": "西村", "version": {"land_use": "cultivated", "area_mu": "20.0", "within_infrastructure_boundary": True}})
    for parcel_id in ("parcel-r1", "parcel-l1", "parcel-h1", "parcel-pbf"):
        linkage.determine_parcel_version("county", parcel_id, 1)
    imported = linkage.import_protection_rules("county", {"batch_id": "batch-1", "idempotency_key": "imp-1", "rules": [{"rule_id": "rule-pbf-1", "parcel_id": "parcel-pbf", "rule_type": "permanent_basic_farmland", "effective_from": "2026-01-01"}]})
    replayed = linkage.import_protection_rules("county", {"batch_id": "batch-1", "idempotency_key": "imp-1", "rules": [{"rule_id": "rule-pbf-1", "parcel_id": "parcel-pbf", "rule_type": "permanent_basic_farmland", "effective_from": "2026-01-01"}]})
    linkage.register_household("handler", {"household_id": "hh-1", "head_name": "王五", "village": "东村", "authorized_scope": "宅基地退出与安置", "authorized_until": "2027-12-31", "reclamation_commitment_deadline": "2027-06-30", "commitment_note": "退出后三个月内完成复垦", "members": [{"member_id": "hh-1-m1", "name": "王五"}, {"member_id": "hh-1-m2", "name": "李四"}]})
    linkage.link_household_user("handler", {"user_id": "family", "household_id": "hh-1"})
    withdrawal = linkage.accept_withdrawal("handler", {"withdrawal_id": "wd-1", "project_id": "remediation-1", "household_id": "hh-1", "homestead_parcel_id": "parcel-h1", "supplementary_parcel_id": "parcel-l1"})
    candidates = linkage.generate_candidates("handler", "wd-1", "contracted-land")
    plan = linkage.create_plan("handler", {"plan_id": "plan-1", "withdrawal_id": "wd-1", "resettlement": {"parcel_id": "parcel-r1", "version_id": resettlement["version"]["version_id"]}, "land": {"parcel_id": "parcel-l1", "version_id": land["version"]["version_id"]}})
    linkage.confirm_plan("handler", "plan-1", 1)
    linkage.start_construction("handler", "plan-1", 2)
    delivered = linkage.deliver_plan("handler", "plan-1", 3)
    family_view = linkage.plan_view("family", "plan-1")
    return {
        "withdrawal": withdrawal["state"],
        "import_replay_stable": replayed["replayed"] and replayed["rule_ids"] == imported["rule_ids"],
        "candidates": [item["parcel_id"] for item in candidates["candidates"]],
        "excluded_with_reasons": sorted(item["parcel_id"] for item in candidates["excluded"]),
        "plan": plan["plan_id"],
        "delivered": delivered["state"],
        "family_view_fields": sorted(family_view.keys()),
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "village-a", "name": "北部示范村", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mu": "500000"})
    service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
    service.create_route("plan", {"route_id": "pool-a-b", "origin_id": "village-a", "destination_id": "settlement-b", "product": "cultivated-land", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pool-a-b", "shipper_id": "household-east", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pool-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "relocation-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pool-a-b": "20"}, "demand_changes": {"village-a:cultivated-land": "-5"}})
    service.approve_scenario("risk", "relocation-recovery", 1)
    scenario = service.run_scenario("plan", "relocation-recovery", "2026-09-23")
    linkage = run_linkage(connection, service)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "linkage": linkage, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行乡镇片区调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
