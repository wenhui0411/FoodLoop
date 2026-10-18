"""Tests for the persistence layer.

Focus areas (critical behaviour from the brief):
* round-trip persistence of domain objects,
* duplicate-claim prevention,
* explicit status transitions (claim / collect / cancel),
* a claim never silently counting as a collection.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from foodloop.repository import Repository

from foodloop.errors import (
    InvalidTransitionError,
    NotFoundError,
    OverReservationError,
)
from foodloop.models import (
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _donor() -> Donor:
    return Donor("d1", "Test Bakery", DonorType.BAKERY, Location("Central"))


def _recipient(rid: str = "r1", verified: bool = True) -> Recipient:
    return Recipient(
        rid, f"Org {rid}", RecipientType.FOOD_BANK, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        verified=verified,
    )


def _donation(now: datetime, did: str = "dn1") -> Donation:
    return Donation(
        did, "d1", "Bread", "Fresh bread", FoodCategory.BAKERY,
        Quantity(10, Unit.LOAVES), Location("Central"),
        collection_deadline=now + timedelta(days=1), created_at=now,
    )


# --------------------------------------------------------------------------
# Round-trip persistence
# --------------------------------------------------------------------------
def test_donation_roundtrip_preserves_fields(repo, now):
    repo.add_donor(_donor())
    original = _donation(now)
    original.allergens = ["gluten"]
    original.allergens_declared = True
    repo.add_donation(original)

    loaded = repo.get_donation("dn1")
    assert loaded.title == "Bread"
    assert loaded.category is FoodCategory.BAKERY
    assert loaded.quantity == Quantity(10, Unit.LOAVES)
    assert loaded.allergens == ["gluten"]
    assert loaded.allergens_declared is True
    assert loaded.status is DonationStatus.AVAILABLE


def test_recipient_roundtrip_preserves_sets(repo):
    r = _recipient()
    r.accepted_categories = {FoodCategory.BAKERY, FoodCategory.DAIRY}
    r.collection_days = {"Mon", "Wed"}
    repo.add_recipient(r)

    loaded = repo.get_recipient("r1")
    assert loaded.accepted_categories == {FoodCategory.BAKERY, FoodCategory.DAIRY}
    assert loaded.collection_days == {"Mon", "Wed"}
    assert loaded.verified is True


def test_get_missing_entities_raise(repo):
    with pytest.raises(NotFoundError):
        repo.get_donation("nope")
    with pytest.raises(NotFoundError):
        repo.get_recipient("nope")


# --------------------------------------------------------------------------
# Reservation (partial quantities) + status
# --------------------------------------------------------------------------
def test_whole_listing_claim_reserves_and_marks_reserved(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient())
    repo.add_donation(_donation(now))  # Quantity(10, LOAVES)

    claim = repo.create_claim(
        claim_id="c1", donation_id="dn1", recipient_id="r1", claimed_at=now, now=now
    )
    assert claim.donation_id == "dn1"
    assert claim.quantity_value == 10            # reserved all remaining
    assert claim.pickup_code                      # a code was issued
    assert claim.status is ClaimStatus.RESERVED
    assert claim.is_collected is False
    # Fully reserved -> donation RESERVED.
    assert repo.get_donation("dn1").status is DonationStatus.RESERVED


def test_two_partial_claims_within_total_succeed(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient("r1"))
    repo.add_recipient(_recipient("r2"))
    repo.add_donation(_donation(now))  # total 10

    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, quantity=4, now=now)
    repo.create_claim(claim_id="c2", donation_id="dn1", recipient_id="r2",
                      claimed_at=now, quantity=6, now=now)

    avail = repo.donation_availability("dn1")
    assert avail.reserved == 10 and avail.remaining == 0
    assert repo.get_donation("dn1").status is DonationStatus.RESERVED


def test_over_limit_claim_rejected_without_db_change(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient("r1"))
    repo.add_recipient(_recipient("r2"))
    repo.add_donation(_donation(now))  # total 10

    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, quantity=7, now=now)
    with pytest.raises(OverReservationError):
        repo.create_claim(claim_id="c2", donation_id="dn1", recipient_id="r2",
                          claimed_at=now, quantity=5, now=now)  # 7+5 > 10

    # Nothing changed: only the first claim exists, 7 reserved.
    claims = repo.list_claims_for_donation("dn1")
    assert len(claims) == 1
    assert repo.donation_availability("dn1").reserved == 7


def test_concurrent_reservations_cannot_overreserve(repo, now):
    """Two simultaneous reservers of 7 (total 10): BEGIN IMMEDIATE serialises
    the writers, so exactly one succeeds and the other sees the updated sum
    and is rejected — the invariant Σ reserved ≤ total always holds."""
    repo.add_donor(_donor())
    repo.add_recipient(_recipient("r1"))
    repo.add_recipient(_recipient("r2"))
    repo.add_donation(_donation(now))  # total 10

    # Separate Repository instances on the same DB file (connection-per-op).
    rp1 = Repository(repo.db_path)
    rp2 = Repository(repo.db_path)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def attempt(rp: Repository, claim_id: str, recipient_id: str) -> None:
        barrier.wait()  # maximise contention
        try:
            rp.create_claim(claim_id=claim_id, donation_id="dn1",
                            recipient_id=recipient_id, claimed_at=now,
                            quantity=7, now=now)
            results[claim_id] = "ok"
        except OverReservationError:
            results[claim_id] = "over"
        except Exception as exc:  # pragma: no cover - surfaces unexpected errors
            results[claim_id] = f"err:{type(exc).__name__}"

    t1 = threading.Thread(target=attempt, args=(rp1, "c1", "r1"))
    t2 = threading.Thread(target=attempt, args=(rp2, "c2", "r2"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(results.values()) == ["ok", "over"], results
    assert repo.donation_availability("dn1").reserved == 7   # never 14
    assert len(repo.list_claims_for_donation("dn1")) == 1


def test_concurrent_handover_confirmation_is_single_use(repo, now):
    """Two simultaneous confirmations of the same pickup code: BEGIN IMMEDIATE
    serialises them, so exactly one collects and the other is rejected."""
    repo.add_donor(_donor())  # d1
    repo.add_recipient(_recipient("r1"))
    repo.add_donation(_donation(now))  # dn1 owned by d1
    claim = repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                              claimed_at=now, now=now)
    code = claim.pickup_code

    rp1 = Repository(repo.db_path)
    rp2 = Repository(repo.db_path)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def attempt(rp: Repository, key: str) -> None:
        barrier.wait()
        try:
            rp.confirm_handover("d1", code, now, now=now)
            results[key] = "ok"
        except InvalidTransitionError:
            results[key] = "used"
        except Exception as exc:  # pragma: no cover
            results[key] = f"err:{type(exc).__name__}"

    t1 = threading.Thread(target=attempt, args=(rp1, "a"))
    t2 = threading.Thread(target=attempt, args=(rp2, "b"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(results.values()) == ["ok", "used"], results
    collected = [c for c in repo.list_claims_for_donation("dn1")
                 if c.status is ClaimStatus.COLLECTED]
    assert len(collected) == 1


# --------------------------------------------------------------------------
# Collection + cancellation (by claim)
# --------------------------------------------------------------------------
def test_confirm_requires_a_reserved_claim(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient())
    repo.add_donation(_donation(now))
    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, now=now)
    repo.cancel_claim("c1", now=now)  # -> CANCELLED

    with pytest.raises(InvalidTransitionError):
        repo.confirm_collection("c1", now, now=now)
    with pytest.raises(NotFoundError):
        repo.confirm_collection("nope", now, now=now)


def test_claim_then_confirm_collection_marks_collected(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient())
    repo.add_donation(_donation(now))
    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, now=now)

    collected_at = now + timedelta(hours=3)
    claim = repo.confirm_collection("c1", collected_at, now=collected_at)

    assert repo.get_donation("dn1").status is DonationStatus.COLLECTED
    assert claim.collected_at == collected_at
    assert claim.status is ClaimStatus.COLLECTED


def test_cancel_claim_frees_quantity_and_allows_reclaim(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient("r1"))
    repo.add_recipient(_recipient("r2"))
    repo.add_donation(_donation(now))

    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, now=now)
    repo.cancel_claim("c1", now=now)
    assert repo.get_donation("dn1").status is DonationStatus.AVAILABLE
    assert repo.donation_availability("dn1").remaining == 10  # freed

    # The cancelled claim is preserved (history), and the quantity is reclaimable.
    statuses = {c.id: c.status for c in repo.list_claims_for_donation("dn1")}
    assert statuses["c1"] is ClaimStatus.CANCELLED
    repo.create_claim(claim_id="c2", donation_id="dn1", recipient_id="r2",
                      claimed_at=now, now=now)
    assert repo.get_donation("dn1").status is DonationStatus.RESERVED


def test_cannot_cancel_a_collected_claim(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient())
    repo.add_donation(_donation(now))
    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=now, now=now)
    repo.confirm_collection("c1", now + timedelta(hours=1), now=now + timedelta(hours=1))

    with pytest.raises(InvalidTransitionError):
        repo.cancel_claim("c1", now=now + timedelta(hours=2))
    assert repo.get_donation("dn1").status is DonationStatus.COLLECTED


# --------------------------------------------------------------------------
# Listing / filtering
# --------------------------------------------------------------------------
def test_list_donations_filters_by_status_and_donor(repo, now):
    repo.add_donor(_donor())
    repo.add_recipient(_recipient())
    repo.add_donation(_donation(now, "dn1"))
    repo.add_donation(_donation(now, "dn2"))
    repo.create_claim(claim_id="c1", donation_id="dn2", recipient_id="r1",
                      claimed_at=now, now=now)  # dn2 -> RESERVED

    available = repo.list_donations(status=DonationStatus.AVAILABLE)
    assert [d.id for d in available] == ["dn1"]

    by_donor = repo.list_donations(donor_id="d1")
    assert {d.id for d in by_donor} == {"dn1", "dn2"}
