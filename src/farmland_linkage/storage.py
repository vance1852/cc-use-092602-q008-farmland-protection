"""耕地保护与宅基地退出联动服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS linkage_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('registrar','officer','household','auditor')),
    household_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcels (
    parcel_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcel_versions (
    parcel_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    version INTEGER NOT NULL,
    village_id TEXT NOT NULL,
    land_use TEXT NOT NULL
        CHECK(land_use IN ('cultivated','homestead','construction','facility','forest','reserve')),
    permanent_basic_farmland INTEGER NOT NULL CHECK(permanent_basic_farmland IN (0,1)),
    within_infrastructure_boundary INTEGER NOT NULL CHECK(within_infrastructure_boundary IN (0,1)),
    area_mu TEXT NOT NULL,
    current_holder_household_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    registered_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    registered_at TEXT NOT NULL,
    UNIQUE(parcel_id, version)
);

CREATE INDEX IF NOT EXISTS idx_parcel_versions_latest
ON parcel_versions(parcel_id, version);

CREATE TABLE IF NOT EXISTS protection_rule_batches (
    batch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    imported_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_rules (
    rule_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES protection_rule_batches(batch_id),
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    scope_type TEXT NOT NULL
        CHECK(scope_type IN ('permanent_basic_farmland','use_control','infrastructure_boundary')),
    allowed_purposes TEXT,
    effective_from TEXT,
    effective_to TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_protection_rules_parcel
ON protection_rules(parcel_id, scope_type);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    village_id TEXT NOT NULL,
    head_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS household_members (
    household_id TEXT NOT NULL REFERENCES households(household_id),
    member_id TEXT NOT NULL,
    name TEXT NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 1 CHECK(eligible IN (0,1)),
    ineligible_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(household_id, member_id)
);

CREATE TABLE IF NOT EXISTS household_authorizations (
    authorization_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    homestead_parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    consented_member_ids TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    created_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reclamation_commitments (
    commitment_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    responsible_party TEXT NOT NULL,
    promised_deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','fulfilled','breached')),
    created_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remediation_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    village_ids TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'accepting'
        CHECK(state IN ('accepting','in_construction','manual_review','suspended','completed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_applications (
    application_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES remediation_projects(project_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    homestead_parcel_version_id INTEGER NOT NULL REFERENCES parcel_versions(parcel_version_id),
    supplement_parcel_version_id INTEGER NOT NULL REFERENCES parcel_versions(parcel_version_id),
    authorization_id TEXT NOT NULL REFERENCES household_authorizations(authorization_id),
    commitment_id TEXT NOT NULL REFERENCES reclamation_commitments(commitment_id),
    state TEXT NOT NULL DEFAULT 'accepted' CHECK(state IN ('accepted','cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    accepted_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    accepted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exit_applications_household
ON exit_applications(household_id, state);

CREATE TABLE IF NOT EXISTS linkage_plans (
    plan_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES exit_applications(application_id),
    project_id TEXT NOT NULL REFERENCES remediation_projects(project_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    purpose TEXT NOT NULL CHECK(purpose IN ('contracted-supplement','resettlement')),
    homestead_parcel_version_id INTEGER NOT NULL REFERENCES parcel_versions(parcel_version_id),
    supplement_parcel_version_id INTEGER NOT NULL REFERENCES parcel_versions(parcel_version_id),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','delivered','blocked','manual_review','cancelled')),
    evaluated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES linkage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkage_plans_household
ON linkage_plans(household_id, state);

CREATE INDEX IF NOT EXISTS idx_linkage_plans_project
ON linkage_plans(project_id, state);

CREATE TABLE IF NOT EXISTS plan_candidates (
    plan_id TEXT NOT NULL REFERENCES linkage_plans(plan_id),
    parcel_id TEXT NOT NULL,
    parcel_version_id INTEGER NOT NULL REFERENCES parcel_versions(parcel_version_id),
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
    reasons_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, parcel_id)
);

CREATE TABLE IF NOT EXISTS linkage_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS linkage_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_linkage_audit_entity
ON linkage_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
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
