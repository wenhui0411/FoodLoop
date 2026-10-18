"""Tests for township insights (claim-accurate summaries + observations)."""

from __future__ import annotations

from datetime import timedelta

from foodloop import insights
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
        accepted_categories={FoodCategory.BAKERY, FoodCategory.DAIRY},
        storage_capabilities={StorageRequirement.AMBIENT}, verified=True,
    ))


def _listing(repo, now, did, *, area="Central", category=FoodCategory.BAKERY,
             short=False):
    deadline = now + (timedelta(minutes=1) if short else timedelta(days=1))
    repo.add_donation(Donation(
        did, "d1", f"Item {did}", "", category, Quantity(10, Unit.LOAVES),
        Location(area), collection_deadline=deadline, created_at=now,
        status=DonationStatus.AVAILABLE,
    ))


def _collect(repo, now, cid, did):
    repo.create_claim(claim_id=cid, donation_id=did, recipient_id="r1",
                      claimed_at=now, now=now)
    repo.confirm_collection(cid, now + timedelta(hours=1), now=now + timedelta(hours=1))


def _reserve(repo, now, cid, did):
    repo.create_claim(claim_id=cid, donation_id=did, recipient_id="r1",
                      claimed_at=now, now=now)


# --------------------------------------------------------------------------
# Dataset composition (demo vs user) — listing-level, unchanged
# --------------------------------------------------------------------------
def test_dataset_composition_splits_demo_and_user(repo, now):
    _setup(repo)
    for did in ("dn-bread", "dn-abc123", "dn-xyz789"):
        _listing(repo, now, did)

    comp = insights.dataset_composition(repo, demo_ids=frozenset({"dn-bread"}))
    assert comp.total == 3
    assert comp.demo == 1
    assert comp.user == 2


def test_dataset_composition_matches_real_seed_ids(repo, now):
    from data.seed import SEED_DONATION_IDS, ensure_seeded

    ensure_seeded(repo, now)
    comp = insights.dataset_composition(repo, SEED_DONATION_IDS)
    assert comp.total == comp.demo
    assert comp.user == 0
    assert comp.demo == len(SEED_DONATION_IDS)


# --------------------------------------------------------------------------
# Area + category breakdowns (claim-based handovers)
# --------------------------------------------------------------------------
def test_area_breakdown_counts_claims_and_rate_threshold(repo, now):
    _setup(repo)
    # Central: 3 collected claims + 1 expired claim = 4 concluded (>= MIN_SAMPLE).
    for i in range(3):
        _listing(repo, now, f"c{i}", area="Central")
        _collect(repo, now, f"cc{i}", f"c{i}")
    _listing(repo, now, "e0", area="Central", short=True)
    _reserve(repo, now, "ce0", "e0")
    # Riverside: only 1 concluded claim (< MIN_SAMPLE -> rate suppressed).
    _listing(repo, now, "r0", area="Riverside")
    _collect(repo, now, "cr0", "r0")
    repo.expire_overdue(now + timedelta(minutes=5))  # ce0 -> EXPIRED

    stats = {s.area: s for s in insights.area_breakdown(repo)}
    assert stats["Central"].listed == 4          # donation listings
    assert stats["Central"].collected == 3       # collected claims
    assert stats["Central"].expired == 1         # expired claims
    assert stats["Central"].collection_rate == 0.75
    assert stats["Riverside"].collection_rate is None


def test_area_breakdown_is_ordered_by_listed_then_name(repo, now):
    _setup(repo)
    _listing(repo, now, "a", area="Zone B")
    _listing(repo, now, "b", area="Zone A")
    _listing(repo, now, "c", area="Zone A")
    stats = insights.area_breakdown(repo)
    assert [s.area for s in stats] == ["Zone A", "Zone B"]


def test_category_breakdown_counts_claims(repo, now):
    _setup(repo)
    _listing(repo, now, "b1", category=FoodCategory.BAKERY)
    _collect(repo, now, "cb1", "b1")
    _listing(repo, now, "b2", category=FoodCategory.BAKERY, short=True)
    _reserve(repo, now, "cb2", "b2")
    _listing(repo, now, "d1", category=FoodCategory.DAIRY)  # available, no claim
    repo.expire_overdue(now + timedelta(minutes=5))         # cb2 -> EXPIRED

    stats = {s.category: s for s in insights.category_breakdown(repo)}
    assert stats["bakery"].listed == 2
    assert stats["bakery"].collected == 1
    assert stats["bakery"].expired == 1
    assert stats["dairy"].listed == 1
    assert stats["dairy"].collected == 0


# --------------------------------------------------------------------------
# Observations (claim-based)
# --------------------------------------------------------------------------
def test_no_observations_when_insufficient_data(repo, now):
    _setup(repo)
    _listing(repo, now, "c0")
    _collect(repo, now, "cc0", "c0")  # only 1 concluded claim
    assert insights.observations(repo, now=now) == []


def test_observations_report_missed_collections_with_counts(repo, now):
    _setup(repo)
    for i in range(2):
        _listing(repo, now, f"c{i}", area="Central")
        _collect(repo, now, f"cc{i}", f"c{i}")
    for i in range(2):
        _listing(repo, now, f"e{i}", area="Hillview", short=True)
        _reserve(repo, now, f"ce{i}", f"e{i}")
    repo.expire_overdue(now + timedelta(minutes=5))

    obs = insights.observations(repo, now=now)
    assert obs
    missed = next(o for o in obs if o.title == "Missed collections")
    assert "2 of 4" in missed.detail
    assert any("Hillview" in o.counts for o in obs)


def test_observations_positive_when_no_expiries(repo, now):
    _setup(repo)
    for i in range(3):
        _listing(repo, now, f"c{i}")
        _collect(repo, now, f"cc{i}", f"c{i}")
    obs = insights.observations(repo, now=now)
    assert obs[0].title == "No missed collections"


def test_observations_capped_and_deterministic(repo, now):
    _setup(repo)
    for i in range(4):
        _listing(repo, now, f"c{i}", category=FoodCategory.BAKERY)
        _collect(repo, now, f"cc{i}", f"c{i}")
    for i in range(4):
        _listing(repo, now, f"e{i}", category=FoodCategory.DAIRY,
                 area="Hillview", short=True)
        _reserve(repo, now, f"ce{i}", f"e{i}")
    repo.expire_overdue(now + timedelta(minutes=5))

    first = insights.observations(repo, now=now)
    second = insights.observations(repo, now=now)
    assert len(first) <= 3
    assert [o.title for o in first] == [o.title for o in second]
