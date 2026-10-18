"""Tests for recipient eligibility rules (shared by claiming and matching)."""

from __future__ import annotations

from datetime import datetime, timedelta

from foodloop.eligibility import (
    can_collect_before,
    capacity_fits,
    check_eligibility,
)
from foodloop.models import (
    Donation,
    DonationStatus,
    FoodCategory,
    Location,
    Quantity,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)


def _recipient(**overrides) -> Recipient:
    base = dict(
        id="r1", name="Org", recipient_type=RecipientType.FOOD_BANK,
        location=Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=50,
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=8, collection_end_hour=18,
        verified=True,
    )
    base.update(overrides)
    return Recipient(**base)


def _donation(now: datetime, **overrides) -> Donation:
    base = dict(
        id="dn1", donor_id="d1", title="Bread", description="",
        category=FoodCategory.BAKERY, quantity=Quantity(10, Unit.KG),
        location=Location("Central"),
        collection_deadline=now + timedelta(days=1),
        created_at=now, status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    )
    base.update(overrides)
    return Donation(**base)


# --- can_collect_before ----------------------------------------------------
def test_can_collect_within_window(now):
    r = _recipient()
    assert can_collect_before(r, now + timedelta(days=1), now) is True


def test_cannot_collect_when_deadline_in_past(now):
    r = _recipient()
    assert can_collect_before(r, now - timedelta(hours=1), now) is False


def test_cannot_collect_without_matching_day(now):
    # now is Thursday; deadline Friday morning. Recipient only collects Sunday.
    r = _recipient(collection_days={"Sun"})
    assert can_collect_before(r, now + timedelta(days=1), now) is False


def test_cannot_collect_without_any_days(now):
    r = _recipient(collection_days=set())
    assert can_collect_before(r, now + timedelta(days=2), now) is False


def test_cannot_collect_when_window_after_deadline(now):
    # Deadline today at 10:00 but recipient only opens at 14:00.
    r = _recipient(collection_start_hour=14, collection_end_hour=18)
    deadline = now.replace(hour=10)
    assert can_collect_before(r, deadline, now) is False


# --- capacity_fits ---------------------------------------------------------
def test_capacity_comparable_and_fits(now):
    r = _recipient(max_quantity_kg_equiv=50)
    fits, comparable = capacity_fits(r, _donation(now, quantity=Quantity(40, Unit.KG)))
    assert (fits, comparable) == (True, True)


def test_capacity_comparable_and_exceeds(now):
    r = _recipient(max_quantity_kg_equiv=50)
    fits, comparable = capacity_fits(r, _donation(now, quantity=Quantity(80, Unit.KG)))
    assert (fits, comparable) == (False, True)


def test_capacity_not_comparable_for_countable_units(now):
    r = _recipient(max_quantity_kg_equiv=50)
    fits, comparable = capacity_fits(r, _donation(now, quantity=Quantity(500, Unit.ITEMS)))
    # Not a hard blocker when units aren't kg.
    assert (fits, comparable) == (True, False)


# --- check_eligibility -----------------------------------------------------
def test_fully_eligible(now):
    result = check_eligibility(_recipient(), _donation(now), now=now)
    assert result.eligible is True
    assert result.reasons == []


def test_unverified_recipient_not_eligible(now):
    result = check_eligibility(_recipient(verified=False), _donation(now), now=now)
    assert result.eligible is False
    assert any("verified" in r.lower() for r in result.reasons)


def test_wrong_category_not_eligible(now):
    r = _recipient(accepted_categories={FoodCategory.DAIRY})
    result = check_eligibility(r, _donation(now), now=now)
    assert result.eligible is False


def test_missing_storage_capability_not_eligible(now):
    r = _recipient(storage_capabilities={StorageRequirement.AMBIENT})
    d = _donation(now, storage=StorageRequirement.FROZEN)
    result = check_eligibility(r, d, now=now)
    assert result.eligible is False


def test_over_capacity_not_eligible(now):
    r = _recipient(max_quantity_kg_equiv=5)
    d = _donation(now, quantity=Quantity(100, Unit.KG))
    result = check_eligibility(r, d, now=now)
    assert result.eligible is False


def test_non_available_donation_not_eligible(now):
    d = _donation(now, status=DonationStatus.CLAIMED)
    result = check_eligibility(_recipient(), d, now=now)
    assert result.eligible is False


def test_checks_are_exposed_for_explainability(now):
    result = check_eligibility(_recipient(), _donation(now), now=now)
    names = {c.name for c in result.checks}
    assert names == {
        "availability", "verified", "category", "storage", "deadline", "capacity"
    }
