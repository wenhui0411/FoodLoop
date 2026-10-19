"""SQLite persistence for FoodLoop.

All SQL lives here, hidden behind a :class:`Repository` whose methods speak
in domain objects (``Donation``, ``Recipient``, ``Claim`` ...). The rest of
the app never writes SQL.

Why SQLite (stdlib ``sqlite3``)
-------------------------------
* Zero extra dependency.
* A ``UNIQUE`` constraint on ``claims.donation_id`` lets the database itself
  guarantee a donation has at most one active claim — this is what makes
  duplicate-claim prevention real rather than best-effort, even if two
  claim attempts race.

Connection strategy
-------------------
Each public method opens a short-lived connection via :meth:`_connect`.
Streamlit re-runs scripts across threads, and sqlite connections are not
safe to share between threads; a connection-per-operation sidesteps that
entirely. For a prototype the overhead is negligible.

Honest limitation
------------------
sqlite serialises writes with a database-level lock, so this is correct but
not built for high write concurrency. That is acceptable for a township
prototype and is documented rather than hidden.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .errors import (
    ClaimsClosedError,
    EligibilityError,
    InvalidTransitionError,
    MigrationError,
    NotFoundError,
    OverReservationError,
)
from .models import (
    Claim,
    ClaimStatus,
    Donation,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Quantity,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)
from .reservation import DonationAvailability, available_quantity, derive_donation_status

#: Current on-disk schema version (PRAGMA user_version). Bumped by migrations.
#: v1 added partial-claim columns; v2 removed the one-claim-per-donation
#: UNIQUE(donation_id) constraint to allow partial reservations.
_SCHEMA_VERSION = 2

#: How long a blocked writer waits for the SQLite lock before erroring.
_BUSY_TIMEOUT_MS = 5000

#: Pickup-code alphabet: unambiguous (no I, L, O, 0, 1) for human entry.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS donors (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    donor_type    TEXT NOT NULL,
    area          TEXT NOT NULL,
    latitude      REAL,
    longitude     REAL,
    contact_email TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recipients (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    recipient_type         TEXT NOT NULL,
    area                   TEXT NOT NULL,
    latitude               REAL,
    longitude              REAL,
    accepted_categories    TEXT NOT NULL DEFAULT '[]',
    storage_capabilities   TEXT NOT NULL DEFAULT '[]',
    max_quantity_kg_equiv  REAL,
    collection_days        TEXT NOT NULL DEFAULT '[]',
    collection_start_hour  INTEGER NOT NULL DEFAULT 8,
    collection_end_hour    INTEGER NOT NULL DEFAULT 18,
    verified               INTEGER NOT NULL DEFAULT 0,
    contact_email          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS donations (
    id                   TEXT PRIMARY KEY,
    donor_id             TEXT NOT NULL REFERENCES donors(id),
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    category             TEXT NOT NULL,
    quantity_value       REAL NOT NULL,
    quantity_unit        TEXT NOT NULL,
    area                 TEXT NOT NULL,
    latitude             REAL,
    longitude            REAL,
    collection_deadline  TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    status               TEXT NOT NULL,
    storage              TEXT NOT NULL,
    allergens            TEXT NOT NULL DEFAULT '[]',
    allergens_declared   INTEGER NOT NULL DEFAULT 0,
    handling_notes       TEXT NOT NULL DEFAULT '',
    best_before          TEXT,
    closed_to_claims     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claims (
    id             TEXT PRIMARY KEY,
    donation_id    TEXT NOT NULL REFERENCES donations(id),
    recipient_id   TEXT NOT NULL REFERENCES recipients(id),
    claimed_at     TEXT NOT NULL,
    collected_at   TEXT,
    quantity_value REAL NOT NULL DEFAULT 0,
    pickup_code    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'reserved'
);

CREATE INDEX IF NOT EXISTS idx_donations_status ON donations(status);
CREATE INDEX IF NOT EXISTS idx_donations_donor ON donations(donor_id);
CREATE INDEX IF NOT EXISTS idx_claims_recipient ON claims(recipient_id);
CREATE INDEX IF NOT EXISTS idx_claims_donation ON claims(donation_id);
"""

