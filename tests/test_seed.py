"""Tests that the synthetic seed data loads and holds its invariants."""

from __future__ import annotations

from datetime import timedelta

from foodloop.models import (
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Quantity,
    Unit,
)
from foodloop.models import Donation
from data.seed import ensure_seeded, is_seeded, seed_repository


def test_seed_loads_all_states(repo, now):
    seed_repository(repo, now)

    assert len(repo.list_donors()) == 5
    assert len(repo.list_recipients()) == 5

    statuses = {d.id: d.status for d in repo.list_donations()}
    assert statuses["dn-bread"] is DonationStatus.AVAILABLE
    assert statuses["dn-pastry"] is DonationStatus.RESERVED   # whole-listing claim
    assert statuses["dn-fruit"] is DonationStatus.COLLECTED
    assert statuses["dn-salad"] is DonationStatus.EXPIRED


def test_collected_seed_donations_have_stamped_claims(repo, now):
    """Every COLLECTED donation must have a claim with a collection time;
    an expired one must not be collected."""
    seed_repository(repo, now)

    for d in repo.list_donations(status=DonationStatus.COLLECTED):
        claim = repo.get_claim_for_donation(d.id)
        assert claim is not None
        assert claim.is_collected, f"{d.id} collected but claim not stamped"

    expired = repo.list_donations(status=DonationStatus.EXPIRED)
    for d in expired:
        claim = repo.get_claim_for_donation(d.id)
        # Expired donations were never collected.
        assert claim is None or not claim.is_collected


def test_reserved_seed_donations_are_not_collected(repo, now):
    seed_repository(repo, now)
    reserved = repo.list_donations(status=DonationStatus.RESERVED)
    assert reserved  # at least one reserved (claimed-but-uncollected) donation
    for d in reserved:
        claim = repo.get_claim_for_donation(d.id)
        assert claim is not None
        assert not claim.is_collected


def test_ensure_seeded_is_idempotent(repo, now):
    assert is_seeded(repo) is False

    assert ensure_seeded(repo, now) is True        # first call seeds
    count_after_first = len(repo.list_donations())

    assert ensure_seeded(repo, now) is False       # second call is a no-op
    assert len(repo.list_donations()) == count_after_first
    assert len(repo.list_donors()) == 5


def test_ensure_seeded_preserves_user_created_data(repo, now):
    ensure_seeded(repo, now)
    repo.add_donor(Donor("user-donor", "My Cafe", DonorType.CAFE, Location("Central")))
    repo.add_donation(Donation(
        "user-dn", "user-donor", "My listing", "", FoodCategory.BAKERY,
        Quantity(5, Unit.LOAVES), Location("Central"),
        collection_deadline=now + timedelta(days=1), created_at=now,
    ))

    # A later start must NOT re-seed or wipe the user's data.
    assert ensure_seeded(repo, now) is False
    assert repo.get_donation("user-dn").title == "My listing"
    assert "user-donor" in {d.id for d in repo.list_donors()}
