"""Tests for the FoodLoopService use-case layer (the UI-facing API).

Covers the complete create -> claim -> confirm-collection slice plus the
failure paths: invalid input, ineligible recipients, expired deadlines,
duplicate claims, invalid status transitions, and the rule that only
confirmed collections count toward completed-donation metrics.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foodloop import impact
from foodloop.errors import (
    ClaimsClosedError,
    EligibilityError,
    InvalidTransitionError,
    NotFoundError,
    OverReservationError,
    ValidationError,
)
from foodloop.models import (
    ClaimStatus,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
def _add_donor(repo, donor_id="d1"):
    repo.add_donor(Donor(donor_id, "Bakery", DonorType.BAKERY, Location("Central")))


def _add_recipient(repo, rid="r1", **overrides):
    base = dict(
        id=rid, name=f"Org {rid}", recipient_type=RecipientType.FOOD_BANK,
        location=Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=100,
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=7, collection_end_hour=20,
        verified=True,
    )
    base.update(overrides)
    repo.add_recipient(Recipient(**base))


def _create_donation(service, now, **overrides):
    kwargs = dict(
        donor_id="d1", title="Bread", description="End of day",
        category=FoodCategory.BAKERY, quantity_value=10, quantity_unit=Unit.LOAVES,
        area="Central", collection_deadline=now + timedelta(days=1),
    )
    kwargs.update(overrides)
    return service.create_donation(**kwargs)


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
def test_create_donation_persists_available_listing(service, repo, now):
    _add_donor(repo)
    donation = _create_donation(service, now)

    assert donation.status is DonationStatus.AVAILABLE
    assert donation.id.startswith("dn-")
    # It is retrievable and appears in the donor's history and marketplace.
    assert repo.get_donation(donation.id).title == "Bread"
    assert donation.id in {d.id for d in repo.list_donations(donor_id="d1")}
    assert donation.id in {
        d.id for d in repo.list_donations(status=DonationStatus.AVAILABLE)
    }


def test_create_donation_rejects_invalid_input(service, repo, now):
    _add_donor(repo)
    with pytest.raises(ValidationError):
        _create_donation(service, now, quantity_value=-1)


def test_create_donation_rejects_unknown_donor(service, now):
    with pytest.raises(NotFoundError):
        _create_donation(service, now)  # no donor added


# --------------------------------------------------------------------------
# Eligibility + claim
# --------------------------------------------------------------------------
def test_eligible_recipient_can_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now)

    claim = service.claim_donation(donation.id, "r1")
    assert claim.recipient_id == "r1"
    assert claim.collected_at is None
    # Whole-listing claim reserves everything -> donation RESERVED.
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED


def test_unverified_recipient_cannot_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, verified=False)
    donation = _create_donation(service, now)

    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1")
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE


def test_ineligible_category_cannot_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, accepted_categories={FoodCategory.DAIRY})
    donation = _create_donation(service, now)

    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1")


def test_recipient_without_collection_window_cannot_claim(service, repo, now):
    _add_donor(repo)
    # Only collects Sunday; deadline is tomorrow (Friday) -> no window.
    _add_recipient(repo, collection_days={"Sun"})
    donation = _create_donation(service, now, collection_deadline=now + timedelta(days=1))

    with pytest.raises(EligibilityError) as exc:
        service.claim_donation(donation.id, "r1")
    assert "deadline" in str(exc.value).lower() or "window" in str(exc.value).lower()


# --------------------------------------------------------------------------
# Duplicate claims
# --------------------------------------------------------------------------
def test_duplicate_claim_is_prevented(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)

    service.claim_donation(donation.id, "r1")  # reserves the whole listing
    # Nothing remains: the second claim is rejected (the donation is RESERVED,
    # so eligibility fails; over-reservation is the DB-level guard).
    with pytest.raises((EligibilityError, OverReservationError, InvalidTransitionError)):
        service.claim_donation(donation.id, "r2")
    assert repo.get_claim_for_donation(donation.id).recipient_id == "r1"


# --------------------------------------------------------------------------
# Collection + invalid transitions
# --------------------------------------------------------------------------
def test_full_create_claim_confirm_workflow(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)

    donation = _create_donation(service, now)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE

    claim = service.claim_donation(donation.id, "r1")
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    # A claim alone must NOT count as a collection.
    assert impact.completed_donation_count(repo) == 0

    confirmed = service.confirm_collection(claim.id)
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED
    assert confirmed.is_collected
    assert impact.completed_donation_count(repo) == 1


def test_cannot_confirm_an_unknown_claim(service, repo, now):
    _add_donor(repo)
    donation = _create_donation(service, now)
    # No claim exists for this donation -> confirming by a bogus id is NotFound.
    with pytest.raises(NotFoundError):
        service.confirm_collection(donation.id)


def test_cancel_claim_allows_reclaim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)

    claim = service.claim_donation(donation.id, "r1")
    service.cancel_claim(claim.id)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE

    reclaim = service.claim_donation(donation.id, "r2")
    assert reclaim.recipient_id == "r2"


def test_withdraw_closes_donation_but_keeps_reservation(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)
    service.claim_donation(donation.id, "r1")  # reserve the whole listing

    # Withdrawal closes to NEW claims; the existing reservation is preserved.
    service.cancel_donation(donation.id)
    assert repo.get_donation(donation.id).closed_to_claims is True
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    # A new claim is now refused.
    with pytest.raises((ClaimsClosedError, EligibilityError, OverReservationError)):
        service.claim_donation(donation.id, "r2")


# --------------------------------------------------------------------------
# Expiry + metrics integrity
# --------------------------------------------------------------------------
def test_expire_overdue_marks_uncollected_past_deadline(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    # Create with a valid future deadline, then advance the clock past it by
    # using a later service for expiry.
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    expired = later.expire_overdue()
    assert donation.id in expired
    assert repo.get_donation(donation.id).status is DonationStatus.EXPIRED
    # Expired food is never counted as rescued.
    assert impact.completed_donation_count(repo) == 0


def test_claimed_but_expired_does_not_count_as_collected(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))
    service.claim_donation(donation.id, "r1")

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    later.expire_overdue()

    assert repo.get_donation(donation.id).status is DonationStatus.EXPIRED
    assert impact.completed_donation_count(repo) == 0
    # The uncollected claim must not be stamped as collected.
    claim = repo.get_claim_for_donation(donation.id)
    assert claim is None or not claim.is_collected


# Small helper to run the service at a specific "now".
def FoodLoopServiceAt(repo, at: datetime):
    from foodloop.workflow import FoodLoopService

    return FoodLoopService(repo, clock=lambda: at)


# --------------------------------------------------------------------------
# M3: donor pickup-code handover verification
# --------------------------------------------------------------------------
def _reserve(service, repo, now):
    """Set up a donor (d1), eligible recipient (r1), donation and a claim.
    Returns the claim."""
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now)
    return donation, service.claim_donation(donation.id, "r1")


def test_pickup_code_is_unpredictable_and_not_the_claim_id(service, repo, now):
    _, claim = _reserve(service, repo, now)
    assert claim.pickup_code
    assert claim.pickup_code != claim.id          # requirement 1
    assert len(claim.pickup_code) == 6
    assert all(ch in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" for ch in claim.pickup_code)


def test_confirm_handover_by_code_marks_collected(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    assert impact.completed_donation_count(repo) == 0

    confirmed = service.confirm_handover("d1", claim.pickup_code)
    assert confirmed.id == claim.id
    assert confirmed.status is ClaimStatus.COLLECTED
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED
    assert impact.completed_donation_count(repo) == 1


def test_confirm_handover_is_case_and_space_insensitive(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    service.confirm_handover("d1", f"  {claim.pickup_code.lower()}  ")
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED


def test_confirm_handover_invalid_code_changes_nothing(service, repo, now):
    donation, _ = _reserve(service, repo, now)
    with pytest.raises(NotFoundError):
        service.confirm_handover("d1", "ZZZZZZ")
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0


def test_confirm_handover_wrong_donor_rejected(service, repo, now):
    _add_donor(repo, "d2")  # a different donor
    donation, claim = _reserve(service, repo, now)  # donation owned by d1
    with pytest.raises(NotFoundError):
        service.confirm_handover("d2", claim.pickup_code)
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0


def test_confirm_handover_is_single_use(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    service.confirm_handover("d1", claim.pickup_code)
    with pytest.raises(InvalidTransitionError):
        service.confirm_handover("d1", claim.pickup_code)   # reused code
    assert impact.completed_donation_count(repo) == 1        # still exactly one


def test_confirm_handover_after_deadline_rejected(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))
    claim = service.claim_donation(donation.id, "r1")

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    with pytest.raises(InvalidTransitionError):
        later.confirm_handover("d1", claim.pickup_code)
    # Unchanged (still reserved; impact zero).
    assert repo.get_claim_for_donation(donation.id).status is ClaimStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0