# Created after migration (the column it indexes may not exist on legacy DBs
# until the migration adds it). Partial so legacy blank codes don't collide.
_PICKUP_CODE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_pickup_code "
    "ON claims(pickup_code) WHERE pickup_code != ''"
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Repository:
    """Domain-object persistence backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ensure_parent_dir()
        self._initialise()

    def _ensure_parent_dir(self) -> None:
        parent = Path(self.db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    def _initialise(self) -> None:
        """Create the schema (fresh DBs) and migrate legacy DBs to the current
        version. Runs on a dedicated autocommit connection so the migration can
        manage its own explicit transaction."""
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # autocommit; migration uses explicit BEGIN
        conn.row_factory = sqlite3.Row
        try:
            # Foreign keys OFF during setup: a v1->v2 migration rebuilds the
            # claims table, and FK enforcement must be disabled for that
            # (per the SQLite table-rebuild procedure). Runtime connections
            # (_connect) enable foreign keys normally.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(_PICKUP_CODE_INDEX)  # safe: columns now guaranteed
        finally:
            conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, committing on success and closing always."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a ``BEGIN IMMEDIATE`` write transaction.

        ``IMMEDIATE`` takes the write lock up front, serialising concurrent
        writers so a read-then-write (e.g. the reservation sum-check in M2)
        cannot interleave. Combined with ``busy_timeout``, a blocked writer
        waits rather than failing immediately. (Provided for M2; the live
        claim flow still uses :meth:`_connect` in M1.)
        """
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Migration (versioned via PRAGMA user_version, applied stepwise)
    # ------------------------------------------------------------------
    def _backup_path(self, target_version: int) -> Path:
        """Backup filename for the snapshot taken *before* migrating to
        ``target_version`` (so it holds the ``target_version - 1`` DB)."""
        p = Path(self.db_path)
        return p.with_name(f"{p.stem}.pre-v{target_version}{p.suffix}")

    def _table_counts(self) -> dict[str, int]:
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("donors", "donations", "claims")
            }
        finally:
            conn.close()

    def _verify_backup(
        self, backup: Path, expected_version: int,
        live_counts: dict[str, int] | None,
    ) -> None:
        """Confirm the backup opens, is intact, is a snapshot at
        ``expected_version`` with the core tables, and (when just created)
        matches the live row counts. Raises :class:`MigrationError` otherwise."""
        try:
            conn = sqlite3.connect(str(backup))
        except sqlite3.Error as exc:
            raise MigrationError(f"Migration backup {backup} cannot be opened: {exc}")
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError(f"Migration backup {backup} failed integrity check.")
            actual_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if actual_version != expected_version:
                raise MigrationError(
                    f"Migration backup {backup} is at version {actual_version}, "
                    f"expected a pre-migration (v{expected_version}) snapshot."
                )
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"donors", "donations", "claims"} <= tables:
                raise MigrationError(
                    f"Migration backup {backup} is missing expected tables."
                )
            if live_counts is not None:
                for table, expected in live_counts.items():
                    actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if actual != expected:
                        raise MigrationError(
                            f"Migration backup {backup} row count mismatch for "
                            f"{table}: {actual} != {expected}."
                        )
        except sqlite3.Error as exc:
            raise MigrationError(f"Migration backup {backup} is unreadable: {exc}")
        finally:
            conn.close()

    def _backup_before_migration(self, target_version: int) -> Path:
        """Snapshot the current DB to ``<name>.pre-v{target}.<suffix>`` before
        migrating it to ``target_version``.

        Never overwrites an existing backup: a valid one (at the expected
        pre-migration version) is reused, an invalid one aborts. A freshly
        created backup is verified against the live row counts first. Restore
        (if a migration fails): stop the app and replace the live DB file with
        this backup.
        """
        expected_version = target_version - 1
        backup = self._backup_path(target_version)
        if backup.exists():
            self._verify_backup(backup, expected_version, live_counts=None)
            return backup
        live_counts = self._table_counts()
        escaped = str(backup).replace("'", "''")
        try:
            conn = sqlite3.connect(self.db_path)
            conn.isolation_level = None  # VACUUM cannot run inside a transaction
            try:
                conn.execute(f"VACUUM INTO '{escaped}'")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise MigrationError(f"Could not create migration backup {backup}: {exc}")
        self._verify_backup(backup, expected_version, live_counts=live_counts)
        return backup

    @staticmethod
    def _has_unique_donation_index(conn: sqlite3.Connection) -> bool:
        for idx in conn.execute("PRAGMA index_list(claims)").fetchall():
            name, unique = idx[1], idx[2]
            if unique:
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
                if cols == ["donation_id"]:
                    return True
        return False

    def _is_current_shape(self, conn: sqlite3.Connection) -> bool:
        claim_cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
        donation_cols = {r[1] for r in conn.execute("PRAGMA table_info(donations)")}
        return (
            {"quantity_value", "pickup_code", "status"} <= claim_cols
            and "closed_to_claims" in donation_cols
            and not self._has_unique_donation_index(conn)
        )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring an existing database up to ``_SCHEMA_VERSION``, one step at a
        time. Fresh databases are stamped; legacy databases are backed up and
        migrated inside transactions that roll back on any error.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION:
            return
        if version == 0 and self._is_current_shape(conn):
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        if version == 0:
            self._migrate_v0_to_v1(conn)
            version = 1
        if version == 1:
            self._migrate_v1_to_v2(conn)
            version = 2

    def _migrate_v0_to_v1(self, conn: sqlite3.Connection) -> None:
        """Add partial-claim columns and backfill legacy claims."""
        self._backup_before_migration(target_version=1)
        claim_cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
        donation_cols = {r[1] for r in conn.execute("PRAGMA table_info(donations)")}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "quantity_value" not in claim_cols:
                conn.execute(
                    "ALTER TABLE claims ADD COLUMN quantity_value REAL NOT NULL DEFAULT 0"
                )
            if "pickup_code" not in claim_cols:
                conn.execute(
                    "ALTER TABLE claims ADD COLUMN pickup_code TEXT NOT NULL DEFAULT ''"
                )
            if "status" not in claim_cols:
                conn.execute(
                    "ALTER TABLE claims ADD COLUMN status TEXT NOT NULL DEFAULT 'reserved'"
                )
            if "closed_to_claims" not in donation_cols:
                conn.execute(
                    "ALTER TABLE donations ADD COLUMN closed_to_claims "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            rows = conn.execute(
                "SELECT c.id AS cid, c.collected_at AS collected_at, "
                "d.quantity_value AS qv "
                "FROM claims c JOIN donations d ON d.id = c.donation_id"
            ).fetchall()
            for row in rows:
                status = (ClaimStatus.COLLECTED.value if row["collected_at"]
                          else ClaimStatus.RESERVED.value)
                code = self._unique_pickup_code(conn)
                conn.execute(
                    "UPDATE claims SET quantity_value = ?, status = ?, "
                    "pickup_code = ? WHERE id = ?",
                    (row["qv"], status, code, row["cid"]),
                )
            conn.execute("PRAGMA user_version = 1")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """Drop the one-claim-per-donation UNIQUE(donation_id) by rebuilding
        the claims table. Preserves every claim row and pickup code.

        Foreign keys are already OFF for the setup connection (see
        ``_initialise``), as required for the SQLite table-rebuild procedure.
        """
        self._backup_before_migration(target_version=2)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """CREATE TABLE claims_new (
                    id             TEXT PRIMARY KEY,
                    donation_id    TEXT NOT NULL REFERENCES donations(id),
                    recipient_id   TEXT NOT NULL REFERENCES recipients(id),
                    claimed_at     TEXT NOT NULL,
                    collected_at   TEXT,
                    quantity_value REAL NOT NULL DEFAULT 0,
                    pickup_code    TEXT NOT NULL DEFAULT '',
                    status         TEXT NOT NULL DEFAULT 'reserved'
                )"""
            )
            conn.execute(
                """INSERT INTO claims_new
                   (id, donation_id, recipient_id, claimed_at, collected_at,
                    quantity_value, pickup_code, status)
                   SELECT id, donation_id, recipient_id, claimed_at, collected_at,
                          quantity_value, pickup_code, status FROM claims"""
            )
            conn.execute("DROP TABLE claims")
            conn.execute("ALTER TABLE claims_new RENAME TO claims")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_recipient ON claims(recipient_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_donation ON claims(donation_id)"
            )
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Pickup codes
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_pickup_code() -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))

    def _unique_pickup_code(self, conn: sqlite3.Connection) -> str:
        for _ in range(20):
            code = self._generate_pickup_code()
            exists = conn.execute(
                "SELECT 1 FROM claims WHERE pickup_code = ?", (code,)
            ).fetchone()
            if exists is None:
                return code
        raise MigrationError("Could not generate a unique pickup code.")

    # ------------------------------------------------------------------
    # Donors
    # ------------------------------------------------------------------
    def add_donor(self, donor: Donor) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO donors
                   (id, name, donor_type, area, latitude, longitude, contact_email)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    donor.id, donor.name, donor.donor_type.value,
                    donor.location.area, donor.location.latitude,
                    donor.location.longitude, donor.contact_email,
                ),
            )

    def get_donor(self, donor_id: str) -> Donor:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM donors WHERE id = ?", (donor_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Donor {donor_id!r} not found")
        return self._row_to_donor(row)

    def list_donors(self) -> list[Donor]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM donors ORDER BY name").fetchall()
        return [self._row_to_donor(r) for r in rows]

    # ------------------------------------------------------------------
    # Recipients
    # ------------------------------------------------------------------
    def add_recipient(self, recipient: Recipient) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO recipients
                   (id, name, recipient_type, area, latitude, longitude,
                    accepted_categories, storage_capabilities, max_quantity_kg_equiv,
                    collection_days, collection_start_hour, collection_end_hour,
                    verified, contact_email)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    recipient.id, recipient.name, recipient.recipient_type.value,
                    recipient.location.area, recipient.location.latitude,
                    recipient.location.longitude,
                    json.dumps(sorted(c.value for c in recipient.accepted_categories)),
                    json.dumps(sorted(s.value for s in recipient.storage_capabilities)),
                    recipient.max_quantity_kg_equiv,
                    json.dumps(sorted(recipient.collection_days)),
                    recipient.collection_start_hour, recipient.collection_end_hour,
                    int(recipient.verified), recipient.contact_email,
                ),
            )

    def get_recipient(self, recipient_id: str) -> Recipient:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recipients WHERE id = ?", (recipient_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Recipient {recipient_id!r} not found")
        return self._row_to_recipient(row)

    def list_recipients(self) -> list[Recipient]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recipients ORDER BY name").fetchall()
        return [self._row_to_recipient(r) for r in rows]

    # ------------------------------------------------------------------
    # Donations
    # ------------------------------------------------------------------
    def add_donation(self, donation: Donation) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO donations
                   (id, donor_id, title, description, category,
                    quantity_value, quantity_unit, area, latitude, longitude,
                    collection_deadline, created_at, status, storage,
                    allergens, allergens_declared, handling_notes, best_before,
                    closed_to_claims)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    donation.id, donation.donor_id, donation.title,
                    donation.description, donation.category.value,
                    donation.quantity.value, donation.quantity.unit.value,
                    donation.location.area, donation.location.latitude,
                    donation.location.longitude,
                    _iso(donation.collection_deadline), _iso(donation.created_at),
                    donation.status.value, donation.storage.value,
                    json.dumps(donation.allergens), int(donation.allergens_declared),
                    donation.handling_notes, _iso(donation.best_before),
                    int(donation.closed_to_claims),
                ),
            )

    def get_donation(self, donation_id: str) -> Donation:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM donations WHERE id = ?", (donation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Donation {donation_id!r} not found")
        return self._row_to_donation(row)

    def list_donations(
        self,
        *,
        status: DonationStatus | None = None,
        donor_id: str | None = None,
    ) -> list[Donation]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if donor_id is not None:
            clauses.append("donor_id = ?")
            params.append(donor_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM donations {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [self._row_to_donation(r) for r in rows]

    def set_donation_status(self, donation_id: str, status: DonationStatus) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE donations SET status = ? WHERE id = ?",
                (status.value, donation_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Donation {donation_id!r} not found")

    # ------------------------------------------------------------------
    # Claims — partial reservation with atomic availability checking
    # ------------------------------------------------------------------
    def create_claim(
        self, *, claim_id: str, donation_id: str, recipient_id: str,
        claimed_at: datetime, quantity: float | None = None,
        now: datetime | None = None, recipient_capacity: float | None = None,
    ) -> Claim:
        """Reserve a quantity of a donation, atomically.

        Availability check, reservation sum, insert, and donation-status
        recomputation all happen inside one ``BEGIN IMMEDIATE`` transaction,
        so concurrent reservers cannot both pass the check — the second waits
        and sees the updated sum. ``quantity=None`` reserves all remaining
        (whole-listing claim). Raises on a closed donation, a passed deadline,
        or a request exceeding the remaining quantity — in which case nothing
        is written. When ``recipient_capacity`` is given (the service passes it
        only where capacity applies), the recipient's held + requested amount
        on this donation is re-checked against it in the same transaction.
        """
        now = now or datetime.now()
        with self._write_txn() as conn:
            drow = conn.execute(
                "SELECT quantity_value, collection_deadline, closed_to_claims "
                "FROM donations WHERE id = ?", (donation_id,),
            ).fetchone()
            if drow is None:
                raise NotFoundError(f"Donation {donation_id!r} not found")
            if drow["closed_to_claims"]:
                raise ClaimsClosedError(
                    f"Donation {donation_id!r} is closed to new claims."
                )
            deadline = _parse_dt(drow["collection_deadline"])
            if now >= deadline:
                raise InvalidTransitionError(
                    "The collection deadline has passed; this donation can no "
                    "longer be claimed."
                )
            total = Decimal(str(drow["quantity_value"]))
            # Sum per row via Decimal(str(...)), as reservation.py does — SQLite's
            # SUM() over REALs carries binary drift (0.1 + 0.2 != 0.3), which
            # would wrongly reject claiming an exact remainder.
            active = conn.execute(
                "SELECT recipient_id, quantity_value FROM claims "
                "WHERE donation_id = ? AND status IN (?, ?)",
                (donation_id, ClaimStatus.RESERVED.value, ClaimStatus.COLLECTED.value),
            ).fetchall()
            consumed = sum((Decimal(str(r["quantity_value"])) for r in active), Decimal(0))
            remaining = total - consumed
            qty = remaining if quantity is None else Decimal(str(quantity))
            if qty <= 0:
                raise OverReservationError(
                    f"No quantity available to reserve (remaining {remaining})."
                )
            if qty > remaining:
                raise OverReservationError(
                    f"Requested {qty} exceeds the remaining {remaining}."
                )
            if recipient_capacity is not None:
                held = sum(
                    (Decimal(str(r["quantity_value"])) for r in active
                     if r["recipient_id"] == recipient_id),
                    Decimal(0),
                )
                if held + qty > Decimal(str(recipient_capacity)):
                    raise EligibilityError(
                        f"Requested {qty} would exceed the recipient's capacity "
                        f"of {recipient_capacity:g} per donation (already holds {held})."
                    )
            code = self._unique_pickup_code(conn)
            conn.execute(
                """INSERT INTO claims
                   (id, donation_id, recipient_id, claimed_at,
                    quantity_value, pickup_code, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (claim_id, donation_id, recipient_id, _iso(claimed_at),
                 float(qty), code, ClaimStatus.RESERVED.value),
            )
            self._recompute_in_conn(conn, donation_id, now)
        return Claim(
            id=claim_id, donation_id=donation_id, recipient_id=recipient_id,
            claimed_at=claimed_at, quantity_value=float(qty),
            pickup_code=code, status=ClaimStatus.RESERVED,
        )

    def confirm_collection(
        self, claim_id: str, collected_at: datetime, *, now: datetime | None = None
    ) -> Claim:
        """Confirm a specific RESERVED claim as collected (the only success
        path). Enforces the deadline inclusively (allowed while now <= deadline)
        and recomputes the donation's status. Raises if the claim is not
        RESERVED or the deadline has passed."""
        now = now or collected_at
        with self._write_txn() as conn:
            crow = conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if crow is None:
                raise NotFoundError(f"Claim {claim_id!r} not found")
            if crow["status"] != ClaimStatus.RESERVED.value:
                raise InvalidTransitionError(
                    f"Claim is '{crow['status']}', only reserved claims can be "
                    "confirmed as collected."
                )
            drow = conn.execute(
                "SELECT collection_deadline FROM donations WHERE id = ?",
                (crow["donation_id"],),
            ).fetchone()
            if now > _parse_dt(drow["collection_deadline"]):
                raise InvalidTransitionError(
                    "The collection deadline has passed; collection cannot be "
                    "confirmed."
                )
            conn.execute(
                "UPDATE claims SET collected_at = ?, status = ? WHERE id = ?",
                (_iso(collected_at), ClaimStatus.COLLECTED.value, claim_id),
            )
            self._recompute_in_conn(conn, crow["donation_id"], now)
            final = conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
        return self._row_to_claim(final)

    def confirm_handover(
        self, donor_id: str, code: str, collected_at: datetime,
        *, now: datetime | None = None,
    ) -> Claim:
        """Donor-side handover confirmation by pickup code, atomically.

        In one ``BEGIN IMMEDIATE`` transaction, verifies the code resolves to a
        claim on a donation owned by ``donor_id``, the claim is still RESERVED,
        and the deadline has not passed (inclusive: allowed while
        ``now <= deadline``), then marks it COLLECTED. Any failure leaves the
        database unchanged. Codes are single-use: a confirmed claim is no
        longer RESERVED, so re-submitting the same code is rejected.

        A code that does not exist — or that belongs to a different donor — is
        reported identically (``NotFoundError``) so the workflow never reveals
        another donor's valid code.
        """
        now = now or collected_at
        code = (code or "").strip().upper()
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT c.id AS cid, c.status AS status, c.donation_id AS did, "
                "d.donor_id AS donor_id, d.collection_deadline AS deadline "
                "FROM claims c JOIN donations d ON d.id = c.donation_id "
                "WHERE c.pickup_code = ?", (code,),
            ).fetchone()
            if row is None or row["donor_id"] != donor_id:
                raise NotFoundError("No reserved pickup matches that code.")
            if row["status"] != ClaimStatus.RESERVED.value:
                raise InvalidTransitionError(
                    "This pickup code has already been used or is no longer reserved."
                )
            if now > _parse_dt(row["deadline"]):
                raise InvalidTransitionError(
                    "The collection deadline has passed; the handover cannot be "
                    "confirmed."
                )
            conn.execute(
                "UPDATE claims SET collected_at = ?, status = ? WHERE id = ?",
                (_iso(collected_at), ClaimStatus.COLLECTED.value, row["cid"]),
            )
            self._recompute_in_conn(conn, row["did"], now)
            final = conn.execute(
                "SELECT * FROM claims WHERE id = ?", (row["cid"],)
            ).fetchone()
        return self._row_to_claim(final)

    def cancel_claim(self, claim_id: str, *, now: datetime | None = None) -> None:
        """Release a RESERVED claim: mark it CANCELLED (history preserved),
        freeing its quantity, and recompute the donation's status. Collected,
        cancelled, or expired claims cannot be cancelled."""
        now = now or datetime.now()
        with self._write_txn() as conn:
            crow = conn.execute(
                "SELECT donation_id, status FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if crow is None:
                raise NotFoundError(f"Claim {claim_id!r} not found")
            if crow["status"] != ClaimStatus.RESERVED.value:
                raise InvalidTransitionError(
                    f"Claim is '{crow['status']}' and cannot be cancelled."
                )
            conn.execute(
                "UPDATE claims SET status = ? WHERE id = ?",
                (ClaimStatus.CANCELLED.value, claim_id),
            )
            self._recompute_in_conn(conn, crow["donation_id"], now)

    def close_to_claims(self, donation_id: str, *, now: datetime | None = None) -> DonationStatus:
        """Donor withdrawal: stop NEW claims without touching existing
        reservations or collected history. Recomputes the donation status
        (CANCELLED if nothing is committed, otherwise RESERVED/COLLECTED)."""
        now = now or datetime.now()
        with self._write_txn() as conn:
            drow = conn.execute(
                "SELECT 1 FROM donations WHERE id = ?", (donation_id,)
            ).fetchone()
            if drow is None:
                raise NotFoundError(f"Donation {donation_id!r} not found")
            conn.execute(
                "UPDATE donations SET closed_to_claims = 1 WHERE id = ?", (donation_id,)
            )
            return self._recompute_in_conn(conn, donation_id, now)

    def expire_overdue(self, now: datetime) -> list[str]:
        """Lapse past-deadline, uncollected donations. Reserved claims on an
        expired donation become EXPIRED (freeing nothing useful — the listing
        is closing); collected claims are untouched. Returns the expired
        donation ids."""
        expired: list[str] = []
        active = (DonationStatus.AVAILABLE.value, DonationStatus.CLAIMED.value,
                  DonationStatus.RESERVED.value)
        with self._write_txn() as conn:
            rows = conn.execute(
                f"SELECT id, collection_deadline FROM donations "
                f"WHERE status IN ({','.join('?' * len(active))})", active,
            ).fetchall()
            for row in rows:
                if now > _parse_dt(row["collection_deadline"]):
                    conn.execute(
                        "UPDATE claims SET status = ? WHERE donation_id = ? AND status = ?",
                        (ClaimStatus.EXPIRED.value, row["id"], ClaimStatus.RESERVED.value),
                    )
                    self._recompute_in_conn(conn, row["id"], now)
                    expired.append(row["id"])
        return expired

    def _recompute_in_conn(
        self, conn: sqlite3.Connection, donation_id: str, now: datetime
    ) -> DonationStatus:
        """Recompute and persist a donation's status from its claims, on an
        already-open transaction/connection."""
        drow = conn.execute(
            "SELECT quantity_value, collection_deadline, closed_to_claims "
            "FROM donations WHERE id = ?", (donation_id,),
        ).fetchone()
        claims = [
            self._row_to_claim(r) for r in conn.execute(
                "SELECT * FROM claims WHERE donation_id = ?", (donation_id,)
            ).fetchall()
        ]
        status = derive_donation_status(
            drow["quantity_value"], claims, now=now,
            deadline=_parse_dt(drow["collection_deadline"]),
            closed_to_claims=bool(drow["closed_to_claims"]),
        )
        conn.execute(
            "UPDATE donations SET status = ? WHERE id = ?", (status.value, donation_id)
        )
        return status

    def get_claim_for_donation(self, donation_id: str) -> Claim | None:
        """Return a single claim for the donation (earliest). Ambiguous once a
        donation has multiple partial claims; kept as a bridge for existing
        donation-level callers until M4 moves them to claim-level queries."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM claims WHERE donation_id = ? ORDER BY claimed_at LIMIT 1",
                (donation_id,),
            ).fetchone()
        return self._row_to_claim(row) if row else None

    def list_claims(self, *, recipient_id: str | None = None) -> list[Claim]:
        clause = "WHERE recipient_id = ?" if recipient_id else ""
        params = (recipient_id,) if recipient_id else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM claims {clause} ORDER BY claimed_at DESC", params
            ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    def list_claims_for_donation(self, donation_id: str) -> list[Claim]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE donation_id = ? ORDER BY claimed_at",
                (donation_id,),
            ).fetchall()
        return [self._row_to_claim(r) for r in rows]

    # ------------------------------------------------------------------
    # Reservation / availability (pure maths lives in reservation.py)
    # ------------------------------------------------------------------
    def donation_availability(self, donation_id: str) -> DonationAvailability:
        """Reserved / collected / remaining for a donation (in its unit)."""
        donation = self.get_donation(donation_id)
        claims = self.list_claims_for_donation(donation_id)
        return available_quantity(donation.quantity.value, claims)

    def recompute_donation_status(self, donation_id: str, *, now: datetime) -> DonationStatus:
        """Recompute and persist a donation's status from its claims.

        Encodes the status precedence (see ``reservation.derive_donation_status``).
        Provided for M2 to call after each claim mutation; the M1 live claim
        flow does not call it yet (it keeps the legacy AVAILABLE/CLAIMED/
        COLLECTED path), so existing behaviour is unchanged.
        """
        donation = self.get_donation(donation_id)
        claims = self.list_claims_for_donation(donation_id)
        status = derive_donation_status(
            donation.quantity.value, claims, now=now,
            deadline=donation.collection_deadline,
            closed_to_claims=donation.closed_to_claims,
        )
        self.set_donation_status(donation_id, status)
        return status

    # ------------------------------------------------------------------
    # Row -> dataclass mappers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_location(row: sqlite3.Row) -> Location:
        return Location(
            area=row["area"], latitude=row["latitude"], longitude=row["longitude"]
        )

    def _row_to_donor(self, row: sqlite3.Row) -> Donor:
        return Donor(
            id=row["id"], name=row["name"],
            donor_type=DonorType(row["donor_type"]),
            location=self._row_to_location(row),
            contact_email=row["contact_email"],
        )

    def _row_to_recipient(self, row: sqlite3.Row) -> Recipient:
        return Recipient(
            id=row["id"], name=row["name"],
            recipient_type=RecipientType(row["recipient_type"]),
            location=self._row_to_location(row),
            accepted_categories={
                FoodCategory(c) for c in json.loads(row["accepted_categories"])
            },
            storage_capabilities={
                StorageRequirement(s) for s in json.loads(row["storage_capabilities"])
            },
            max_quantity_kg_equiv=row["max_quantity_kg_equiv"],
            collection_days=set(json.loads(row["collection_days"])),
            collection_start_hour=row["collection_start_hour"],
            collection_end_hour=row["collection_end_hour"],
            verified=bool(row["verified"]),
            contact_email=row["contact_email"],
        )

    def _row_to_donation(self, row: sqlite3.Row) -> Donation:
        return Donation(
            id=row["id"], donor_id=row["donor_id"], title=row["title"],
            description=row["description"], category=FoodCategory(row["category"]),
            quantity=Quantity(row["quantity_value"], Unit(row["quantity_unit"])),
            location=self._row_to_location(row),
            collection_deadline=_parse_dt(row["collection_deadline"]),
            created_at=_parse_dt(row["created_at"]),
            status=DonationStatus(row["status"]),
            storage=StorageRequirement(row["storage"]),
            allergens=json.loads(row["allergens"]),
            allergens_declared=bool(row["allergens_declared"]),
            handling_notes=row["handling_notes"],
            best_before=_parse_dt(row["best_before"]),
            closed_to_claims=bool(row["closed_to_claims"]),
        )

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        return Claim(
            id=row["id"], donation_id=row["donation_id"],
            recipient_id=row["recipient_id"],
            claimed_at=_parse_dt(row["claimed_at"]),
            collected_at=_parse_dt(row["collected_at"]),
            quantity_value=row["quantity_value"],
            pickup_code=row["pickup_code"],
            status=ClaimStatus(row["status"]),
        )
