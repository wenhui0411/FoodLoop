"""Tests for explainable recipient matching.

Covers: eligibility exclusion (the hard gate), deterministic ordering and
tie-breaking, each ranking factor in isolation, honest missing-data handling,
and edge cases (no eligible recipients, score bounds).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foodloop.matching import (
    DEFAULT_WEIGHTS,
    MAX_DISTANCE_KM,
    earliest_collection,
    haversine_km,
    rank_recipients,
    score_recipient,
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

ALL_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


def _recipient(rid: str, **overrides) -> Recipient:
    base = dict(
        id=rid, name=f"Org {rid}", recipient_type=RecipientType.FOOD_BANK,
        location=Location("Central"),            # no coordinates by default
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=None,
        collection_days=set(ALL_DAYS),
        collection_start_hour=0, collection_end_hour=24,
        verified=True,
    )
    base.update(overrides)
    return Recipient(**base)


def _donation(now: datetime, **overrides) -> Donation:
    base = dict(
        id="dn1", donor_id="d1", title="Bread", description="",
        category=FoodCategory.BAKERY, quantity=Quantity(10, Unit.LOAVES),
        location=Location("Central"),            # no coordinates by default
        collection_deadline=now + timedelta(days=2),
        created_at=now, status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    )
    base.update(overrides)
    return Donation(**base)


# --------------------------------------------------------------------------
# Eligibility is a hard gate
# --------------------------------------------------------------------------
def test_ineligible_recipients_are_excluded(now):
    eligible = _recipient("ok")
    unverified = _recipient("bad1", verified=False)
    wrong_cat = _recipient("bad2", accepted_categories={FoodCategory.DAIRY})
    no_window = _recipient("bad3", collection_days=set())

    recs = rank_recipients(
        _donation(now), [eligible, unverified, wrong_cat, no_window], now=now
    )
    assert [r.recipient.id for r in recs] == ["ok"]


def test_high_score_cannot_resurrect_ineligible(now):
    # A nearby recipient that would score well but is unverified must not appear.
    donation = _donation(now, location=Location("Central", -26.20, 28.04))
    strong_but_unverified = _recipient(
        "x", verified=False, location=Location("Central", -26.20, 28.04)
    )
    recs = rank_recipients(donation, [strong_but_unverified], now=now)
    assert recs == []


def test_no_eligible_recipients_returns_empty(now):
    claimed = _donation(now, status=DonationStatus.CLAIMED)
    recs = rank_recipients(claimed, [_recipient("a")], now=now)
    assert recs == []


# --------------------------------------------------------------------------
# Deterministic ordering
# --------------------------------------------------------------------------
def test_ordering_is_deterministic_and_repeatable(now):
    recipients = [_recipient(f"r{i}") for i in range(5)]
    donation = _donation(now)
    first = [r.recipient.id for r in rank_recipients(donation, recipients, now=now)]
    second = [r.recipient.id for r in rank_recipients(donation, list(reversed(recipients)), now=now)]
    assert first == second


def test_ties_broken_by_name(now):
    # Two identical recipients (same everything but name) -> alphabetical.
    a = _recipient("z-id", name="Alpha")
    b = _recipient("a-id", name="Zulu")
    recs = rank_recipients(_donation(now), [b, a], now=now)
    assert [r.recipient.name for r in recs] == ["Alpha", "Zulu"]


# --------------------------------------------------------------------------
# Timing factor
# --------------------------------------------------------------------------
def test_sooner_collector_ranks_higher(now):
    deadline = now + timedelta(days=2)
    soon = _recipient("soon", collection_days=set(ALL_DAYS))  # can collect now
    # Only collects on the deadline's weekday, late window.
    late_day = {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][deadline.weekday()]}
    late = _recipient("late", collection_days=late_day,
                      collection_start_hour=0, collection_end_hour=24)

    recs = rank_recipients(_donation(now, collection_deadline=deadline), [soon, late], now=now)
    assert recs[0].recipient.id == "soon"
    assert recs[0].score > recs[1].score


def test_earliest_collection_respects_now(now):
    r = _recipient("r")  # all days, 0-24
    e = earliest_collection(r, now + timedelta(days=1), now)
    assert e == now  # can collect immediately


# --------------------------------------------------------------------------
# Distance factor + missing data
# --------------------------------------------------------------------------
def test_closer_recipient_ranks_higher_when_only_distance_differs(now):
    donation = _donation(now, location=Location("Central", -26.2041, 28.0473))
    near = _recipient("near", location=Location("Near", -26.2050, 28.0480))
    far = _recipient("far", location=Location("Far", -26.3500, 28.2000))

    recs = rank_recipients(donation, [near, far], now=now)
    assert recs[0].recipient.id == "near"


def test_distance_unavailable_when_coordinates_missing(now):
    # Donation has no coordinates -> distance factor unavailable, not zero.
    rec = score_recipient(_recipient("r", location=Location("X", -26.2, 28.0)),
                          _donation(now), now)
    dist = next(f for f in rec.factors if f.name == "distance")
    assert dist.available is False
    assert dist.score is None
    assert "unavailable" in dist.detail.lower()


def test_missing_distance_not_counted_as_zero(now):
    """A recipient missing distance data is scored on its other factors, not
    penalised with a zero for the absent one."""
    donation = _donation(now)  # no coords -> distance unavailable for all
    rec = score_recipient(_recipient("r"), donation, now)
    # Only timing is available (non-kg, no coords) and timing margin is high.
    assert rec.score > 0.8
    assert all(f.name != "distance" or not f.available for f in rec.factors)


def test_haversine_none_without_coordinates():
    assert haversine_km(Location("A"), Location("B", -26.2, 28.0)) is None
    assert haversine_km(Location("A", -26.2, 28.0), Location("B")) is None


# --------------------------------------------------------------------------
# Capacity factor
# --------------------------------------------------------------------------
def test_capacity_scored_only_for_comparable_units(now):
    # Non-kg donation -> capacity not comparable.
    rec = score_recipient(
        _recipient("r", max_quantity_kg_equiv=50),
        _donation(now, quantity=Quantity(10, Unit.LOAVES)), now
    )
    cap = next(f for f in rec.factors if f.name == "capacity")
    assert cap.available is False


def test_more_headroom_ranks_higher_for_kg_donation(now):
    donation = _donation(now, quantity=Quantity(10, Unit.KG))
    roomy = _recipient("roomy", max_quantity_kg_equiv=100)   # lots of headroom
    tight = _recipient("tight", max_quantity_kg_equiv=12)    # just fits
    recs = rank_recipients(donation, [roomy, tight], now=now)
    assert recs[0].recipient.id == "roomy"


# --------------------------------------------------------------------------
# Score invariants
# --------------------------------------------------------------------------
def test_scores_within_unit_interval(now):
    donation = _donation(now, location=Location("Central", -26.2041, 28.0473),
                         quantity=Quantity(5, Unit.KG))
    rec = score_recipient(
        _recipient("r", location=Location("Near", -26.2050, 28.0480),
                   max_quantity_kg_equiv=50),
        donation, now
    )
    assert 0.0 <= rec.score <= 1.0
    for f in rec.factors:
        if f.available:
            assert 0.0 <= f.score <= 1.0


def test_weights_are_documented_and_sum_sensibly():
    assert set(DEFAULT_WEIGHTS) == {"timing", "distance", "capacity"}
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
