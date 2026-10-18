"""Tests for claim- and quantity-accurate donor / recipient impact."""

from __future__ import annotations

from datetime import timedelta

from foodloop import impact
from foodloop.models import (
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


def _setup(repo):
    repo.add_donor(Donor("d1", "Bakery", DonorType.BAKERY, Location("Central")))
    repo.add_recipient(Recipient(
        "r1", "Food Bank", RecipientType.FOOD_BANK, Location("Central"),
        accepted_categories={FoodCategory.BAKERY}, verified=True,
        storage_capabilities={StorageRequirement.AMBIENT},
    ))
    repo.add_recipient(Recipient(
        "r2", "Shelter", RecipientType.SHELTER, Location("Central"),
        accepted_categories={FoodCategory.BAKERY}, verified=True,
        storage_capabilities={StorageRequirement.AMBIENT},
    ))


def _don(repo, now, did, *, qty=10, unit=Unit.LOAVES, short=False, donor="d1"):
    deadline = now + (timedelta(minutes=1) if short else timedelta(days=1))
    repo.add_donation(Donation(
        did, donor, f"Item {did}", "", FoodCategory.BAKERY, Quantity(qty, unit),
        Location("Central"), collection_deadline=deadline, created_at=now,
        status=DonationStatus.AVAILABLE,
    ))


def _reserve(repo, now, cid, did, rid="r1", qty=None):
    return repo.create_claim(claim_id=cid, donation_id=did, recipient_id=rid,
                             claimed_at=now, quantity=qty, now=now)


def _collect(repo, now, cid, did, rid="r1", qty=None):
    _reserve(repo, now, cid, did, rid, qty)
    repo.confirm_collection(cid, now + timedelta(hours=1), now=now + timedelta(hours=1))


# --------------------------------------------------------------------------
# Donor impact — partial collection, multiple claims, reserved vs collected
# --------------------------------------------------------------------------
def test_donor_impact_partial_collection_counts_claim_quantities(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10)
    _collect(repo, now, "c1", "dn", rid="r1", qty=4)   # collected 4 of 10
    _reserve(repo, now, "c2", "dn", rid="r2", qty=3)   # reserved 3 (not collected)

    di = impact.donor_impact(repo, "d1")
    assert di.total_listed == 1                        # one listing
    assert di.confirmed_collections == 1               # one collected claim
    assert di.reserved_claims == 1
    assert di.collected_by_unit == {"loaves": 4}       # NOT the full 10
    assert di.reserved_by_unit == {"loaves": 3}        # kept separate
    assert di.organisations_helped == 1


def test_donor_impact_counts_multiple_collected_claims(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10)
    _collect(repo, now, "c1", "dn", rid="r1", qty=6)
    _collect(repo, now, "c2", "dn", rid="r2", qty=4)   # fully collected now

    di = impact.donor_impact(repo, "d1")
    assert di.confirmed_collections == 2               # two handovers
    assert di.collected_by_unit == {"loaves": 10}
    assert di.organisations_helped == 2
    assert repo.get_donation("dn").status is DonationStatus.COLLECTED


def test_donor_impact_cancelled_and_reserved_not_collected(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10)
    repo.cancel_claim(_reserve(repo, now, "c1", "dn", qty=4).id, now=now)  # cancelled
    _reserve(repo, now, "c2", "dn", rid="r2", qty=3)                       # reserved

    di = impact.donor_impact(repo, "d1")
    assert di.confirmed_collections == 0
    assert di.collected_by_unit == {}
    assert di.reserved_claims == 1 and di.reserved_by_unit == {"loaves": 3}
    assert di.cancelled_claims == 1


def test_donor_collection_rate_is_claim_based(repo, now):
    _setup(repo)
    _don(repo, now, "dn1", qty=10)
    _collect(repo, now, "c1", "dn1")                   # collected claim
    _don(repo, now, "dn2", qty=10, short=True)
    _reserve(repo, now, "c2", "dn2")                   # will expire
    repo.expire_overdue(now + timedelta(minutes=5))    # c2 -> EXPIRED

    di = impact.donor_impact(repo, "d1")
    assert di.confirmed_collections == 1 and di.expired_claims == 1
    assert di.rate_denominator == 2
    assert di.collection_rate == 0.5


def test_donor_collection_rate_none_when_no_concluded_claims(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10)
    _reserve(repo, now, "c1", "dn")                    # reserved only
    di = impact.donor_impact(repo, "d1")
    assert di.rate_denominator == 0
    assert di.collection_rate is None


def test_donor_impact_multiple_units_not_combined(repo, now):
    _setup(repo)
    _don(repo, now, "dn1", qty=10, unit=Unit.LOAVES)
    _don(repo, now, "dn2", qty=5, unit=Unit.KG)
    _collect(repo, now, "c1", "dn1")
    _collect(repo, now, "c2", "dn2")
    di = impact.donor_impact(repo, "d1")
    assert di.collected_by_unit == {"loaves": 10, "kg": 5}


def test_donor_impact_only_counts_own_donations(repo, now):
    _setup(repo)
    repo.add_donor(Donor("d2", "Other", DonorType.CAFE, Location("Central")))
    _don(repo, now, "mine", qty=10, donor="d1")
    _don(repo, now, "theirs", qty=10, donor="d2")
    _collect(repo, now, "c1", "mine")
    _collect(repo, now, "c2", "theirs")
    assert impact.donor_impact(repo, "d1").confirmed_collections == 1
    assert impact.donor_impact(repo, "d2").confirmed_collections == 1


def test_empty_donor_has_zeroed_impact(repo, now):
    _setup(repo)
    di = impact.donor_impact(repo, "d1")
    assert di.total_listed == 0
    assert di.confirmed_collections == 0
    assert di.collection_rate is None
    assert di.collected_by_unit == {} and di.reserved_by_unit == {}


def test_completed_donation_count_counts_collected_claims(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10)
    _collect(repo, now, "c1", "dn", rid="r1", qty=6)
    _collect(repo, now, "c2", "dn", rid="r2", qty=4)
    assert impact.completed_donation_count(repo) == 2          # two handovers
    assert impact.completed_donation_count(repo, donor_id="d1") == 2


# --------------------------------------------------------------------------
# Recipient impact
# --------------------------------------------------------------------------
def test_recipient_impact_claim_accurate(repo, now):
    _setup(repo)
    _don(repo, now, "dn1", qty=10, unit=Unit.LOAVES)
    _don(repo, now, "dn2", qty=5, unit=Unit.KG)
    _collect(repo, now, "c1", "dn1", rid="r1", qty=10)
    _reserve(repo, now, "c2", "dn2", rid="r1", qty=2)   # reserved, not collected

    ri = impact.recipient_impact(repo, "r1")
    assert ri.confirmed_collections == 1
    assert ri.active_claims == 1
    assert ri.donors_supported == 1
    assert ri.collected_by_unit == {"loaves": 10}
    assert ri.reserved_by_unit == {"kg": 2}


def test_recipient_impact_excludes_expired_claims(repo, now):
    _setup(repo)
    _don(repo, now, "dn", qty=10, short=True)
    _reserve(repo, now, "c1", "dn", rid="r1")
    repo.expire_overdue(now + timedelta(minutes=5))     # c1 -> EXPIRED
    ri = impact.recipient_impact(repo, "r1")
    assert ri.confirmed_collections == 0 and ri.active_claims == 0


def test_recipient_impact_empty(repo, now):
    _setup(repo)
    ri = impact.recipient_impact(repo, "r1")
    assert ri.confirmed_collections == 0
    assert ri.active_claims == 0
    assert ri.donors_supported == 0
    assert ri.collected_by_unit == {} and ri.reserved_by_unit == {}
