"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS containment_units (
    unit_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    barrier_level INTEGER NOT NULL DEFAULT 0 CHECK(barrier_level >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, name)
);
CREATE TABLE IF NOT EXISTS storage_locations (
    location_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    unit_id TEXT NOT NULL REFERENCES containment_units(unit_id),
    capacity_total REAL NOT NULL CHECK(capacity_total > 0),
    quantity_unit TEXT NOT NULL,
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    barrier_level INTEGER NOT NULL DEFAULT 0 CHECK(barrier_level >= 0),
    required_certifications_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, name)
);
CREATE TABLE IF NOT EXISTS storage_location_history (
    history_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    UNIQUE(location_id, version)
);
CREATE TABLE IF NOT EXISTS safety_sheets (
    sheet_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    supplier_name TEXT NOT NULL,
    chemical_name TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    hazard_class TEXT NOT NULL,
    concentration REAL NOT NULL CHECK(concentration > 0 AND concentration <= 100),
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    container_materials_json TEXT NOT NULL,
    incompatible_classes_json TEXT NOT NULL DEFAULT '[]',
    required_barrier_level INTEGER NOT NULL DEFAULT 0 CHECK(required_barrier_level >= 0),
    effective_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, supplier_name, batch_no, version)
);
CREATE TABLE IF NOT EXISTS rule_books (
    rule_book_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    incompatible_pairs_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, version)
);
CREATE TABLE IF NOT EXISTS chemical_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    supplier_name TEXT NOT NULL,
    chemical_name TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    sheet_id TEXT NOT NULL REFERENCES safety_sheets(sheet_id),
    quantity_received REAL NOT NULL CHECK(quantity_received > 0),
    quantity_unit TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'depleted', 'disposed')),
    received_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, supplier_name, batch_no)
);
CREATE TABLE IF NOT EXISTS chemical_placements (
    placement_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES chemical_batches(batch_id),
    location_id TEXT NOT NULL REFERENCES storage_locations(location_id),
    unit_id TEXT NOT NULL REFERENCES containment_units(unit_id),
    initial_quantity REAL NOT NULL CHECK(initial_quantity > 0),
    quantity REAL NOT NULL CHECK(quantity >= 0),
    planned_quantity REAL NOT NULL DEFAULT 0 CHECK(planned_quantity >= 0),
    quantity_unit TEXT NOT NULL,
    container_material TEXT NOT NULL,
    sheet_id TEXT NOT NULL REFERENCES safety_sheets(sheet_id),
    rule_book_id TEXT NOT NULL REFERENCES rule_books(rule_book_id),
    responsible_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK(status IN ('occupied', 'quarantined', 'released')),
    placed_at TEXT NOT NULL,
    released_at TEXT,
    request_id TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_placements_location ON chemical_placements(location_id, status);
CREATE INDEX IF NOT EXISTS idx_placements_batch ON chemical_placements(batch_id, status);
CREATE TABLE IF NOT EXISTS placement_status_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    placement_id TEXT NOT NULL REFERENCES chemical_placements(placement_id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_placement_status ON placement_status_events(placement_id, occurred_at);
CREATE TABLE IF NOT EXISTS container_changes (
    change_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL REFERENCES chemical_placements(placement_id),
    batch_id TEXT NOT NULL REFERENCES chemical_batches(batch_id),
    old_material TEXT NOT NULL,
    new_material TEXT NOT NULL,
    sheet_id TEXT NOT NULL REFERENCES safety_sheets(sheet_id),
    reason TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    request_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placement_moves (
    move_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES chemical_batches(batch_id),
    from_placement_id TEXT NOT NULL REFERENCES chemical_placements(placement_id),
    to_placement_id TEXT NOT NULL REFERENCES chemical_placements(placement_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    actor_id TEXT NOT NULL,
    moved_at TEXT NOT NULL,
    request_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_ledger (
    ledger_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES chemical_batches(batch_id),
    movement_type TEXT NOT NULL CHECK(movement_type IN ('receive', 'issue', 'loss', 'return', 'disposal')),
    quantity REAL NOT NULL,
    quantity_unit TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    placement_id TEXT,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_batch ON stock_ledger(batch_id, occurred_at);
CREATE TABLE IF NOT EXISTS certifications (
    certification_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    certification_type TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(actor_id, certification_type, valid_from)
);
CREATE TABLE IF NOT EXISTS isolation_measures (
    measure_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    unit_id TEXT REFERENCES containment_units(unit_id),
    location_id TEXT REFERENCES storage_locations(location_id),
    batch_id TEXT REFERENCES chemical_batches(batch_id),
    reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT,
    lifted_at TEXT,
    imposed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_isolation_scope ON isolation_measures(site_id, lifted_at);
CREATE TABLE IF NOT EXISTS stock_counts (
    count_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES chemical_batches(batch_id),
    counted_quantity REAL NOT NULL CHECK(counted_quantity >= 0),
    ledger_remaining REAL NOT NULL,
    placement_quantity REAL NOT NULL,
    discrepancy REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    counted_by TEXT NOT NULL,
    counted_at TEXT NOT NULL,
    request_id TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
