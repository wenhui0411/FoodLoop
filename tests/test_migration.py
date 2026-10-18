"""Tests for the versioned SQLite migration (v0 -> v1) and its safe backup.

Covers: fresh DB stamped at the current version; a legacy v0 DB migrated with
columns added and claims backfilled; the pre-migration backup created and
verified; a pre-existing valid backup reused (never overwritten); and a
pre-existing invalid backup aborting the migration with the live DB intact.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from foodloop.errors import MigrationError
from foodloop.repository import Repository

NOW = datetime(2026, 1, 15, 9, 0, 0)

# Pre-M1 ("v0") schema: donations without closed_to_claims; claims without
# quantity_value / pickup_code / status, with UNIQUE(donation_id).
_OLD_SCHEMA = """
CREATE TABLE donors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, donor_type TEXT NOT NULL,
    area TEXT NOT NULL, latitude REAL, longitude REAL,
    contact_email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE recipients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, recipient_type TEXT NOT NULL,
    area TEXT NOT NULL, latitude REAL, longitude REAL,
    accepted_categories TEXT NOT NULL DEFAULT '[]',
    storage_capabilities TEXT NOT NULL DEFAULT '[]',
    max_quantity_kg_equiv REAL, collection_days TEXT NOT NULL DEFAULT '[]',
    collection_start_hour INTEGER NOT NULL DEFAULT 8,
    collection_end_hour INTEGER NOT NULL DEFAULT 18,
    verified INTEGER NOT NULL DEFAULT 0, contact_email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE donations (
    id TEXT PRIMARY KEY, donor_id TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,
    quantity_value REAL NOT NULL, quantity_unit TEXT NOT NULL, area TEXT NOT NULL,
    latitude REAL, longitude REAL, collection_deadline TEXT NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL, storage TEXT NOT NULL,
    allergens TEXT NOT NULL DEFAULT '[]', allergens_declared INTEGER NOT NULL DEFAULT 0,
    handling_notes TEXT NOT NULL DEFAULT '', best_before TEXT
);
CREATE TABLE claims (
    id TEXT PRIMARY KEY, donation_id TEXT NOT NULL UNIQUE,
    recipient_id TEXT NOT NULL, claimed_at TEXT NOT NULL, collected_at TEXT
);
"""


def _make_legacy_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute("INSERT INTO donors (id, name, donor_type, area) "
                     "VALUES ('d1', 'Bakery', 'bakery', 'Central')")
        conn.execute("INSERT INTO recipients (id, name, recipient_type, area, verified) "
                     "VALUES ('r1', 'Food Bank', 'food_bank', 'Central', 1)")
        deadline = (NOW + timedelta(days=1)).isoformat()
        for did, qty, status in (("dn1", 10.0, "collected"), ("dn2", 5.0, "claimed")):
            conn.execute(
                "INSERT INTO donations (id, donor_id, title, category, quantity_value, "
                "quantity_unit, area, collection_deadline, created_at, status, storage) "
                "VALUES (?, 'd1', 'Item', 'bakery', ?, 'loaves', 'Central', ?, ?, ?, 'ambient')",
                (did, qty, deadline, NOW.isoformat(), status),
            )
        # c1 collected, c2 pending (no status column yet)
        conn.execute("INSERT INTO claims (id, donation_id, recipient_id, claimed_at, "
                     "collected_at) VALUES ('c1', 'dn1', 'r1', ?, ?)",
                     (NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()))
        conn.execute("INSERT INTO claims (id, donation_id, recipient_id, claimed_at, "
                     "collected_at) VALUES ('c2', 'dn2', 'r1', ?, NULL)",
                     (NOW.isoformat(),))
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()


def _user_version(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _claim_columns(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
    finally:
        conn.close()


def _backup_path(db: Path) -> Path:
    return db.with_name(f"{db.stem}.pre-v1{db.suffix}")


# --------------------------------------------------------------------------
# Fresh DB
# --------------------------------------------------------------------------
def test_fresh_db_is_stamped_and_has_new_columns(tmp_path):
    db = tmp_path / "fresh.db"
    Repository(db)
    assert _user_version(db) == 2
    assert {"quantity_value", "pickup_code", "status"} <= _claim_columns(db)
    assert not _backup_path(db).exists()  # no backup needed for a fresh DB


# --------------------------------------------------------------------------
# Legacy migration + backup
# --------------------------------------------------------------------------
def test_legacy_db_migrates_backfills_and_backs_up(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(str(db))

    repo = Repository(db)  # triggers migration (v0 -> v1 -> v2)

    assert _user_version(db) == 2
    assert {"quantity_value", "pickup_code", "status"} <= _claim_columns(db)
    # UNIQUE(donation_id) removed: multiple claims per donation now allowed.
    assert not repo._has_unique_donation_index(sqlite3.connect(str(db)))

    c1 = repo.get_claim_for_donation("dn1")
    c2 = repo.get_claim_for_donation("dn2")
    assert c1.status.value == "collected" and c1.quantity_value == 10.0
    assert c2.status.value == "reserved" and c2.quantity_value == 5.0
    # Codes generated (preserved through the v1->v2 rebuild), non-empty, unique.
    assert c1.pickup_code and c2.pickup_code and c1.pickup_code != c2.pickup_code
    # Donation-level column added; data preserved.
    assert repo.get_donation("dn1").closed_to_claims is False
    assert len(repo.list_donations()) == 2

    # The v0 snapshot (taken before the v0->v1 step) is a valid pre-migration copy.
    backup = _backup_path(db)
    assert backup.exists()
    assert _user_version(backup) == 0


def test_migration_reuses_valid_existing_backup_without_overwriting(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(str(db))
    backup = _backup_path(db)
    # A hand-placed, valid v0 snapshot (a copy of the pre-migration DB).
    backup.write_bytes(db.read_bytes())
    original = backup.read_bytes()

    Repository(db)  # should reuse, not overwrite, the existing pre-v1 backup

    assert _user_version(db) == 2              # migration still succeeded
    assert backup.read_bytes() == original     # pre-v1 backup untouched


def test_migration_aborts_on_invalid_existing_backup_leaving_db_intact(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(str(db))
    _backup_path(db).write_bytes(b"not a sqlite database")  # corrupt backup

    with pytest.raises(MigrationError):
        Repository(db)

    # Live DB untouched: still v0, no new columns.
    assert _user_version(db) == 0
    assert "status" not in _claim_columns(db)
