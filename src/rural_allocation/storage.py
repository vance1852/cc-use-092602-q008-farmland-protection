"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','handler','natural_resources','household')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mu TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mu TEXT NOT NULL,
    available_mu TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mu TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    allocated_mu TEXT NOT NULL DEFAULT '0',
    delivered_mu TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    surveyed_mu TEXT NOT NULL,
    expected_delivered_mu TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

CREATE TABLE IF NOT EXISTS linkage_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'accepting' CHECK(state IN ('accepting','closed')),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkage_parcels (
    parcel_id TEXT PRIMARY KEY,
    village TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkage_parcel_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    version_no INTEGER NOT NULL,
    land_use TEXT NOT NULL CHECK(land_use IN ('cultivated','homestead','construction','facility')),
    area_mu TEXT NOT NULL,
    within_infrastructure_boundary INTEGER NOT NULL CHECK(within_infrastructure_boundary IN (0,1)),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','determined')),
    determined_at TEXT,
    registered_by TEXT NOT NULL REFERENCES supply_users(user_id),
    registered_at TEXT NOT NULL,
    UNIQUE(parcel_id, version_no)
);

CREATE TABLE IF NOT EXISTS linkage_protection_batches (
    batch_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    imported_by TEXT NOT NULL REFERENCES supply_users(user_id),
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkage_protection_rules (
    rule_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES linkage_protection_batches(batch_id),
    parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    rule_type TEXT NOT NULL CHECK(rule_type IN ('permanent_basic_farmland','use_control')),
    restricted_use TEXT CHECK(restricted_use IS NULL OR restricted_use IN ('cultivated','homestead','construction','facility')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkage_rules_parcel
ON linkage_protection_rules(parcel_id, effective_from);

CREATE TABLE IF NOT EXISTS linkage_households (
    household_id TEXT PRIMARY KEY,
    head_name TEXT NOT NULL,
    village TEXT NOT NULL,
    authorized_scope TEXT NOT NULL,
    authorized_until TEXT NOT NULL,
    reclamation_commitment_deadline TEXT NOT NULL,
    commitment_note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linkage_household_members (
    member_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES linkage_households(household_id),
    name TEXT NOT NULL,
    qualified INTEGER NOT NULL DEFAULT 1 CHECK(qualified IN (0,1)),
    withdrawn_reason TEXT,
    withdrawn_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkage_members_household
ON linkage_household_members(household_id);

CREATE TABLE IF NOT EXISTS linkage_household_users (
    user_id TEXT PRIMARY KEY REFERENCES supply_users(user_id),
    household_id TEXT NOT NULL REFERENCES linkage_households(household_id)
);

CREATE TABLE IF NOT EXISTS linkage_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES linkage_projects(project_id),
    household_id TEXT NOT NULL REFERENCES linkage_households(household_id),
    homestead_parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    supplementary_parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    state TEXT NOT NULL DEFAULT 'accepted' CHECK(state IN ('accepted','cancelled')),
    checks_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkage_withdrawals_household
ON linkage_withdrawals(household_id);

CREATE TABLE IF NOT EXISTS linkage_candidate_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    withdrawal_id TEXT NOT NULL REFERENCES linkage_withdrawals(withdrawal_id),
    purpose TEXT NOT NULL CHECK(purpose IN ('resettlement','contracted-land')),
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkage_candidate_runs_withdrawal
ON linkage_candidate_runs(withdrawal_id);

CREATE TABLE IF NOT EXISTS linkage_plans (
    plan_id TEXT PRIMARY KEY,
    withdrawal_id TEXT NOT NULL REFERENCES linkage_withdrawals(withdrawal_id),
    project_id TEXT NOT NULL REFERENCES linkage_projects(project_id),
    household_id TEXT NOT NULL REFERENCES linkage_households(household_id),
    resettlement_parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    resettlement_version_id INTEGER NOT NULL REFERENCES linkage_parcel_versions(version_id),
    land_parcel_id TEXT NOT NULL REFERENCES linkage_parcels(parcel_id),
    land_version_id INTEGER NOT NULL REFERENCES linkage_parcel_versions(version_id),
    protection_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','in_construction','delivered','blocked','manual_review')),
    revision INTEGER NOT NULL DEFAULT 1,
    state_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_linkage_plans_household
ON linkage_plans(household_id, state);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 在 worker 线程中处理请求，连接需要允许跨线程使用；
    # SQLite 以串行模式编译，配合 BEGIN IMMEDIATE 事务保证写入串行化。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
